"""faster-whisper 懒加载单例 + 防幻觉参数。

CTranslate2 计算时释放 GIL，调用方用 asyncio.to_thread 包裹即可不卡事件循环。
模型不保证并发安全，内部加锁串行（被打断遗弃的转写线程跑完即弃）。
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import Config

logger = logging.getLogger(__name__)


class ASR:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        """加载并预热（首次会从 HF 下载权重到 models/，建议 HF_ENDPOINT=https://hf-mirror.com）。"""
        if self._model is not None:
            return
        import os

        # 本机直连 HF 慢且 Xet CAS 会 401（绕过镜像），默认走 hf-mirror + 禁 Xet；已设的环境变量优先
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from faster_whisper import WhisperModel

        logger.info("loading whisper %s (%s) ...", self.cfg.asr_model, self.cfg.asr_compute_type)
        self._model = WhisperModel(
            self.cfg.asr_model,
            device="cpu",
            compute_type=self.cfg.asr_compute_type,
            download_root=self.cfg.asr_download_root,
        )
        # 预热：0.5s 静音，吃掉首次调用的初始化抖动
        warm = np.zeros(8000, dtype=np.float32)
        segs, _ = self._model.transcribe(warm, language=self.cfg.asr_language, beam_size=1)
        list(segs)
        logger.info("whisper ready")

    def transcribe(self, pcm16: bytes) -> str:
        """16k mono PCM16 → 文本。空串 = 无有效语音（全部段被防幻觉过滤）。"""
        self.load()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
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
