"""ACP→OpenAI 兼容桥：顶替退役 Hermes gateway 的 127.0.0.1:8647 插槽。

背景（2026-10-05 高律师定「hermes-live 后端转为中枢通版 CodeBuddy 的 DeepSeek」）：
- 10-02 Hermes gateway 退役后 8647 无人监听，hermes-live 语音链路断了大脑
  （ASR/TTS 正常，health 回 hermes:false，日志持续 UNREACHABLE）。
- 中枢现役引擎＝主版 WorkBuddy CLI + deepseek-v4.1-flash，经 ACP stdio 供 cc-connect
  消费（薄桥 ~/skills/cc-connect/scripts/workbuddy-acp-clean.mjs，spawn 参数见
  ~/.cc-connect/config.toml [projects.agent.options]）。
- 本服务用与中枢完全相同的 spawn 参数拉起 CLI，把 hermes_live 客户端
  （server/hermes_client.py）发的 OpenAI 兼容 /v1/chat/completions SSE 翻译成
  ACP session/prompt——hermes-live 本体零改动、无需重启（每轮现发 POST，无缓存状态）。

线格式（2026-10-05 对主版 CLI 实测，/tmp/acp_probe.py 探针）：
- initialize {protocolVersion:1} → result.agentCapabilities
- session/new {cwd, mcpServers:[]} → result.sessionId（冷启动约 10s，故启动即预热一个）
- session/prompt {sessionId, prompt:[{type:"text",text}]} 流式回：
    session/update agent_thought_chunk  = 思考流（整条丢弃，防语音把思考念出来——
                                        与中枢薄桥同款过滤）
    session/update agent_message_chunk  = 正文 delta
    session/update tool_call/tool_call_update = 工具进度（转 hermes.tool.progress 事件）
    最终 RESP 同 id {stopReason:"end_turn"} → finish_reason=stop → [DONE]
- 客户端断开（语音打断/barge-in）→ session/cancel，对齐原 gateway 的 agent.interrupt()

会话语义（对齐 hermes_client.py 的注释）：
- hermes-live 每条 WS 连接持一个自铸的 X-Hermes-Session-Id（hl-xxx，跨重连保持，
  点「新会话」才轮换）；本桥维护 hl-id → ACP sessionId 映射，首见即建。
- 桥进程重启/CLI 崩溃重拉 → 映射清空 → 各 hl-id 重新开新 ACP 会话（上下文清零，
  与原 gateway 重启后 state.db 会话失联的行为同级，可接受）。

安全边界：只绑 127.0.0.1；与中枢同款 --permission-mode bypassPermissions（语音场景
无人点权限卡）+ hindsight MCP（wai-mcp.json，与中枢同一份）。要收工具面：删
--mcp-config 或加 --tools ""。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import uuid
from pathlib import Path

import yaml
from aiohttp import web

log = logging.getLogger("acp_bridge")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# spawn 参数对齐 ~/.cc-connect/config.toml 的中枢引擎形态；CLI 与模型三档来源：
# 代码默认 → config.yaml acp_cli/acp_model → env ACP_BRIDGE_CLI/ACP_BRIDGE_MODEL 覆盖。
# CLI 两家：主版=WorkBuddy.app（copilot.tencent.com）、AI 版=WorkBuddy AI.app（workbuddy.ai），
# 凭据独立（同一 binary 家族，--model 的合法值两家菜单不同，换家时先探针）。
MAIN_CLI = ("/Applications/WorkBuddy.app/Contents/Resources/"
            "app.asar.unpacked/cli/bin/codebuddy")
AI_CLI = ("/Applications/WorkBuddy AI.app/Contents/Resources/"
          "app.asar.unpacked/cli/bin/codebuddy")
MCP_CONFIG = os.environ.get(
    "ACP_BRIDGE_MCP_CONFIG", os.path.expanduser("~/.cc-connect/wai-mcp.json")
)
# cwd 必须避开家目录：实测 cwd=~ 时 CLI 自动加载章程 CLAUDE.md，语音会把「回话必带
# Q/A 格式」之类同事规矩带进来，每轮先念一遍问题（2026-10-05 实测踩坑后改项目根）
CWD = os.environ.get("ACP_BRIDGE_CWD", str(PROJECT_ROOT))
HOST = os.environ.get("ACP_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ACP_BRIDGE_PORT", "8647"))

# 思考流整条丢弃（语音不能念出思考过程；与中枢 workbuddy-acp-clean.mjs 同一决定）
DROP_UPDATES = {"agent_thought_chunk"}
# 转发为 hermes.tool.progress 的更新（hermes_live session.py 消费 tool/label/emoji/status 四字段）
TOOL_UPDATES = {"tool_call", "tool_call_update"}


def load_acp_cli() -> str:
    """CLI 二进制：代码默认主版 → config.yaml acp_cli → env ACP_BRIDGE_MODEL 同款三档。
    2026-10-05 高律师定「换回ai版ds」：线上=AI 版（WorkBuddy AI.app）。"""
    cli = MAIN_CLI
    try:
        data = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        if str(data.get("acp_cli") or "").strip():
            cli = str(data["acp_cli"]).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("acp_cli 读取失败（%s），用默认主版", e)
    env = os.environ.get("ACP_BRIDGE_CLI")
    if env:
        cli = env
    return cli


def load_acp_model() -> str:
    """对话模型：代码默认 → config.yaml acp_model → env ACP_BRIDGE_MODEL 覆盖。
    2026-10-05 高律师定「改glm5.3flash模型」（原话），此前为中枢同款 deepseek-v4.1-flash。"""
    model = "deepseek-v4.1-flash"
    try:
        data = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        if str(data.get("acp_model") or "").strip():
            model = str(data["acp_model"]).strip()
    except Exception as e:  # noqa: BLE001 — 读不到用默认，桥不能因它起不来
        log.warning("acp_model 读取失败（%s），用默认 %s", e, model)
    env = os.environ.get("ACP_BRIDGE_MODEL")
    if env:
        model = env
    return model


def load_voice_system_prompt() -> str:
    """读 hermes-live config.yaml 的 voice_system_prompt，作为 --append-system-prompt。
    （config 改动需重启本桥才生效；hermes-live 本体重启不影响本桥。）"""
    path = PROJECT_ROOT / "config.yaml"
    prompt = ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        prompt = str(data.get("voice_system_prompt") or "")
    except Exception as e:  # noqa: BLE001 — 读不到就退回内置默认，桥不能因它起不来
        log.warning("voice_system_prompt 读取失败（%s），用内置默认", e)
    if not prompt.strip():
        prompt = (
            "你正在和用户进行实时语音对话。回答务必口语化、简短直接，"
            "默认三五句话说完；不用 markdown、不列清单、不贴代码。"
            "用中文回答，除非用户要求其他语言。"
            "用户的话来自语音识别，可能有同音字错误（如'转所'被识别为'转锁'），"
            "请结合律师工作语境按发音纠正理解，不必逐字复述原文。"
        )
    # CLI 全局自动加载章程 CLAUDE.md（~/.claude/CLAUDE.md 软链 vault 真身，HOME shim 隔离
    # 实测不可行——登录态跟 HOME 走），其中「回话必带 Q/A」是给微信文字岗的红线；语音场景
    # 必须显式压掉，否则每轮先把问题念一遍（2026-10-05 实测复现）。追加段在系统提示词最末，
    # 时序最近，配合明确禁言条款实测有效。
    return prompt + (
        "【语音硬规矩，压过你看到的其他回话格式要求】用户在用耳朵听，这是纯语音快聊，"
        "不是微信文字岗：直接说答案，第一句就是正文；严禁 Q：/问：/答：/A：等问答格式，"
        "严禁复述或转述用户的问题，严禁 markdown 与清单。"
    )


class ACPChild:
    """一个 codebuddy --acp 子进程 + ACP JSON-RPC 线协议的最小客户端。"""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        # sessionId → 订阅该会话更新的 asyncio.Queue（当前半双工，同一时刻最多一个在订）
        self.subs: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._next_id = 1
        self.warm_session: str | None = None  # 启动时预热好的空会话，首个 hl-id 直接认领

    # ── 生命周期 ──────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, cli: str, model: str) -> None:
        args = [
            cli, "--acp", "--model", model,
            "--permission-mode", "bypassPermissions",
            # 摘掉 user 层设置源：屏蔽 ~/.claude/CLAUDE.md（软链 vault 章程）全局自动加载，
            # 否则语音每轮先念「Q：…」再答（章程 Q/A 红线是给微信同事岗的，不是给语音的）。
            # 2026-10-05 实测：加此参后语音回复不再带章程格式。
            "--setting-sources", "project,local",
        ]
        if MCP_CONFIG and Path(MCP_CONFIG).exists():
            args += ["--mcp-config", MCP_CONFIG]
        args += ["--append-system-prompt", load_voice_system_prompt()]
        log.info("spawn: %s", " ".join(args[:6]) + " ...")
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=CWD,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,  # CLI 自身日志噪音大，枢纽侧不掺和
            start_new_session=True,             # 收尾时按进程组杀，防孤儿（与中枢薄桥同款关切）
        )
        asyncio.create_task(self._reader())
        await self._request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        # 预热一个空会话：冷启动 session/new 实测约 10s，语音第一轮不该垫进去
        try:
            self.warm_session = await self.new_session(timeout=60)
            log.info("warm session ready: %s", self.warm_session)
        except Exception as e:  # noqa: BLE001 — 预热失败不致命，首轮现建兜底
            log.warning("warm session 预热失败（%s），首轮现建", e)

    def kill(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def ensure_alive(self, cli: str, model: str) -> None:
        if self.alive:
            return
        if self.proc is None:
            log.info("首次拉起 CLI 子进程（cli=%s model=%s）",
                     "AI版" if "WorkBuddy AI" in cli else "主版", model)
        else:
            log.warning("CLI 子进程已死（returncode=%s），重拉", self.proc.returncode)
        self.pending.clear()
        self.subs.clear()
        self.warm_session = None
        await self.start(cli, model)

    # ── 线协议 ────────────────────────────────────────────────

    async def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.debug("非 JSON 行: %.120s", line)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(f"ACP error: {msg['error']}"))
                    else:
                        fut.set_result(msg["result"])
            elif msg.get("method") == "session/update":
                params = msg.get("params") or {}
                q = self.subs.get(params.get("sessionId") or "")
                if q is not None:
                    q.put_nowait(params.get("update") or {})
            # 其余 method（session/request_permission 等）在 bypassPermissions 下不应出现，忽略
        log.warning("CLI stdout 关闭（进程退出）")

    async def _request(self, method: str, params: dict, timeout: float = 120) -> dict:
        assert self.proc and self.proc.stdin
        async with self._lock:
            rid = self._next_id
            self._next_id += 1
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.pending[rid] = fut
            line = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                              ensure_ascii=False)
            self.proc.stdin.write(line.encode("utf-8") + b"\n")
            await self.proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=timeout)

    async def new_session(self, timeout: float = 60) -> str:
        res = await self._request("session/new", {"cwd": CWD, "mcpServers": []}, timeout=timeout)
        return res["sessionId"]

    async def prompt_stream(self, session_id: str, text: str):
        """发起一轮 prompt，yield 更新 dict，直到收到 session/prompt 的 RESP。
        取消消费方（客户端断开）→ 自动 session/cancel 对应会话。"""
        q: asyncio.Queue = asyncio.Queue()
        self.subs[session_id] = q
        task = asyncio.create_task(
            self._request("session/prompt",
                          {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                          timeout=600)
        )
        try:
            while True:
                get_task = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({task, get_task}, return_when=asyncio.FIRST_COMPLETED)
                if get_task in done:
                    yield get_task.result()
                    get_task = None
                    continue
                break  # prompt RESP 先回 = 本轮结束
            result = task.result()
            yield {"_stopReason": result.get("stopReason", "end_turn")}
        finally:
            if task and not task.done():
                task.cancel()
                # 语音打断：明确 cancel 该 ACP 会话，防 CLI 继续烧 token（对齐 gateway interrupt）
                try:
                    await self._request("session/cancel", {"sessionId": session_id}, timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            self.subs.pop(session_id, None)


class HeadScrub:
    """剥除章程 Q/A 泄漏的头部回声。

    背景：CLI 全局自动加载章程 CLAUDE.md（软链 vault 真身），「回话必带 Q/A」是给微信
    文字岗的红线，语音场景模型偶尔仍以「Q：<复述问题>A：<正文>」开头（追加提示词的
    禁言条款 2026-10-05 实测大部分压制但不彻底，E2E 仍抽到一次「A：」头）。语音播报
    必须确定性干净，故在桥内缓冲开头若干字符：未见标记即放行；见「Q：/问：」等标记
    则等「A：/答：」出现后剥掉前缀再放行，超长兜底放行防卡死。
    """

    MARKERS = ("Q：", "Q:", "问：")
    ANSWERS = ("A：", "A:", "答：")
    HOLD = 40    # 无任何标记时的放行阈值（字）：语音首句延迟敏感，不宜大
    CAP = 200    # 见标记但迟迟无「答」段的兜底放行阈值

    def __init__(self) -> None:
        self.buf = ""
        self.done = False
        self.stripped = False

    def feed(self, text: str) -> str:
        if self.done:
            return text
        self.buf += text
        if not any(m in self.buf for m in self.MARKERS + self.ANSWERS):
            if len(self.buf) >= self.HOLD:
                self.done = True
                out, self.buf = self.buf, ""
                return out
            return ""
        for m in self.ANSWERS:
            idx = self.buf.find(m)
            if idx >= 0:
                self.done = True
                self.stripped = True
                rest = self.buf[idx + len(m):]
                log.info("剥除 Q/A 头部回声（章程泄漏）: %.80s", self.buf)
                return rest.lstrip("：: \n　")
        if len(self.buf) > self.CAP:
            self.done = True
            out, self.buf = self.buf, ""
            return out
        return ""

    def flush(self) -> str:
        self.done = True
        out, self.buf = self.buf, ""
        return out


class Bridge:
    def __init__(self) -> None:
        self.child = ACPChild()
        self.cli = load_acp_cli()
        self.model = load_acp_model()
        # hl-xxx（hermes-live 自铸会话身份）→ ACP sessionId
        self.sessions: dict[str, str] = {}
        self.turn_lock = asyncio.Lock()  # 半双工：同一时刻只跑一轮模型

    async def _acp_session_for(self, hl_id: str) -> str:
        sid = self.sessions.get(hl_id)
        if sid:
            return sid
        if self.child.warm_session:  # 预热会话只给第一个进门的 hl-id
            sid, self.child.warm_session = self.child.warm_session, None
        else:
            sid = await self.child.new_session()
        self.sessions[hl_id] = sid
        log.info("hl-session %s → acp %s", hl_id, sid)
        return sid

    async def health(self, _request: web.Request) -> web.Response:
        if self.child.alive:
            return web.json_response({"ok": True, "engine": self.model})
        return web.json_response({"ok": False}, status=503)

    async def chat(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad json"}, status=400)
        hl_id = request.headers.get("X-Hermes-Session-Id") or f"hl-anon-{uuid.uuid4().hex[:8]}"
        # 取最后一条 user 消息（hermes_client 每轮只发 system+最新 user，与 gateway 语义一致）
        user_text = ""
        for m in reversed(body.get("messages") or []):
            if m.get("role") == "user":
                user_text = str(m.get("content") or "")
                break
        if not user_text.strip():
            return web.json_response({"error": "no user message"}, status=400)

        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Hermes-Session-Id": hl_id,  # 原样回传（client 每次响应都更新本地保存值）
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)

        def sse(data: dict, event: str | None = None) -> bytes:
            out = b""
            if event:
                out += f"event: {event}\n".encode()
            out += b"data: " + json.dumps(data, ensure_ascii=False).encode() + b"\n\n"
            return out

        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        def chunk(delta: dict, finish: str | None = None) -> dict:
            return {
                "id": cid, "object": "chat.completion.chunk", "created": 0, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        try:
            async with self.turn_lock:
                await self.child.ensure_alive(self.cli, self.model)
                acp_sid = await self._acp_session_for(hl_id)
                await resp.write(sse(chunk({"role": "assistant", "content": ""})))
                scrub = HeadScrub()
                async for upd in self.child.prompt_stream(acp_sid, user_text):
                    kind = upd.get("sessionUpdate")
                    if upd.get("_stopReason"):
                        rest = scrub.flush()
                        if rest:
                            await resp.write(sse(chunk({"content": rest})))
                        await resp.write(sse(chunk({}, finish="stop")))
                        break
                    if kind in DROP_UPDATES:
                        continue
                    if kind in TOOL_UPDATES:
                        title = str(upd.get("title") or upd.get("toolCallId") or "tool")
                        status_map = {"pending": "running", "in_progress": "running",
                                      "completed": "done", "failed": "failed"}
                        await resp.write(sse(
                            {"tool": title, "label": title, "emoji": "🔧",
                             "status": status_map.get(upd.get("status"), "running")},
                            event="hermes.tool.progress"))
                        continue
                    if kind == "agent_message_chunk":
                        text = scrub.feed((upd.get("content") or {}).get("text") or "")
                        if text:
                            await resp.write(sse(chunk({"content": text})))
                await resp.write(b"data: [DONE]\n\n")
        except asyncio.CancelledError:
            # 客户端断开（语音打断/页面刷新）：prompt_stream 的 finally 会发 session/cancel
            raise
        except Exception as e:  # noqa: BLE001 — 线上任何断法都以 error 事件收口
            log.exception("chat turn failed")
            try:
                await resp.write(sse({"message": f"bridge: {e}"}, event="error"))
            except Exception:  # noqa: BLE001
                pass
        await resp.write_eof()
        return resp


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = web.Application()
    bridge = Bridge()

    async def on_start(_app: web.Application) -> None:
        await bridge.child.ensure_alive(bridge.cli, bridge.model)

    async def on_cleanup(_app: web.Application) -> None:
        bridge.child.kill()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/v1/health", bridge.health)
    app.router.add_post("/v1/chat/completions", bridge.chat)
    log.info("acp_bridge 监听 %s:%s（顶替退役 Hermes gateway 的 8647 插槽，引擎=%s）", HOST, PORT, bridge.model)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
