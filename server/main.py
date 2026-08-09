"""Hermes-live 本地服务入口。

    .venv/bin/python -m server.main [--config config.yaml] [--port 8698] [--no-preload]

只读依赖本机 Hermes gateway(8647)；绝不重启/修改 gateway，不碰 state.db。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .asr import ASR, SherpaStreamingASR
from .config import PROJECT_ROOT, load_config
from .hermes_client import HermesClient
from .protocol import unpack_audio_up
from .session import Session
from .tts import TTSEngine

logger = logging.getLogger("hermes-live")
WEB_DIR = PROJECT_ROOT / "web"


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


UPLOAD_MAX = 200 * 1024 * 1024  # 与 areco 同款 200MB 上限


def _sanitize_name(raw: str) -> str:
    """照抄 areco 落盘规则：basename → 非法字符换 _ → 截尾 120 保扩展名 → 空则 file。"""
    import re
    base = Path(raw).name
    base = re.sub(r'[/\\:*?"<>|]', "_", base)[-120:]
    return base or "file"


def _check_token(request: web.Request) -> None:
    """回环免 token（壳/本机浏览器打开的页面不带 token）；外部地址必须带对。"""
    if request.remote in ("127.0.0.1", "::1"):
        return
    cfg = request.app["cfg"]
    if cfg.auth_token and request.query.get("token") != cfg.auth_token:
        raise web.HTTPUnauthorized(text="bad token")


async def handle_upload(request: web.Request) -> web.Response:
    """POST /api/files/upload?name=<urlencoded原名>，raw body 直收流（areco 同构）。
    响应 {ok, data:{path, size}}；前端把 path 当纯文本回填输入框，无附件字段。"""
    _check_token(request)
    base = _sanitize_name(request.query.get("name", "file"))
    day = time.strftime("%Y-%m-%d")
    dir_ = PROJECT_ROOT / "tmp" / "uploads" / day
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / base
    stem, ext = target.stem, target.suffix
    i = 2
    while target.exists():                      # 重名加序号，不覆盖
        target = dir_ / f"{stem}-{i}{ext}"
        i += 1
    size = 0
    try:
        with open(target, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 16):
                size += len(chunk)
                if size > UPLOAD_MAX:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=UPLOAD_MAX, actual_size=size)
                f.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)          # 半截文件不留
        raise
    return web.json_response(
        {"ok": True, "data": {"path": str(target), "size": size}},
        headers={"Access-Control-Allow-Origin": "*"})


async def handle_upload_options(request: web.Request) -> web.Response:
    # 手机页面在回环域、上传目标在 Mac —— 跨源预检放行（token 仍拦真请求）
    return web.Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })


async def handle_health(request: web.Request) -> web.Response:
    hermes: HermesClient = request.app["hermes"]
    return web.json_response({
        "ok": True,
        "hermes": await hermes.health(),
        "asr_model": ("sherpa-paraformer" if request.app.get("asr_stream") and request.app["asr_stream"].available
                      else request.app["cfg"].asr_model),
        "voice": request.app["cfg"].tts_voice,
        "session_id": hermes.session_id,
    })


async def ws_writer(ws: web.WebSocketResponse, outbox: asyncio.Queue) -> None:
    """唯一写者：保证多任务出站消息不交错。"""
    while True:
        item = await outbox.get()
        if item is None:
            return
        kind, payload = item
        if kind == "json":
            await ws.send_str(payload)
        else:
            await ws.send_bytes(payload)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    _check_token(request)
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    app = request.app
    outbox: asyncio.Queue = asyncio.Queue()
    session = Session(app["cfg"], app["asr"], app["tts"], app["hermes"], outbox,
                      asr_stream=app.get("asr_stream"))
    writer = asyncio.create_task(ws_writer(ws, outbox))
    session.send_json("hello", session_id=app["hermes"].session_id,
                      voice=app["cfg"].tts_voice, asr_model=app["cfg"].asr_model)
    logger.info("ws connected")
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                pcm = unpack_audio_up(msg.data)
                if pcm is not None:
                    await session.on_audio(pcm)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    obj = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await session.on_control(obj)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                break
    finally:
        await session.close()
        outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer, timeout=2)
        if not writer.done():
            writer.cancel()
        logger.info("ws closed")
    return ws


def build_app(cfg, preload: bool = True) -> web.Application:
    app = web.Application()
    app["cfg"] = cfg
    app["asr"] = ASR(cfg)
    app["asr_stream"] = SherpaStreamingASR(cfg)  # 流式首选；load() 失败时静默回退 ASR
    app["tts"] = TTSEngine(cfg)

    async def on_startup(app: web.Application) -> None:
        app["http"] = aiohttp.ClientSession()
        app["hermes"] = HermesClient(cfg, app["http"])
        if preload:
            # 优先加载流式 ASR（快，0.7s）；失败则回退加载批量 ASR（慢，~6s）
            if app["asr_stream"].load():
                logger.info("streaming ASR (sherpa-onnx) ready")
            else:
                logger.info("sherpa-onnx 不可用，加载批量 ASR ...")
                app["asr_stream"] = None
                await asyncio.to_thread(app["asr"].load)
        if await app["hermes"].health():
            logger.info("hermes gateway ok @ %s", cfg.hermes_base_url)
        else:
            logger.warning("hermes gateway UNREACHABLE @ %s（服务仍启动，恢复后自动可用）",
                           cfg.hermes_base_url)

    async def on_cleanup(app: web.Application) -> None:
        await app["http"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/api/files/upload", handle_upload)
    app.router.add_options("/api/files/upload", handle_upload_options)
    app.router.add_get("/ws", handle_ws)
    app.router.add_static("/web", WEB_DIR)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes-live server")
    parser.add_argument("--config", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-preload", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.port:
        cfg.port = args.port
    app = build_app(cfg, preload=not args.no_preload)
    logger.info("Hermes-live → http://%s:%s", cfg.host, cfg.port)
    web.run_app(app, host=cfg.host, port=cfg.port, print=None)


if __name__ == "__main__":
    main()
