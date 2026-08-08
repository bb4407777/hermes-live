"""ASR 双后端，懒加载单例 + 防幻觉参数。

backend 实测（M1 Pro，2.9s 中文测试音频，2026-08-08）：
  whispercpp (turbo q5_0, Metal)  —— 首选：质量=turbo，速度靠 GPU
  faster-whisper large-v3-turbo int8 —— RTF≈1.2，太慢，只作兜底
  faster-whisper small int8          —— RTF≈0.4 但错字明显

调用方用 asyncio.to_thread 包裹（两个后端计算时都释放 GIL）。
模型不保证并发安全，内部加锁串行（被打断遗弃的转写线程跑完即弃）。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .config import Config

logger = logging.getLogger(__name__)

# 静音/极短段时 whisper 家族的常见幻觉（配合 VAD 严分段，命中即丢）
_HALLUCINATIONS = ("谢谢观看", "请订阅", "字幕由", "感谢观看", "谢谢大家", "明镜与点点")


class ASR:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._backend: str | None = None
        self._model = None
        self._lock = threading.Lock()

    # ---------- 加载 ----------

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

        # 本机直连 HF 慢且 Xet CAS 会 401（绕过镜像），默认走 hf-mirror + 禁 Xet；已设的环境变量优先
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

    # ---------- 转写 ----------

    def transcribe(self, pcm16: bytes) -> str:
        """16k mono PCM16 → 文本。空串 = 无有效语音。"""
        self.load()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            text = self._transcribe_locked(audio)
        for h in _HALLUCINATIONS:
            if text.startswith(h) and len(text) <= len(h) + 2:
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
            vad_filter=False,  # 分段已由我们的 VAD 做
        )
        parts = []
        for seg in segments:
            if seg.no_speech_prob > self.cfg.asr_no_speech_prob_max:
                continue
            if seg.avg_logprob < self.cfg.asr_avg_logprob_min:
                continue
            parts.append(seg.text.strip())
        return "".join(parts).strip()
