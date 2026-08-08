"""edge-tts → PyAV 增量解码 → 24k mono PCM16 异步生成器。

edge-tts 免费口输出 audio-24khz-48kbitrate-mono-mp3；mp3 分块到达即用
av.CodecContext.parse() 增量出包、解码、重采样为 s16，不等整句合成完。
接口化：换备胎（say / Piper）只改本文件。
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import av
import edge_tts

from .config import Config

logger = logging.getLogger(__name__)


class TTSEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def synth_stream(self, text: str) -> AsyncIterator[bytes]:
        """一句文本 → 若干 PCM16(24k mono) chunk。任务取消即中止合成（ws 随之关闭）。"""
        codec = av.CodecContext.create("mp3", "r")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=self.cfg.out_rate)
        comm = edge_tts.Communicate(text, voice=self.cfg.tts_voice, rate=self.cfg.tts_rate)
        got_audio = False
        async for chunk in comm.stream():
            if chunk["type"] != "audio":
                continue
            got_audio = True
            for pcm in self._decode(codec, resampler, chunk["data"]):
                yield pcm
        # 冲刷解码器残余
        if got_audio:
            for pcm in self._flush(codec, resampler):
                yield pcm

    @staticmethod
    def _pcm_bytes(frame) -> bytes:
        # 不用 planes[0]：FFmpeg 缓冲有对齐填充，to_ndarray 按 linesize 取准确样本
        return frame.to_ndarray().tobytes()

    def _decode(self, codec, resampler, data: bytes):
        for packet in codec.parse(data):
            try:
                frames = codec.decode(packet)
            except av.FFmpegError:  # mp3 流开头可能有元数据碎片
                continue
            for frame in frames:
                for out in resampler.resample(frame):
                    yield self._pcm_bytes(out)

    def _flush(self, codec, resampler):
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
