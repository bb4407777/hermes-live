"""M1 服务端回归：模拟浏览器客户端过 WS 全链路（需 8698 已启动）。

场景 A：语音 turn —— 喂 tmp/test16k.wav 帧+尾静音，断言 thinking→asr_final→delta→speaking→音频帧→done→listening
场景 B：文字 turn + 打断 —— 发 text，收到音频后立刻 interrupt，断言 turn 递增、音频停发、回到 listening

共 2 次 LLM 调用（场景 B 提前打断，token 消耗有限）。
用法：.venv/bin/python scripts/ws_regression.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.m0_pipeline import wav_frames_16k  # noqa: E402

URL = "ws://127.0.0.1:8698/ws"
WAV = Path(__file__).resolve().parent.parent / "tmp" / "test16k.wav"
FRAME = 1024


class Log:
    def __init__(self):
        self.msgs: list[dict] = []
        self.audio_turns: list[int] = []
        self.audio_bytes = 0

    def states(self):
        return [(m["state"], m.get("turn")) for m in self.msgs if m["type"] == "state"]

    def types(self):
        return [m["type"] for m in self.msgs]

    def get(self, t):
        return [m for m in self.msgs if m["type"] == t]


async def pump(ws, log: Log, stop: asyncio.Event):
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            data = msg.data
            if data[0] == 0x01:
                log.audio_turns.append(data[1])
                log.audio_bytes += len(data) - 2
                # 模拟客户端：首帧即回执播完（跳过服务端时长等待）
                if log.audio_turns.count(data[1]) == 1:
                    await ws.send_str(json.dumps({"type": "playback_done", "turn": data[1]}))
        elif msg.type == aiohttp.WSMsgType.TEXT:
            m = json.loads(msg.data)
            log.msgs.append(m)
            tag = {k: v for k, v in m.items() if k in ("type", "state", "turn", "text", "finish_reason")}
            print("  ←", tag)
        if stop.is_set():
            return


async def wait_for(log: Log, pred, timeout=120, what=""):
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if pred(log):
            return True
        await asyncio.sleep(0.1)
    print(f"  ✗ 等待超时: {what}")
    return False


def check(cond, what, fails):
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        fails.append(what)


async def main() -> int:
    fails: list[str] = []
    pcm = wav_frames_16k(str(WAV))
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(URL, max_msg_size=4 * 1024 * 1024) as ws:
            log = Log()
            stop = asyncio.Event()
            pumper = asyncio.create_task(pump(ws, log, stop))

            print("== 场景 A：语音 turn ==")
            await ws.send_str(json.dumps({"type": "start"}))
            for i in range(0, len(pcm) - FRAME + 1, FRAME):
                await ws.send_bytes(b"\x01" + pcm[i:i + FRAME])
                await asyncio.sleep(0.002)  # 轻微节流，别一次灌爆
            for _ in range(45):
                await ws.send_bytes(b"\x01" + b"\x00" * FRAME)
                await asyncio.sleep(0.002)

            ok = await wait_for(log, lambda l: l.get("agent_done"), what="agent_done")
            check(ok, "收到 agent_done", fails)
            # 音频与 speaking 态在 agent_done 之后才流完，先等 turn 收口再断言
            ok = await wait_for(log, lambda l: l.states() and l.states()[-1][0] == "listening",
                                timeout=60, what="回到 listening")
            check(ok, "turn 结束回到 listening", fails)
            check(bool(log.get("asr_final")) and len(log.get("asr_final")[0]["text"]) > 0,
                  f"asr_final 非空: {log.get('asr_final') and log.get('asr_final')[0]['text']!r}", fails)
            check(bool(log.get("agent_delta")), "收到 agent_delta", fails)
            check(log.audio_bytes > 24000, f"收到音频 {log.audio_bytes/2/24000:.1f}s", fails)
            check(("thinking" in [s for s, _ in log.states()])
                  and ("speaking" in [s for s, _ in log.states()]),
                  "状态经过 thinking→speaking", fails)

            print("== 场景 B：文字 turn + 打断 ==")
            before_turns = set(log.audio_turns)
            await ws.send_str(json.dumps({"type": "text", "text": "请慢慢讲一个三百字的故事"}))
            ok = await wait_for(log, lambda l: set(l.audio_turns) - before_turns,
                                what="新 turn 音频")
            check(ok, "新 turn 音频开始到达", fails)
            new_turn = max(set(log.audio_turns) - before_turns) if ok else -1
            await ws.send_str(json.dumps({"type": "interrupt"}))
            ok = await wait_for(
                log, lambda l: any(s == "listening" and (t or 0) > new_turn for s, t in l.states()),
                timeout=15, what="打断后 listening(turn+1)")
            check(ok, "打断后回到 listening 且 turn 递增", fails)
            n_before = len(log.audio_turns)
            await asyncio.sleep(2)
            late = [t for t in log.audio_turns[n_before:] if t == new_turn]
            check(len(late) == 0, f"打断后旧 turn 音频停发（迟到帧 {len(late)}）", fails)

            stop.set()
            await ws.close()
            pumper.cancel()

    print(f"\n== 回归{'通过' if not fails else '失败: ' + '; '.join(fails)} ==")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
