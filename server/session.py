"""每 WS 连接一个 Session：状态机 + 任务编排 + barge-in。

状态机（命名沿用 gpt-live）：idle → listening → thinking → speaking → listening
- listening：VAD 分段；段结束 → thinking 开新 turn
- thinking：ASR + Hermes SSE；忽略上行音频（防噪声误取消），只认手动 interrupt
- speaking：全双工，VAD 走高门槛打断档；音频发完后等 playback_done（或按时长估算超时）
barge-in：turn+1（在途旧音频帧被客户端按 turn 字节丢弃）→ 取消 turn 任务
（内部级联取消 TTS 与 Hermes SSE，断开 SSE 即触发网关 agent.interrupt，不白烧 token）
→ pre-roll 灌回分段器无缝进入新一轮。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import protocol
from .asr import ASR
from .config import Config
from .hermes_client import HermesClient
from .metrics import TurnMetrics
from .sentencer import SentenceAssembler
from .tts import TTSEngine
from .vad import BargeDetector, Segmenter, new_vad

logger = logging.getLogger(__name__)

FRAME_BYTES = 1024  # 512 样本 * 2 字节
ECHO_GUARD_S = 0.3  # speaking→listening 后的余声忽略窗


class Session:
    def __init__(self, cfg: Config, asr: ASR, tts: TTSEngine, hermes: HermesClient,
                 outbox: asyncio.Queue):
        self.cfg = cfg
        self.asr = asr
        self.tts = tts
        self.hermes = hermes
        self.outbox = outbox  # ("json", str) | ("bin", bytes)，由 ws 写协程串行发送

        self.state = "idle"
        self.turn = 0
        self.turn_task: asyncio.Task | None = None
        self.segmenter = Segmenter(cfg, new_vad())
        self.barge = BargeDetector(cfg, new_vad())
        self.listen_ignore_until = 0.0   # 回声忽略窗
        self.barge_cooldown_until = 0.0
        self.playback_done: dict[int, asyncio.Event] = {}
        self.closed = False

    # ---------- 出站 ----------

    def send_json(self, type_: str, **kw) -> None:
        if not self.closed:
            self.outbox.put_nowait(("json", protocol.msg(type_, **kw)))

    def send_audio(self, turn: int, pcm: bytes) -> None:
        if not self.closed:
            self.outbox.put_nowait(("bin", protocol.pack_audio_down(turn, pcm)))

    def _set_state(self, state: str) -> None:
        self.state = state
        self.send_json("state", state=state, turn=self.turn)

    # ---------- 入站 ----------

    async def on_audio(self, pcm: bytes) -> None:
        if len(pcm) != FRAME_BYTES:
            return
        if self.state == "listening":
            if time.monotonic() < self.listen_ignore_until:
                return
            ev = self.segmenter.feed(pcm)
            if ev and ev[0] == "end" and ev[1]:
                self._begin_turn(pcm_utt=ev[1].pcm)
        elif self.state == "speaking":
            if time.monotonic() < self.barge_cooldown_until:
                return
            pre = self.barge.feed(pcm)
            if pre is not None:
                await self._barge_in(pre)
        # thinking / idle：丢弃

    async def on_control(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "start":
            if self.state == "idle":
                self._enter_listening()
        elif t == "stop":
            await self._cancel_turn()
            self.segmenter.reset()
            self.barge.reset()
            self._set_state("idle")
        elif t == "interrupt":
            if self.state in ("thinking", "speaking"):
                await self._barge_in(None)
        elif t == "text":
            text = (obj.get("text") or "").strip()
            if text:
                await self._cancel_turn()
                self._begin_turn(text=text)
        elif t == "new_session":
            await self._cancel_turn()
            self.hermes.new_session()
            self.send_json("hello", session_id=None, voice=self.cfg.tts_voice,
                           asr_model=self.cfg.asr_model, note="新会话")
            if self.state != "idle":
                self._enter_listening()
        elif t == "playback_done":
            ev = self.playback_done.get(int(obj.get("turn", -1)))
            if ev:
                ev.set()
        elif t == "set_config":
            if v := obj.get("voice"):
                self.cfg.tts_voice = str(v)
            if r := obj.get("tts_rate"):
                self.cfg.tts_rate = str(r)
            self.send_json("hello", session_id=self.hermes.session_id,
                           voice=self.cfg.tts_voice, asr_model=self.cfg.asr_model)

    async def close(self) -> None:
        self.closed = True
        await self._cancel_turn()

    # ---------- 状态迁移 ----------

    def _enter_listening(self, echo_guard: bool = False) -> None:
        self.segmenter.reset()
        if echo_guard:
            self.listen_ignore_until = time.monotonic() + ECHO_GUARD_S
        self._set_state("listening")

    def _begin_turn(self, pcm_utt: bytes | None = None, text: str | None = None) -> None:
        self.turn += 1
        self._set_state("thinking")
        self.turn_task = asyncio.create_task(
            self._run_turn(self.turn, pcm_utt=pcm_utt, text=text))

    async def _cancel_turn(self) -> None:
        task, self.turn_task = self.turn_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _barge_in(self, preroll_pcm: bytes | None) -> None:
        self.turn += 1  # 立即作废在途旧音频帧
        self.barge_cooldown_until = time.monotonic() + self.cfg.barge_cooldown_ms / 1000
        await self._cancel_turn()
        self.barge.reset()
        self._enter_listening()
        if preroll_pcm:
            # 把打断者已说出的开头灌回分段器
            for i in range(0, len(preroll_pcm) - FRAME_BYTES + 1, FRAME_BYTES):
                ev = self.segmenter.feed(preroll_pcm[i:i + FRAME_BYTES])
                if ev and ev[0] == "end" and ev[1]:
                    self._begin_turn(pcm_utt=ev[1].pcm)
                    return

    # ---------- turn 主流程 ----------

    async def _run_turn(self, t: int, pcm_utt: bytes | None, text: str | None) -> None:
        m = TurnMetrics(turn=t)
        queue: asyncio.Queue = asyncio.Queue()
        tts_task = asyncio.create_task(self._tts_consumer(t, queue, m))
        try:
            if text is None:
                text = await asyncio.to_thread(self.asr.transcribe, pcm_utt)
                m.mark("asr_done")
                m.asr_text = text
                if not text:
                    tts_task.cancel()
                    if self.turn == t:
                        self._enter_listening()
                    return
                self.send_json("asr_final", turn=t, text=text)
            else:
                m.mark("asr_done")
                m.asr_text = text

            assembler = SentenceAssembler(self.cfg.sentence_max_buffer,
                                          self.cfg.sentence_first_min)
            finish_reason = None
            async for ev in self.hermes.chat_stream(text):
                if ev.kind == "delta":
                    m.mark("first_delta")
                    self.send_json("agent_delta", turn=t, text=ev.text)
                    for s in assembler.feed(ev.text):
                        m.mark("first_sentence")
                        queue.put_nowait(s)
                elif ev.kind == "tool":
                    if self.cfg.show_tool_progress:
                        tool = ev.tool or {}
                        self.send_json("tool_progress", turn=t,
                                       tool=tool.get("tool"), label=tool.get("label"),
                                       emoji=tool.get("emoji"), status=tool.get("status"))
                elif ev.kind == "done":
                    finish_reason = ev.finish_reason
            for s in assembler.flush():
                m.mark("first_sentence")
                queue.put_nowait(s)
            queue.put_nowait(None)  # 收口哨兵
            self.send_json("agent_done", turn=t, finish_reason=finish_reason)
            await tts_task  # 等音频发完 + 客户端播完
            m.mark("done")
            m.log()
            if self.turn == t:
                self._enter_listening(echo_guard=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("turn %s failed", t)
            self.send_json("error", turn=t, message=f"{type(exc).__name__}: {exc}")
            if self.turn == t:
                self._enter_listening()
        finally:
            if not tts_task.done():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tts_task
            self.playback_done.pop(t, None)

    # ---------- TTS 流水线 ----------

    async def _tts_consumer(self, t: int, sentences: asyncio.Queue, m: TurnMetrics) -> None:
        """句子队列 → edge-tts（lookahead 预合成）→ 按序下发音频帧。"""
        synth_q: asyncio.Queue = asyncio.Queue(maxsize=max(1, self.cfg.tts_lookahead))
        pcm_sent = 0
        first_send: float | None = None
        self.playback_done[t] = asyncio.Event()

        async def synth_to_queue(sentence: str, chunks: asyncio.Queue) -> None:
            try:
                yielded = False
                for attempt in (1, 2):
                    try:
                        # aclosing：取消/异常时显式关掉 edge-tts 流，防挂起任务告警
                        async with contextlib.aclosing(self.tts.synth_stream(sentence)) as gen:
                            async for pcm in gen:
                                yielded = True
                                chunks.put_nowait(pcm)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # 半句失败不重试（音频会跳段）；整句未出声则单次重试
                        if yielded or attempt == 2:
                            raise
                        logger.warning("edge-tts 失败重试一次: %s", exc)
            finally:
                chunks.put_nowait(None)

        async def prefetcher() -> None:
            while True:
                s = await sentences.get()
                if s is None:
                    await synth_q.put(None)
                    return
                chunks: asyncio.Queue = asyncio.Queue()
                task = asyncio.create_task(synth_to_queue(s, chunks))
                await synth_q.put((s, chunks, task))  # maxsize 限制预合成并发

        pf = asyncio.create_task(prefetcher())
        inflight: asyncio.Task | None = None
        try:
            while True:
                item = await synth_q.get()
                if item is None:
                    break
                sentence, chunks, inflight = item
                self.send_json("tts_sentence", turn=t, text=sentence)
                while True:
                    pcm = await chunks.get()
                    if pcm is None:
                        break
                    if first_send is None:
                        first_send = time.monotonic()
                        m.mark("first_audio")
                        if self.turn == t:
                            self._set_state("speaking")
                            self.barge.reset()
                    pcm_sent += len(pcm)
                    self.send_audio(t, pcm)
                await inflight  # 让合成异常浮出来
                inflight = None
            # 音频全部发出（客户端缓冲播放中）：等 playback_done，超时按时长估算兜底
            if first_send is not None:
                dur = pcm_sent / 2 / self.cfg.out_rate
                remain = max(0.5, dur - (time.monotonic() - first_send) + 0.4)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.playback_done[t].wait(), timeout=remain)
        finally:
            pf.cancel()
            if inflight and not inflight.done():
                inflight.cancel()
            # 清掉 synth_q 里已启动的预合成任务
            while not synth_q.empty():
                left = synth_q.get_nowait()
                if left:
                    left[2].cancel()
