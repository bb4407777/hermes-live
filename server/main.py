"""Hermes-live 本地服务入口。

    .venv/bin/python -m server.main [--config config.yaml] [--port 8698] [--no-preload]

只读依赖本机 ACP 桥（8647，server/acp_bridge.py，后端=中枢同款 CodeBuddy CLI）；绝不重启/修改桥子进程。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hmac
import json
import logging
import os
import signal
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.web_log import AccessLogger, KeyMethod

from . import __version__
from .asr import ASR, SherpaStreamingASR, DoubaoStreamingASR
from .config import PROJECT_ROOT, load_config
from .hermes_client import HermesClient
from .protocol import unpack_audio_up
from .qwen_asr import QwenSidecarASR
from .session import Session
from .tts import TTSEngine

logger = logging.getLogger("hermes-live")
WEB_DIR = PROJECT_ROOT / "web"


class TokenMaskingAccessLogger(AccessLogger):
    """访问日志里遮掉 auth_token。

    默认 LOG_FORMAT 的 %r（请求行）和 %{Referer}i 都会带上 ?token=…，
    实测已把明文 token 写进 logs/service.log；这里包住父类编译出的每个
    取值函数，对结果做替换（_format_* 是绑到 AccessLogger 上的 staticmethod，
    覆写它们不生效，只能包 _methods）。
    """

    def __init__(self, logger, log_format):
        super().__init__(logger, log_format)
        self._methods = [KeyMethod(k, functools.partial(self._masked, fn))
                         for k, fn in self._methods]

    def _masked(self, fn, *args):
        text = fn(*args)
        request = args[0]
        # %s/%b 这类取值函数返回 int，直接 `token in text` 会抛 TypeError；
        # 而 AccessLogger.log 整个包在 try/except 里 —— 一旦抛异常这行日志被静默丢弃，
        # 等于把访问日志全关了。
        if not isinstance(text, str) or request is None:
            return text
        cfg = request.app.get("cfg")
        token = getattr(cfg, "auth_token", "") if cfg is not None else ""
        return text.replace(token, "***") if token and token in text else text


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


@web.middleware
async def no_cache_assets(request: web.Request, handler) -> web.StreamResponse:
    """前端三个 JS 互相依赖（app.js → audio.js → worklet），版本错配会整场静音：
    0.4.6 的 app.js 不发 offset，新的 player-processor 于是把每块音频解成 0 个样本，
    而麦克风与 ASR 一切正常，页面看不出任何异常（2026-09-20 线上「没有声音」即此）。
    aiohttp 的静态只发 ETag 不发 Cache-Control，Safari/Chrome 便按「距 Last-Modified
    的约 10%」各自启发式缓存 → 刷新后半新半旧。no-cache 强制每次带 ETag 回源校验，
    没变就是 304 几百字节，代价可忽略。"""
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/web/"):
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


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
    auth = request.headers.get("Authorization", "")
    supplied = request.query.get("token") or (
        auth[len("Bearer "):] if auth.startswith("Bearer ") else "")
    if supplied and hmac.compare_digest(supplied, cfg.auth_token):
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
        "version": __version__,
        "hermes": await hermes.health(),
        "auth_required": bool(request.app["cfg"].auth_token),
        "asr_model": (
            "qwen3-asr-1.7b" if request.app.get("qwen_asr") and request.app["qwen_asr"].available
            else "doubao" if request.app.get("doubao_asr") and request.app["doubao_asr"].available
            else "sherpa-paraformer" if request.app.get("asr_stream") and request.app["asr_stream"].available
            else request.app["asr"].backend_label or request.app["cfg"].asr_model
        ),
        "voice": request.app["cfg"].tts_voice,
        # 不回显 session_id：这个端点不鉴权，会话标识属于外部可读信息
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


async def ws_writer(ws: web.WebSocketResponse, outbox: asyncio.Queue,
                    session: Session | None = None) -> None:
    """唯一写者：保证多任务出站消息不交错。

    发送失败（隧道抽风/手机息屏半开）时不能只让协程死掉：Session 还在往无界
    outbox 里塞音频帧，turn 协程也会一直卡在等永不再来的 playback_done。
    所以失败要回告 Session：停止出站 + 打断在途 turn。
    """
    while True:
        item = await outbox.get()
        if item is None:
            return
        kind, payload = item
        try:
            if kind == "json":
                await ws.send_str(payload)
            else:
                await ws.send_bytes(payload)
        except Exception as exc:
            logger.warning("ws 出站失败，停止发送：%s: %s", type(exc).__name__, exc)
            if session is not None:
                session.on_conn_lost()
            return


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    _check_token(request)
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    app = request.app
    # 多设备并行：每个 WS 连接一个独立 Session（2026-11-07 高律师令：手机与电脑同时用）。
    # 有状态的东西一律按连接隔离——ASR 的 queue/future/task、hermes 会话 id、turn 与 outbox；
    # 只有本身无会话态、可并发复用的引擎留在 app 级共享（TTS、批量 ASR、sherpa recognizer）。
    # 早期版本是单活跃连接（新连接顶替旧连接），两个 Session 抢同一条 ASR 链路会互相
    # reset 导致空结果/窜流；下面这些共享点逐个改成按连接实例后即可并存。
    live = app["live_state"]   # 启动后 app[...] 不可写（aiohttp 弃用），状态挂可变 dict
    outbox: asyncio.Queue = asyncio.Queue()
    # doubao ASR 按连接实例化（load 仅读凭据文件，轻量）：
    # 会话态 queue/future/task 决不跨连接共享，否则互相 reset/覆盖导致空结果与窜流
    doubao = None
    probe = app.get("doubao_asr")
    if probe is not None and probe.available:
        doubao = DoubaoStreamingASR(app["cfg"])
        doubao.load()
    # hermes 客户端同样按连接实例化：session_id 是实例字段，共享会让两路对话串历史
    hermes = HermesClient(app["cfg"], app["http"])
    # sherpa 的 recognizer 权重共享，stream 必须各一条（load 重、create_stream 轻）
    asr_stream = app["asr_stream"].spawn() if app.get("asr_stream") else None
    session = Session(app["cfg"], app["asr"], app["tts"], hermes, outbox,
                      asr_stream=asr_stream,
                      doubao_asr=doubao,
                      qwen_asr=app.get("qwen_asr"))
    live["sessions"].add(session)
    session.ws = ws
    session.last_active = time.monotonic()
    writer = asyncio.create_task(ws_writer(ws, outbox, session))
    session.send_json("hello", session_id=hermes.session_id,
                      voice=app["cfg"].tts_voice, asr_model=session.asr_label())
    logger.info("ws connected（活跃连接 %d）", len(live["sessions"]))
    try:
        async for msg in ws:
            session.last_active = time.monotonic()
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
        live["sessions"].discard(session)
        await session.close()
        outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer, timeout=2)
        if not writer.done():
            writer.cancel()
        logger.info("ws closed")
    return ws


async def _housekeeping(app: web.Application) -> None:
    """两件事：① 空闲会话回收（session_idle_hours，同微信侧 4 小时口径）；
    ② 进程到龄自退（recycle_hours，仅在无活跃会话时，launchd 立刻拉起新进程）。

    ①只断开连接、不杀进程；前端收到 4002 后不自动重连，用户再点开始即起一条全新会话。
    """
    cfg = app["cfg"]
    idle = cfg.session_idle_hours * 3600
    recycle = cfg.recycle_hours * 3600
    spans = [s for s in (idle, recycle) if s > 0]
    if not spans:
        return
    tick = min(60.0, max(1.0, min(spans) / 8))   # 4 小时口径＝每分钟扫一次
    while True:
        await asyncio.sleep(tick)
        now = time.monotonic()
        sessions = list(app["live_state"]["sessions"])
        if idle > 0:
            for session in sessions:
                if session.closed or session.ws is None:
                    continue
                if now - session.last_active < idle:
                    continue
                logger.info("会话空闲超过 %g 小时，自动关闭", idle / 3600)
                with contextlib.suppress(Exception):
                    session.send_json("bye", reason="idle_timeout")
                    await asyncio.sleep(0.2)
                    await session.ws.close(code=4002, message=b"idle timeout")
        if recycle > 0 and not sessions:
            uptime = now - app["started_at"]
            if uptime >= recycle:
                logger.info("进程在役 %.1f 小时且无活跃会话，退出待拉起", uptime / 3600)
                os.kill(os.getpid(), signal.SIGTERM)   # web.run_app 走正常收尾（含 sidecar 回收）
                return


def build_app(cfg, preload: bool = True) -> web.Application:
    app = web.Application(middlewares=[no_cache_assets])
    app["cfg"] = cfg
    app["live_state"] = {"sessions": set()}   # 全部活跃 WS 的 Session（多设备并行）
    app["asr"] = ASR(cfg)
    app["asr_stream"] = SherpaStreamingASR(cfg)
    app["doubao_asr"] = DoubaoStreamingASR(cfg)
    app["qwen_asr"] = QwenSidecarASR(cfg)
    app["tts"] = TTSEngine(cfg)

    async def on_startup(app: web.Application) -> None:
        app["http"] = aiohttp.ClientSession()
        app["hermes"] = HermesClient(cfg, app["http"])
        app["started_at"] = time.monotonic()
        app["housekeeping"] = asyncio.create_task(_housekeeping(app))
        if preload:
            # TTS：kokoro 优先，edge-tts 兜底
            app["tts"].load()
            # ASR 优先链：qwen3-asr 本地 sidecar（离线、weSaw 同款）> 豆包云端 > sherpa > 批量
            if cfg.asr_backend in ("auto", "qwen") and app["qwen_asr"].load():
                logger.info("ASR: qwen3-asr sidecar（本地批量，无实时字幕）")
                app["asr_stream"] = None
                app["doubao_asr"] = None
                if cfg.asr_qwen_warm_at_boot:
                    # 后台预热，不阻塞服务启动；ready 前说话会等到模型加载完
                    asyncio.create_task(app["qwen_asr"].ensure_started())
            elif cfg.asr_backend in ("auto", "doubao") and app["doubao_asr"].load():
                logger.info("ASR: doubaoime（豆包云端流式）ready")
                app["asr_stream"] = None  # doubao 优先，sherpa 不再加载
            elif cfg.asr_backend in ("auto", "sherpa") and app["asr_stream"].load():
                logger.info("ASR: streaming sherpa-onnx ready")
                app["doubao_asr"] = None
            else:
                logger.info("无流式 ASR 可用（asr_backend=%s），加载批量兜底 ...", cfg.asr_backend)
                app["asr_stream"] = None
                app["doubao_asr"] = None
                await asyncio.to_thread(app["asr"].load)
        if await app["hermes"].health():
            logger.info("hermes gateway ok @ %s", cfg.hermes_base_url)
        else:
            logger.warning("hermes gateway UNREACHABLE @ %s（服务仍启动，恢复后自动可用）",
                           cfg.hermes_base_url)

    async def on_cleanup(app: web.Application) -> None:
        app["housekeeping"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await app["housekeeping"]
        await app["http"].close()
        app["qwen_asr"].shutdown()   # 结束 sidecar，防 launchd 重启窗口期双模型驻留

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
    logger.info("Hermes-live v%s → http://%s:%s", __version__, cfg.host, cfg.port)
    web.run_app(app, host=cfg.host, port=cfg.port, print=None,
                access_log_class=TokenMaskingAccessLogger)


if __name__ == "__main__":
    main()
