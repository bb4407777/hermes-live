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

from .asr import ASR, SherpaStreamingASR, DoubaoStreamingASR
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
    """本机回环免 token；其余来源（LAN、Cloudflare Tunnel）必须带对。

    隧道流量由本机 cloudflared 经回环转发，单看 remote 会被回环豁免放行——
    用 Cloudflare edge 注入的 Cf-Ray 头识别隧道流量，对其强制 token。
    token 接受两种携带方式：?token= 查询参数（/ws）或 Authorization: Bearer（API）。
    """
    via_tunnel = "cf-ray" in request.headers
    if request.remote in ("127.0.0.1", "::1") and not via_tunnel:
        return
    cfg = request.app["cfg"]
    if not cfg.auth_token:
        return
    if (request.query.get("token") == cfg.auth_token
            or request.headers.get("Authorization") == f"Bearer {cfg.auth_token}"):
        return
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
        "asr_model": (
            "doubao" if request.app.get("doubao_asr") and request.app["doubao_asr"].available
            else "sherpa-paraformer" if request.app.get("asr_stream") and request.app["asr_stream"].available
            else request.app["cfg"].asr_model
        ),
        "voice": request.app["cfg"].tts_voice,
        "session_id": hermes.session_id,
    })


async def handle_restart(request: web.Request) -> web.Response:
    """重启服务：调用外部脚本重启（参考 areco 模式）。

    用户手动触发的重启（网页按钮）是允许的；AI agent 不得主动调用此接口。
    外部脚本会优雅停止当前进程并拉起新进程，避免自杀式重启带来的竞态问题。
    """
    _check_token(request)
    import subprocess

    logger.info("收到重启请求（用户手动触发），调用外部重启脚本...")

    restart_script = PROJECT_ROOT / "scripts" / "restart-service.sh"
    if not restart_script.exists():
        logger.error("重启脚本不存在: %s", restart_script)
        return web.json_response(
            {"ok": False, "message": "重启脚本不存在"},
            status=500
        )

    # 后台启动重启脚本（detached，避免阻塞响应）
    try:
        subprocess.Popen(
            [str(restart_script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # 完全脱离当前进程组
        )
        logger.info("重启脚本已启动，服务将在数秒内重启")
        return web.json_response({"ok": True, "message": "重启脚本已启动，服务将在数秒内重启"})
    except Exception as e:
        logger.error("启动重启脚本失败: %s", e)
        return web.json_response(
            {"ok": False, "message": f"启动重启脚本失败: {e}"},
            status=500
        )


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
                      asr_stream=app.get("asr_stream"),
                      doubao_asr=app.get("doubao_asr"))
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
    app["asr_stream"] = SherpaStreamingASR(cfg)
    app["doubao_asr"] = DoubaoStreamingASR(cfg)
    app["tts"] = TTSEngine(cfg)

    async def on_startup(app: web.Application) -> None:
        app["http"] = aiohttp.ClientSession()
        app["hermes"] = HermesClient(cfg, app["http"])
        if preload:
            # TTS：kokoro 优先，edge-tts 兜底
            app["tts"].load()
            # 优先豆包云端（联网，中文极准）；失败则 sherpa；再失败则批量 ASR
            if cfg.asr_backend in ("auto", "doubao") and app["doubao_asr"].load():
                logger.info("ASR: doubaoime（豆包云端流式）ready")
                app["asr_stream"] = None  # doubao 优先，sherpa 不再加载
            elif cfg.asr_backend in ("auto", "sherpa") and app["asr_stream"].load():
                logger.info("ASR: streaming sherpa-onnx ready")
                app["doubao_asr"] = None
            else:
                logger.info("sherpa-onnx 不可用，加载批量 ASR ...")
                app["asr_stream"] = None
                app["doubao_asr"] = None
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
    app.router.add_post("/api/restart", handle_restart)
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
