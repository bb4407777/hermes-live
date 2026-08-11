"""TTS エンジン：kokoro-onnx（本地优先）→ edge-tts（云端兜底）。

auto モード：kokoro モデルが存在すれば kokoro、なければ edge-tts。
kokoro モード：強制ローカル（モデルなければ起動エラー）。
edge  モード：強制クラウド（従来動作）。

kokoro 出力：24kHz float32 配列 → s16 PCM に変換して下行ストリームへ送出。
edge-tts 出力：audio-24khz-48kbitrate-mono-mp3 → PyAV 増量デコード → s16 PCM。
どちらも out_rate=24000 で出力し、クライアント変更不要。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

import numpy as np

from .config import Config

logger = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._kokoro = None           # kokoro_onnx.Kokoro インスタンス（遅延ロード）
        self._kokoro_voice = None     # ロード済みの voice ndarray
        self._backend: str | None = None  # "kokoro" or "edge"

    def load(self) -> None:
        """起動時プリロード。失敗しても edge-tts 兜底があるので例外を握りつぶす。"""
        if self.cfg.tts_backend in ("auto", "kokoro"):
            try:
                self._load_kokoro()
            except Exception as e:
                if self.cfg.tts_backend == "kokoro":
                    raise
                logger.warning("kokoro TTS ロード失敗（edge-tts にフォールバック）: %s", e)
        if self._backend is None:
            self._backend = "edge"
            logger.info("TTS: edge-tts (cloud)")

    def _load_kokoro(self) -> None:
        model = Path(self.cfg.kokoro_model)
        voices = Path(self.cfg.kokoro_voices)
        if not model.exists():
            raise FileNotFoundError(f"kokoro model not found: {model}")
        if not voices.exists():
            raise FileNotFoundError(f"kokoro voices not found: {voices}")
        try:
            import kokoro_onnx
        except ImportError as e:
            raise ImportError("kokoro-onnx not installed") from e
        logger.info("loading kokoro-onnx (%s) ...", model.name)
        self._kokoro = kokoro_onnx.Kokoro(str(model), str(voices))
        # voice style を一度だけ取得（ndarray、毎回ロードは重い）
        self._kokoro_voice = self._kokoro.get_voice_style(self.cfg.kokoro_voice)
        self._backend = "kokoro"
        logger.info("TTS: kokoro-onnx %s ready", self.cfg.kokoro_voice)

    async def synth_stream(self, text: str) -> AsyncIterator[bytes]:
        """一句文本 → 若干 PCM16(24k mono) chunk。"""
        if self._backend is None:
            self.load()  # 遅延ロード（--no-preload 時）
        if self._backend == "kokoro":
            async for chunk in self._synth_kokoro(text):
                yield chunk
        else:
            async for chunk in self._synth_edge(text):
                yield chunk

    # ---------- kokoro backend ----------

    async def _synth_kokoro(self, text: str) -> AsyncIterator[bytes]:
        """kokoro create_stream：非同期ジェネレータ、ONNX 推論はスレッドプールで実行。"""
        try:
            async for audio_arr, _sr in self._kokoro.create_stream(
                text,
                voice=self._kokoro_voice,
                speed=self.cfg.kokoro_speed,
                lang="cmn",  # kokoro-v1.1-zh 普通话 espeak 语言码
            ):
                if audio_arr is None or len(audio_arr) == 0:
                    continue
                # float32 [-1,1] → int16
                pcm = (np.clip(audio_arr, -1.0, 1.0) * 32767).astype(np.int16)
                yield pcm.tobytes()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("kokoro 合成失败，回退 edge-tts: %s", exc)
            async for chunk in self._synth_edge(text):
                yield chunk

    # ---------- edge-tts backend ----------

    async def _synth_edge(self, text: str) -> AsyncIterator[bytes]:
        """edge-tts → PyAV 增量解码 → 24k mono PCM16。"""
        import av
        import edge_tts
        codec = av.CodecContext.create("mp3", "r")
        resampler = av.AudioResampler(format="s16", layout="mono",
                                      rate=self.cfg.out_rate)
        comm = edge_tts.Communicate(text, voice=self.cfg.tts_voice,
                                    rate=self.cfg.tts_rate)
        got_audio = False
        try:
            async for chunk in comm.stream():
                if chunk["type"] != "audio":
                    continue
                got_audio = True
                for pcm in self._decode(codec, resampler, chunk["data"]):
                    yield pcm
            if got_audio:
                for pcm in self._flush(codec, resampler):
                    yield pcm
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _pcm_bytes(frame) -> bytes:
        return frame.to_ndarray().tobytes()

    def _decode(self, codec, resampler, data: bytes):
        import av
        for packet in codec.parse(data):
            try:
                frames = codec.decode(packet)
            except av.FFmpegError:
                continue
            for frame in frames:
                for out in resampler.resample(frame):
                    yield self._pcm_bytes(out)

    def _flush(self, codec, resampler):
        import av
        try:
            frames = codec.decode(None)
        except av.FFmpegError:
            frames = []
        for frame in frames:
            for out in resampler.resample(frame):
                yield self._pcm_bytes(out)
        try:
            for out in resampler.resample(None):
                yield self._pcm_bytes(out)
        except av.FFmpegError:
            pass
