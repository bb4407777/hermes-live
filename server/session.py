"""每 WS 连接一个 Session：状态机 + 任务编排 + barge-in。

状态机（命名沿用 gpt-live）：idle → listening → thinking → speaking → listening
- listening：VAD 分段；段结束 → thinking 开新 turn
- thinking：ASR + Hermes SSE；忽略上行音频（防噪声误取消），只认手动 interrupt
- speaking：半双工（用户 2026-08-11 定：排队/全双工方案常出 bug，先不跑 VAD 打断），
  上行音频丢弃；音频发完后等 playback_done（tts_end 已下发，等待窗按时长估算）
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
from .asr import ASR, SherpaStreamingASR, DoubaoStreamingASR
from .config import Config
from .hermes_client import HermesClient
from .metrics import TurnMetrics
from .qwen_asr import QwenSidecarASR
from .sentencer import SentenceAssembler
from .tts import TTSEngine
from .vad import BargeDetector, Segmenter, new_vad

logger = logging.getLogger(__name__)

FRAME_BYTES = 1024  # 512 样本 * 2 字节
ECHO_GUARD_S = 0.3  # speaking→listening 后的余声忽略窗
TTS_ECHO_WINDOW_S = 30.0  # 最近 N 秒内说过的句子参与文本回声比对
TTS_ECHO_RATIO = 0.55     # SequenceMatcher ratio 超过此值判定为回声


class Session:
    def __init__(self, cfg: Config, asr: ASR, tts: TTSEngine, hermes: HermesClient,
                 outbox: asyncio.Queue,
                 asr_stream: SherpaStreamingASR | None = None,
                 doubao_asr: DoubaoStreamingASR | None = None,
                 qwen_asr: QwenSidecarASR | None = None):
        self.cfg = cfg
        self.asr = asr
        self.asr_stream = asr_stream   # 流式后端（sherpa）；None 时回退 batch ASR
        self.doubao_asr = doubao_asr   # 豆包云端流式；优先级最高
        self.qwen_asr = qwen_asr       # qwen3-asr 本地 sidecar 批量（weSaw 同款）
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
        self._last_partial: str = ""   # 流式 ASR 上一帧的中间文本（防重复发送）
        self.ptt = False               # 按住说话模式：listening 态不跑 VAD，等 utterance_end 收口
        self._ptt_buf: list[bytes] = []  # PTT 期间攒的帧（batch ASR 兜底用）
        self.closed = False
        self._recent_tts_texts: list[tuple[float, str]] = []  # (timestamp, sentence) 回声过滤缓冲

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

    # ---------- 文本回声过滤 ----------

    def _record_tts_sentence(self, sentence: str) -> None:
        """记录刚发出的 TTS 句子，供 ASR 回声比对。"""
        now = time.monotonic()
        self._recent_tts_texts.append((now, sentence))
        # 只保留窗口内的条目
        cutoff = now - TTS_ECHO_WINDOW_S
        self._recent_tts_texts = [(t, s) for t, s in self._recent_tts_texts if t > cutoff]

    def _is_tts_echo(self, asr_text: str) -> bool:
        """ASR 结果与近期 TTS 文本相似度过高 → 判定为回声，丢弃。"""
        import difflib
        now = time.monotonic()
        for ts, tts in self._recent_tts_texts:
            if now - ts > TTS_ECHO_WINDOW_S:
                continue
            ratio = difflib.SequenceMatcher(None, asr_text, tts).ratio()
            if ratio >= TTS_ECHO_RATIO:
                logger.info("文本回声过滤: ASR=%r ≈ TTS=%r (ratio=%.2f)", asr_text, tts, ratio)
                return True
        return False

    # ---------- 入站 ----------

    def _send_partial(self, partial: str) -> None:
        if partial and partial != self._last_partial:
            self._last_partial = partial
            self.send_json("asr_partial", text=partial)

    async def on_audio(self, pcm: bytes) -> None:
        if len(pcm) != FRAME_BYTES:
            return
        # 出站已死（on_conn_lost 打的标记）就别再起话轮：send_json/send_audio 已被掐掉，
        # 这里却仍会照常 ASR + 打网关，为一个永远收不到回音的连接白烧 token，
        # 要等 heartbeat 把这条半开连接判死才停（最多 ~30s，期间每句话一轮）。
        if self.closed:
            return
        if self.state == "listening":
            if time.monotonic() < self.listen_ignore_until:
                return
            # sherpa 流式：本地模型，静音期逐帧喂着无害，中间结果实时显示
            if self.doubao_asr is None and self.asr_stream is not None:
                self._send_partial(self.asr_stream.feed(pcm))
            if self.ptt:
                # PTT：不跑 VAD 分段，攒帧等 utterance_end 显式收口；
                # doubao 会话已在 _enter_listening 建好（按住即说，不会闲置挂连）
                if self.doubao_asr is not None:
                    self._send_partial(self.doubao_asr.feed(pcm))
                self._ptt_buf.append(pcm)
                return
            ev = self.segmenter.feed(pcm)
            if self.doubao_asr is not None:
                # 懒建连：真正说话（VAD start）才开豆包会话——闲置挂连约 40s
                # 会被豆包远端掐流（GrpcError: the stream is done），此后整轮丢话
                if ev and ev[0] == "start":
                    self.doubao_asr.reset()
                    self.doubao_asr.start_turn()
                    for f in self.segmenter.buf:   # pre-roll + 触发帧一次性补喂
                        self.doubao_asr.feed(f)
                elif self.segmenter.in_speech:
                    self._send_partial(self.doubao_asr.feed(pcm))
            if ev and ev[0] == "end" and ev[1]:
                # doubao：VAD end 时已有足量帧，finish() 在 _run_turn 里等最终结果
                if self.doubao_asr is not None:
                    self.doubao_asr.feed(b"\x00" * FRAME_BYTES)  # 补一帧静音促 flush
                self._begin_turn(pcm_utt=ev[1].pcm)
            elif (ev is None and self.asr_stream is not None
                  and self.segmenter.in_speech
                  and self.asr_stream.is_endpoint()):
                # sherpa 自己的 endpoint_detection 比 silero 静音计时更快感知到段尾；
                # 在 silero 还没触发 end 时提前收口，减少约 400ms 等待。
                # segmenter.reset() 先清状态，再取 sherpa 累积的音频。
                pcm_utt = b"".join(self.segmenter.buf)
                self.segmenter.reset()
                if len(pcm_utt) >= FRAME_BYTES * (self.cfg.vad_min_utterance_ms // 32):
                    self._begin_turn(pcm_utt=pcm_utt)
        elif self.state == "speaking":
            pass  # 半双工：客户端 speaking/thinking 态不发帧，服务端不需要处理
        # thinking / idle：丢弃

    async def on_control(self, obj: dict) -> None:
        t = obj.get("type")
        if t == "start":
            self.ptt = bool(obj.get("ptt"))
            if self.state == "idle":
                self._enter_listening()
        elif t == "utterance_end":
            # PTT 松手：攒的帧整段开 turn（太短当作误触丢弃）
            if self.state == "listening":
                pcm = b"".join(self._ptt_buf)
                self._ptt_buf = []
                if len(pcm) >= FRAME_BYTES * 10:  # ≥320ms
                    self._begin_turn(pcm_utt=pcm)
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
            new_id = self.hermes.new_session()
            self.turn += 1   # 作废在途旧音频帧：客户端否则会继续放被打断那一轮的残留
            self.playback_done.clear()
            self._recent_tts_texts.clear()
            self.send_json("hello", session_id=new_id, voice=self.cfg.tts_voice,
                           asr_model=self.asr_label(), note="新会话")
            if self.state != "idle":
                self._enter_listening()
        elif t == "playback_done":
            pt = int(obj.get("turn", -1))
            ev = self.playback_done.get(pt)
            # 这条日志是「客户端到底播没播」唯一的观测点：静音类 bug 里服务端
            # 一切正常（音频照发、状态照回 listening），只有这里会缺席。
            logger.info("playback_done turn=%s%s", pt, "" if ev else "（无在途 turn，忽略）")
            if ev:
                ev.set()
        elif t == "set_config":
            if v := obj.get("voice"):
                self.cfg.tts_voice = str(v)
            if r := obj.get("tts_rate"):
                self.cfg.tts_rate = str(r)
            self.send_json("hello", session_id=self.hermes.session_id,
                           voice=self.cfg.tts_voice, asr_model=self.asr_label())
        elif t == "restart":
            import os
            import sys
            logger.info("收到 WS 重启请求，1 秒后重启...")
            self.send_json("restarting")

            async def _do_restart() -> None:
                await asyncio.sleep(1)
                logger.info("执行重启：%s %s", sys.executable, sys.argv)
                os.execv(sys.executable, [sys.executable, "-m", "server.main"])

            asyncio.create_task(_do_restart())

    async def close(self) -> None:
        self.closed = True
        await self._cancel_turn()
        if self.doubao_asr is not None:
            self.doubao_asr.reset()   # cancel 在途豆包会话任务，防连接关闭后泄漏

    def on_conn_lost(self) -> None:
        """出站写失败时由 ws_writer 回告：停止出站 + 打断在途 turn。

        不这么做的话 turn 协程会卡在等一个永不再来的 playback_done，
        同时后续音频帧继续往无界 outbox 里堆。
        """
        if self.closed:
            return
        self.closed = True
        asyncio.create_task(self._cancel_turn())

    # ---------- 状态迁移 ----------

    def _enter_listening(self, echo_guard: bool = False) -> None:
        self.segmenter.reset()
        self._last_partial = ""
        self._ptt_buf = []
        if self.doubao_asr is not None:
            self.doubao_asr.reset()
            if self.ptt:
                # PTT 无 VAD start 事件，只能进监听时建连（按住才录，不会闲置挂连）；
                # VAD 模式懒建连，见 on_audio 的 "start" 分支
                self.doubao_asr.start_turn()
        if self.asr_stream is not None:
            self.asr_stream.reset()
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

    def asr_label(self) -> str:
        """本次连接实际会用到哪一档 ASR——hello/health 回显给客户端看的就是它。

        不能直接回显 `cfg.asr_model`：那只是 faster-whisper 的档位名，线上跑豆包时
        网页状态条会显示 "ASR large-v3"，按它排查会走错方向。分支顺序与 _run_turn 一致。
        """
        if self.doubao_asr is not None:
            return "doubao"
        if self.asr_stream is not None:
            return "sherpa-paraformer"
        if self.qwen_asr is not None and self.qwen_asr.available:
            return "qwen3-asr-1.7b"
        return self.asr.backend_label or self.cfg.asr_model

    async def _run_turn(self, t: int, pcm_utt: bytes | None, text: str | None) -> None:
        m = TurnMetrics(turn=t)
        queue: asyncio.Queue = asyncio.Queue()
        tts_task = asyncio.create_task(self._tts_consumer(t, queue, m))
        try:
            if text is None:
                if self.doubao_asr is not None:
                    # 豆包云端流式路径：finish() 等待最终结果
                    text = await self.doubao_asr.finish()
                    self.doubao_asr.reset()
                elif self.asr_stream is not None:
                    # sherpa 流式路径：VAD 触发时累积结果已就位，get_result() ≈ 0 延迟
                    text = self.asr_stream.get_result()
                    self.asr_stream.reset()
                elif self.qwen_asr is not None and self.qwen_asr.available:
                    # qwen3-asr 批量路径：整段转写（无实时字幕）；sidecar 是 MPS 推理进程，
                    # to_thread 包裹防阻塞事件循环
                    text = await asyncio.to_thread(self.qwen_asr.transcribe, pcm_utt)
                else:
                    # 批量兜底：约 1s 转写延迟
                    text = await asyncio.to_thread(self.asr.transcribe, pcm_utt)
                m.mark("asr_done")
                m.asr_text = text
                if not text:
                    tts_task.cancel()
                    if self.turn == t:
                        self._enter_listening()
                    return
                # 文本回声过滤：ASR 结果与近期 TTS 高度相似 → 自己的声音录进去了，丢弃
                if self._is_tts_echo(text):
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
            # 流式朗读：SSE 边到边分句、逐句送 TTS，不等整段生成完（高律师 2026-08-10 定）
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
                    # 工具间隙缓冲语音：模型跑工具时不出 delta，若本 turn 还没开过口，
                    # 插一句缓冲句，避免语音断档显得卡（高律师 2026-08-13 定）
                    if self.cfg.tool_buffer_text and m.first_audio is None:
                        m.mark("first_sentence")
                        queue.put_nowait(self.cfg.tool_buffer_text)
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
        last_send = 0.0
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
        ended_sent = False
        try:
            while True:
                item = await synth_q.get()
                if item is None:
                    break
                sentence, chunks, inflight = item
                self.send_json("tts_sentence", turn=t, text=sentence)
                self._record_tts_sentence(sentence)  # 存入回声缓冲，供 ASR 比对
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
                    last_send = time.monotonic()
                await inflight  # 让合成异常浮出来
                inflight = None
            # 音频全部发出（客户端缓冲播放中）：显式告知"本 turn 音频已发完"，
            # 客户端只在收到 tts_end 且缓冲排空后才报 playback_done（不再靠欠载猜测，
            # 句间断流 >800ms 不再被误判为播完）。
            if first_send is not None:
                self.send_json("tts_end", turn=t)
                ended_sent = True
                dur = pcm_sent / 2 / self.cfg.out_rate
                # 客户端播完的时刻：音频是边生成边下发的，慢生成时 first_send+dur 会大幅
                # 低估（模型想了 40s、只出 20s 语音 → 20s 就超时收口，喇叭其实还在播，
                # 于是自己的 TTS 被录进下一轮）。取两个下界的较大者：
                #   first_send + dur  —— 生成比实时快时成立（末尾还有整段缓冲）
                #   last_send         —— 生成比实时慢时成立（末尾只剩预灌水位那点）
                # 再加余量覆盖客户端抖动与网络。
                est_end = max(first_send + dur, last_send)
                remain = est_end - time.monotonic() + self.cfg.playback_grace_ms / 1000
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.playback_done[t].wait(), timeout=max(0.5, remain))
        finally:
            pf.cancel()
            if inflight and not inflight.done():
                inflight.cancel()
            # 清掉 synth_q 里已启动的预合成任务
            while not synth_q.empty():
                left = synth_q.get_nowait()
                if left:
                    left[2].cancel()
            # 合成中途抛异常（edge-tts 重试后仍失败）时上面那条 tts_end 发不出去，
            # 而客户端的收口门只认它：门不开 → 已排队的尾音永远不会上报播完，
            # 服务端却按异常路径回了 listening 并放开麦克风 → AI 把自己念的尾巴
            # 录进下一轮。补发一条让客户端干净收口（turn 已作废的会被按 turn 忽略）。
            if first_send is not None and not ended_sent:
                self.send_json("tts_end", turn=t)
