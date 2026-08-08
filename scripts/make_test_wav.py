"""用 edge-tts 合成一句测试语音（男声，模拟用户提问）→ tmp/test16k.wav（16k mono）。
0 次 LLM 调用。用法：.venv/bin/python scripts/make_test_wav.py ["自定义文本"]
"""

from __future__ import annotations

import asyncio
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import av  # noqa: E402
import edge_tts  # noqa: E402

TEXT = sys.argv[1] if len(sys.argv) > 1 else "请用一句话介绍你自己。"
OUT = Path(__file__).resolve().parent.parent / "tmp" / "test16k.wav"


async def main() -> None:
    comm = edge_tts.Communicate(TEXT, voice="zh-CN-YunxiNeural")
    mp3 = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            mp3 += chunk["data"]
    assert len(mp3) > 0, "edge-tts 无音频返回"

    codec = av.CodecContext.create("mp3", "r")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    pcm = bytearray()
    for packet in codec.parse(bytes(mp3)) + codec.parse(None):
        try:
            frames = codec.decode(packet)
        except av.FFmpegError:
            continue
        for frame in frames:
            for r in resampler.resample(frame):
                pcm += r.to_ndarray().tobytes()
    for r in resampler.resample(None):
        pcm += r.to_ndarray().tobytes()

    OUT.parent.mkdir(exist_ok=True)
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(pcm))
    print(f"{OUT}  {len(pcm) / 2 / 16000:.1f}s  text={TEXT!r}")


asyncio.run(main())
