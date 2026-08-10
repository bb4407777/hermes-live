"""ASR 后端：doubaoime（豆包逆向，云端流式）> sherpa-onnx 流式 > whisper 批量兜底。

backend 实测（M1 Pro，2.9s 中文测试音频，2026-08-09）：
  doubaoime（豆包输入法 WebSocket）        —— 云端，中文极准，实时中间结果，RTF 受网络影响
  sherpa-onnx streaming paraformer (CPU)  —— RTF≈0.05，边说边出字，VAD 触发即取结果（≈0延迟）
  whispercpp (turbo q5_0, Metal)          —— RTF≈0.35，一次性转写，VAD 后额外等 1s
  faster-whisper large-v3-turbo int8      —— RTF≈1.2，太慢，仅最终兜底

架构：
  DoubaoStreamingASR  —— 流式；每话轮建一个 WebSocket，逐帧喂 PCM，服务端返回中间/最终结果。
                          VAD 触发 end 时调 finish() 拿最终文本。需网络，凭据自动注册缓存。
  SherpaStreamingASR  —— 流式；由 Session.on_audio 在 listening 态逐帧 feed；
                          VAD 触发 end 时调 get_result()（同步，近似零等待）。
  ASR                 —— 批量；仅 --text CLI 路径或 sherpa 不可用时使用。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import numpy as np

from .config import Config

logger = logging.getLogger(__name__)

# whisper 家族幻觉关键词（命中即丢整段）
_HALLUCINATIONS = (
    "明镜与点点", "请不吝点赞", "不吝点赞",
    "谢谢观看", "感谢观看", "请订阅", "字幕由",
    "谢谢大家", "谢谢收看", "版权所有", "纯音乐", "请打开字幕",
)


def _is_hallucination(text: str) -> bool:
    for h in _HALLUCINATIONS:
        if h in text:
            return True
    return False


# ---------------------------------------------------------------------------
# 流式 ASR（豆包输入法逆向，云端）
# ---------------------------------------------------------------------------

class DoubaoStreamingASR:
    """豆包输入法 WebSocket ASR：每话轮建一个会话，逐帧喂 PCM16，服务端返回中间/最终结果。

    用法：
        asr = DoubaoStreamingASR(cfg)
        ok = asr.load()                # 检查依赖（只检查，不建连接）
        # —— listening 阶段 ——
        asr.start_turn()               # 开始一个新话轮，建 WebSocket 连接
        asr.feed(pcm16_bytes)          # 每帧调用（1024 字节 = 512 样本 @16k）
        # —— VAD 触发 end ——
        partial = asr.get_partial()    # 取当前中间文本（用于实时字幕）
        text = asr.finish()            # 发 LAST 帧 → 等待最终结果
        asr.reset()                    # 清理，准备下一轮

    注意：feed()/finish() 都是同步接口，内部用 asyncio 事件队列桥接 WebSocket。
    由于 session.py 在 asyncio 事件循环里调用这些方法，内部用 asyncio.Queue 与
    后台 WebSocket 协程通信，不阻塞事件循环。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._available = False
        self._config = None           # doubaoime_asr.ASRConfig 实例
        # 每话轮状态
        self._pcm_queue: asyncio.Queue | None = None
        self._result_future: asyncio.Future | None = None
        self._partial: str = ""
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def load(self) -> bool:
        try:
            from doubaoime_asr import ASRConfig
            self._config = ASRConfig(
                credential_path=self.cfg.asr_doubao_credential_path,
            )
            self._available = True
            logger.info("doubaoime-asr ready（豆包云端 ASR）")
            return True
        except ImportError:
            logger.info("doubaoime_asr 未安装，跳过豆包 ASR")
            return False
        except Exception as e:
            logger.warning("doubaoime-asr 初始化失败：%s", e)
            return False

    def start_turn(self) -> None:
        """开始新话轮：初始化队列，启动后台 WebSocket 任务。"""
        if not self._available:
            return
        loop = asyncio.get_event_loop()
        self._loop = loop
        self._pcm_queue = asyncio.Queue()
        self._result_future = loop.create_future()
        self._partial = ""
        logger.info("doubao start_turn: launching background session task")
        self._task = loop.create_task(self._run_session())

    async def _run_session(self) -> None:
        from doubaoime_asr import ASRConfig
        from doubaoime_asr.asr import ResponseType
        from doubaoime_asr import transcribe_realtime

        logger.info("doubao session starting...")

        async def pcm_gen():
            while True:
                chunk = await self._pcm_queue.get()
                if chunk is None:
                    logger.debug("doubao pcm_gen: got None, ending stream")
                    return
                yield chunk

        final_text = ""
        try:
            logger.info("doubao transcribe_realtime() starting...")
            async for resp in transcribe_realtime(pcm_gen(), config=self._config):
                if resp.type.name == "INTERIM_RESULT":
                    self._partial = resp.text
                    logger.debug("doubao interim: %r", resp.text[:50])
                elif resp.type.name == "FINAL_RESULT":
                    final_text = resp.text
                    logger.info("doubao final: %r", final_text[:80])
                elif resp.type.name == "ERROR":
                    logger.warning("doubao ASR 错误：%s", resp.error_msg)
                    break
        except Exception as e:
            logger.warning("doubao ASR 会话异常：%s", e, exc_info=True)
        finally:
            if self._result_future and not self._result_future.done():
                self._result_future.set_result(final_text)
            logger.info("doubao session ended, result=%r", final_text[:80] if final_text else "")

    def feed(self, pcm16: bytes) -> str:
        """喂一帧 PCM16（1024 字节）。返回当前中间文本（可能为空）。"""
        if not self._available or self._pcm_queue is None:
            return ""
        self._pcm_queue.put_nowait(pcm16)
        return self._partial

    async def finish(self) -> str:
        """VAD 触发 end 后调用：发结束信号，等待最终识别结果。"""
        if not self._available or self._pcm_queue is None:
            return ""
        self._pcm_queue.put_nowait(None)   # 关闭音频流
        if self._result_future:
            try:
                text = await asyncio.wait_for(self._result_future, timeout=8.0)
                return text.strip()
            except asyncio.TimeoutError:
                logger.warning("doubao ASR 超时，返回中间结果：%r", self._partial)
                return self._partial.strip()
        return ""

    def get_partial(self) -> str:
        return self._partial

    def reset(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._pcm_queue = None
        self._result_future = None
        self._partial = ""

    @property
    def available(self) -> bool:
        return self._available


# ---------------------------------------------------------------------------
# 流式 ASR（sherpa-onnx streaming paraformer）
# ---------------------------------------------------------------------------

class SherpaStreamingASR:
    """sherpa-onnx 流式识别：边说边转，VAD 触发即取结果。

    用法：
        asr_stream = SherpaStreamingASR(cfg)
        ok = asr_stream.load()          # 加载模型（首次约 0.7s）
        # —— listening 阶段 ——
        asr_stream.feed(pcm16_bytes)    # 每帧调用（同步，RTF≈0.05，约5ms/100ms帧）
        # —— VAD 触发 end ——
        text = asr_stream.get_result()  # flush + 取结果
        asr_stream.reset()              # 准备下一轮
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._recognizer = None
        self._stream = None

    def load(self) -> bool:
        """尝试初始化。成功返回 True，依赖缺失或模型不存在返回 False。"""
        if self._recognizer is not None:
            return True
        model_dir = Path(self.cfg.asr_sherpa_model_dir)
        enc = model_dir / "encoder.int8.onnx"
        dec = model_dir / "decoder.int8.onnx"
        tok = model_dir / "tokens.txt"
        if not (enc.exists() and dec.exists() and tok.exists()):
            logger.info("sherpa-onnx 模型不存在（%s），跳过", model_dir)
            return False
        try:
            import sherpa_onnx
        except ImportError:
            logger.info("sherpa-onnx 未安装，跳过")
            return False
        logger.info("loading sherpa-onnx streaming paraformer ...")
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
            encoder=str(enc),
            decoder=str(dec),
            tokens=str(tok),
            num_threads=self.cfg.asr_sherpa_threads,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=1.0,   # 1s 静音后触发 endpoint
            rule2_min_trailing_silence=0.5,   # 说了 ≥0.5s 后 0.5s 静音即触发
            rule3_min_utterance_length=20,
            decoding_method="greedy_search",
            provider="cpu",
        )
        self._stream = self._recognizer.create_stream()
        logger.info("sherpa-onnx ready (RTF≈0.05, streaming)")
        return True

    def feed(self, pcm16: bytes) -> str:
        """喂一帧 16k mono PCM16（1024 字节/512 样本）。
        返回当前累积文本（可能空串）。RTF≈0.05 → ~5ms/100ms帧，不阻塞事件循环。
        """
        if self._recognizer is None or self._stream is None:
            return ""
        audio = np.frombuffer(pcm16, np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, audio)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        return self._recognizer.get_result(self._stream).strip()

    def get_result(self) -> str:
        """VAD 触发 end 后调用：喂一小段静音 flush → 取最终文本。"""
        if self._recognizer is None or self._stream is None:
            return ""
        silence = np.zeros(1600, dtype=np.float32)  # 100ms 静音
        self._stream.accept_waveform(16000, silence)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        return self._recognizer.get_result(self._stream).strip()

    def reset(self) -> None:
        """新话轮：重建 stream（recognizer 复用，不重新加载权重）。"""
        if self._recognizer is not None:
            self._stream = self._recognizer.create_stream()

    @property
    def available(self) -> bool:
        return self._recognizer is not None


# ---------------------------------------------------------------------------
# 批量 ASR（whisper.cpp / faster-whisper，sherpa 不可用时兜底）
# ---------------------------------------------------------------------------

class ASR:
    """批量转写后端（whisper.cpp Metal 或 faster-whisper）。

    用于：① sherpa 不可用时兜底；② --text CLI 路径（text=None 时不会走这里）。
    调用方用 asyncio.to_thread 包裹（GIL 会释放）。
    模型不保证并发安全，内部加锁串行。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._backend: str | None = None
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._model is not None:
            return
        if self.cfg.asr_backend in ("auto", "whispercpp") and self._try_load_whispercpp():
            return
        if self.cfg.asr_backend == "whispercpp":
            raise RuntimeError("asr_backend=whispercpp 但 pywhispercpp/ggml 权重不可用")
        self._load_faster_whisper()

    def _ggml_path(self) -> Path:
        p = Path(self.cfg.asr_ggml_model)
        return p if p.is_absolute() else Path(self.cfg.asr_download_root).parent / p

    def _try_load_whispercpp(self) -> bool:
        path = self._ggml_path()
        if not path.exists():
            logger.info("ggml 权重不存在（%s），回退 faster-whisper", path)
            return False
        try:
            from pywhispercpp.model import Model
        except ImportError:
            logger.info("pywhispercpp 未安装，回退 faster-whisper")
            return False
        logger.info("loading whisper.cpp %s ...", path.name)
        self._model = Model(str(path), n_threads=self.cfg.asr_threads,
                            print_progress=False, print_realtime=False)
        self._backend = "whispercpp"
        self._warm()
        logger.info("whisper.cpp ready（Metal 与否见上方 ggml 日志）")
        return True

    def _load_faster_whisper(self) -> None:
        import os
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from faster_whisper import WhisperModel
        logger.info("loading faster-whisper %s (%s) ...",
                    self.cfg.asr_model, self.cfg.asr_compute_type)
        self._model = WhisperModel(
            self.cfg.asr_model,
            device="cpu",
            compute_type=self.cfg.asr_compute_type,
            cpu_threads=self.cfg.asr_threads,
            download_root=self.cfg.asr_download_root,
        )
        self._backend = "faster"
        self._warm()
        logger.info("faster-whisper ready")

    def _warm(self) -> None:
        self._transcribe_locked(np.zeros(8000, dtype=np.float32))

    def transcribe(self, pcm16: bytes) -> str:
        """16k mono PCM16 → 文本。空串 = 无有效语音。"""
        self.load()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            text = self._transcribe_locked(audio)
        if _is_hallucination(text):
            logger.debug("幻觉丢弃: %r", text)
            return ""
        return text

    def _transcribe_locked(self, audio: np.ndarray) -> str:
        if self._backend == "whispercpp":
            segs = self._model.transcribe(
                audio,
                language=self.cfg.asr_language,
                initial_prompt=self.cfg.asr_initial_prompt,
            )
            return "".join(s.text for s in segs).strip()
        segments, _info = self._model.transcribe(
            audio,
            language=self.cfg.asr_language,
            beam_size=self.cfg.asr_beam_size,
            condition_on_previous_text=False,
            initial_prompt=self.cfg.asr_initial_prompt,
            vad_filter=False,
        )
        parts = []
        for seg in segments:
            if seg.no_speech_prob > self.cfg.asr_no_speech_prob_max:
                continue
            if seg.avg_logprob < self.cfg.asr_avg_logprob_min:
                continue
            parts.append(seg.text.strip())
        return "".join(parts).strip()
