"""M0 全链路验证（不经浏览器，直接复用 server.Session 的真实状态机）。

    .venv/bin/python -m cli.m0_pipeline --wav tmp/test16k.wav   # 文件注入（自动补尾静音）
    .venv/bin/python -m cli.m0_pipeline --text "你好"            # 跳过 ASR
    .venv/bin/python -m cli.m0_pipeline --mic                    # 终端麦克风对话（半双工）

--wav/--text 把回复音频写到 tmp/reply.wav；--mic 直接扬声器播放。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
import wave
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.asr import ASR                      # noqa: E402
from server.config import load_config           # noqa: E402
from server.hermes_client import HermesClient   # noqa: E402
from server.session import FRAME_BYTES, Session # noqa: E402
from server.tts import TTSEngine                # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("m0")


def wav_frames_16k(path: str) -> bytes:
    """任意音频文件 → 16k mono PCM16（用 PyAV 解码重采样）。"""
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    out = bytearray()
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            for r in resampler.resample(frame):
                out += r.to_ndarray().tobytes()
        for r in resampler.resample(None):
            out += r.to_ndarray().tobytes()
    return bytes(out)


def write_wav(path: str, pcm: bytes, rate: int) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class Collector:
    """消费 Session outbox：json 打印/记账，音频帧收集或喂扬声器。"""

    def __init__(self, session: Session, play_fn=None):
        self.session = session
        self.play_fn = play_fn
        self.asr_text: str | None = None
        self.reply_text = ""
        self.audio = bytearray()
        self.errors: list[str] = []
        self.done = False
        self._sent_playback_done: set[int] = set()

    async def run(self) -> None:
        while True:
            item = await self.session.outbox.get()
            if item is None:
                return
            kind, payload = item
            if kind == "bin":
                turn, pcm = payload[1], payload[2:]
                self.audio += pcm
                if self.play_fn:
                    self.play_fn(pcm)
                elif turn not in self._sent_playback_done:
                    # 无实际播放器：立即回执 playback_done，免得等时长估算超时
                    self._sent_playback_done.add(turn)
                    await self.session.on_control({"type": "playback_done", "turn": turn})
                continue
            obj = json.loads(payload)
            t = obj.get("type")
            if t == "state":
                logger.info("state → %s (turn %s)", obj["state"], obj["turn"])
            elif t == "asr_final":
                self.asr_text = obj["text"]
                logger.info("🎤 %s", obj["text"])
            elif t == "agent_delta":
                self.reply_text += obj["text"]
            elif t == "agent_done":
                self.done = True
                logger.info("🤖 %s", self.reply_text.strip()[:200])
            elif t == "tool_progress":
                logger.info("🔧 %s %s", obj.get("label") or obj.get("tool"), obj.get("status"))
            elif t == "error":
                self.errors.append(obj["message"])
                logger.error("❌ %s", obj["message"])


async def wait_turn_done(session: Session, timeout: float = 180) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        await asyncio.sleep(0.1)
        task = session.turn_task
        if session.state == "listening" and (task is None or task.done()):
            return
        if session.state == "idle":
            return
    raise TimeoutError("turn 未在期限内完成")


async def run_once(cfg, feed, text: str | None, out_wav: str | None) -> int:
    async with aiohttp.ClientSession() as http:
        hermes = HermesClient(cfg, http)
        if not await hermes.health():
            logger.error("Hermes gateway 不可达：%s", cfg.hermes_base_url)
            return 2
        asr = ASR(cfg)
        if feed is not None:
            logger.info("加载 whisper（首次会下载权重）…")
            await asyncio.to_thread(asr.load)
        session = Session(cfg, asr, TTSEngine(cfg), hermes, asyncio.Queue())
        collector = Collector(session)
        consumer = asyncio.create_task(collector.run())

        await session.on_control({"type": "start"})
        if text is not None:
            await session.on_control({"type": "text", "text": text})
        else:
            for i in range(0, len(feed) - FRAME_BYTES + 1, FRAME_BYTES):
                await session.on_audio(feed[i:i + FRAME_BYTES])
            silence = b"\x00" * FRAME_BYTES
            for _ in range(40):  # 1.28s 尾静音触发段结束
                await session.on_audio(silence)
        await wait_turn_done(session)
        await session.close()
        session.outbox.put_nowait(None)
        await consumer

        ok = collector.done and len(collector.audio) > 0 and not collector.errors
        if feed is not None and not collector.asr_text:
            ok = False
        if out_wav and collector.audio:
            write_wav(out_wav, bytes(collector.audio), cfg.out_rate)
            logger.info("回复音频 → %s（%.1fs）", out_wav,
                        len(collector.audio) / 2 / cfg.out_rate)
        logger.info("M0 结果：asr=%r reply=%d字 audio=%.1fs errors=%s → %s",
                    collector.asr_text, len(collector.reply_text),
                    len(collector.audio) / 2 / cfg.out_rate, collector.errors,
                    "PASS" if ok else "FAIL")
        return 0 if ok else 1


async def run_mic(cfg) -> int:
    import sounddevice as sd

    async with aiohttp.ClientSession() as http:
        hermes = HermesClient(cfg, http)
        if not await hermes.health():
            logger.error("Hermes gateway 不可达")
            return 2
        asr = ASR(cfg)
        await asyncio.to_thread(asr.load)
        session = Session(cfg, asr, TTSEngine(cfg), hermes, asyncio.Queue())

        out_stream = sd.RawOutputStream(samplerate=cfg.out_rate, channels=1, dtype="int16")
        out_stream.start()

        def play(pcm: bytes) -> None:
            out_stream.write(pcm)  # 阻塞写在 to_thread 里跑

        loop = asyncio.get_running_loop()
        mic_q: asyncio.Queue = asyncio.Queue()

        def on_mic(indata, frames, t, status) -> None:
            loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

        collector = Collector(session, play_fn=None)

        async def play_consumer() -> None:
            """bin 帧 → 阻塞播放（to_thread），播完回执 playback_done。"""
            while True:
                item = await session.outbox.get()
                if item is None:
                    return
                kind, payload = item
                if kind == "bin":
                    await asyncio.to_thread(play, payload[2:])
                    collector.audio += payload[2:]
                    # 队列见底 = 本 turn 播完
                    if session.outbox.empty():
                        await session.on_control(
                            {"type": "playback_done", "turn": payload[1]})
                else:
                    obj = json.loads(payload)
                    if obj.get("type") == "asr_final":
                        logger.info("🎤 %s", obj["text"])
                    elif obj.get("type") == "agent_done":
                        pass
                    elif obj.get("type") == "state":
                        logger.info("state → %s", obj["state"])
                    elif obj.get("type") == "error":
                        logger.error("❌ %s", obj["message"])

        consumer = asyncio.create_task(play_consumer())
        in_stream = sd.RawInputStream(samplerate=cfg.in_rate, channels=1, dtype="int16",
                                      blocksize=512, callback=on_mic)
        in_stream.start()
        await session.on_control({"type": "start"})
        logger.info("🎙 说话吧（半双工：播音时不收麦，Ctrl+C 退出）")
        try:
            while True:
                frame = await mic_q.get()
                if session.state == "speaking":
                    continue  # CLI 无 AEC，半双工防自听（对齐 gpt-live mac 路径）
                await session.on_audio(frame)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            in_stream.stop(); out_stream.stop()
            await session.close()
            session.outbox.put_nowait(None)
            with contextlib.suppress(Exception):
                await consumer
        return 0


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--wav")
    g.add_argument("--text")
    g.add_argument("--mic", action="store_true")
    p.add_argument("--out", default="tmp/reply.wav")
    p.add_argument("--config", default=None)
    args = p.parse_args()
    cfg = load_config(args.config)

    if args.mic:
        code = asyncio.run(run_mic(cfg))
    elif args.text:
        code = asyncio.run(run_once(cfg, feed=None, text=args.text, out_wav=args.out))
    else:
        pcm = wav_frames_16k(args.wav)
        logger.info("输入音频 %.1fs", len(pcm) / 2 / 16000)
        code = asyncio.run(run_once(cfg, feed=pcm, text=None, out_wav=args.out))
    sys.exit(code)


if __name__ == "__main__":
    main()
