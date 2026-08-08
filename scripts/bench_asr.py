"""ASR 延迟基准（0 次 LLM 调用）：对 tmp/test16k.wav 转写 3 次取均值，输出 RTF。
用法：.venv/bin/python scripts/bench_asr.py [--model small]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.m0_pipeline import wav_frames_16k  # noqa: E402
from server.asr import ASR  # noqa: E402
from server.config import load_config  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--model", default=None)
p.add_argument("--wav", default=str(Path(__file__).resolve().parent.parent / "tmp" / "test16k.wav"))
args = p.parse_args()

cfg = load_config()
if args.model:
    cfg.asr_model = args.model

pcm = wav_frames_16k(args.wav)
audio_s = len(pcm) / 2 / 16000
asr = ASR(cfg)
t0 = time.monotonic()
asr.load()
load_s = time.monotonic() - t0

times = []
text = ""
for _ in range(3):
    t0 = time.monotonic()
    text = asr.transcribe(pcm)
    times.append(time.monotonic() - t0)

avg = sum(times) / len(times)
print(f"model={cfg.asr_model}({cfg.asr_compute_type}) load={load_s:.1f}s "
      f"audio={audio_s:.1f}s avg={avg:.2f}s RTF={avg / audio_s:.2f} text={text!r}")
