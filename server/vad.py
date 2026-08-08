"""pysilero VAD 封装：32ms 帧流 → utterance 分段（含 pre-roll 环形缓冲）。

两种用法：
  Segmenter.feed(frame) —— listening 态分段器，产出完整语音段
  BargeDetector.feed(frame) —— speaking 态打断检测（高门槛 + 持续时长 + 回落重计时）
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from pysilero_vad import SileroVoiceActivityDetector

from .config import Config

FRAME_MS = 32  # 512 样本 @ 16k


@dataclass
class Utterance:
    pcm: bytes          # 16k mono PCM16，含 pre-roll
    duration_ms: int


class Segmenter:
    """帧进、事件出。feed() 返回 ("start"|"end", Utterance|None) 或 None。"""

    def __init__(self, cfg: Config, vad: SileroVoiceActivityDetector):
        self.cfg = cfg
        self.vad = vad
        self.preroll: deque[bytes] = deque(maxlen=max(1, cfg.vad_preroll_ms // FRAME_MS))
        self.in_speech = False
        self.voiced_run = 0
        self.silence_run_ms = 0
        self.buf: list[bytes] = []
        self.buf_ms = 0

    def reset(self) -> None:
        self.preroll.clear()
        self.in_speech = False
        self.voiced_run = 0
        self.silence_run_ms = 0
        self.buf = []
        self.buf_ms = 0

    def feed(self, frame: bytes) -> tuple[str, Utterance | None] | None:
        prob = self.vad(frame)
        if not self.in_speech:
            self.preroll.append(frame)
            if prob >= self.cfg.vad_threshold:
                self.voiced_run += 1
                if self.voiced_run >= self.cfg.vad_start_frames:
                    # 进入语音段：pre-roll（含触发帧）整体作为开头
                    self.in_speech = True
                    self.buf = list(self.preroll)
                    self.buf_ms = len(self.buf) * FRAME_MS
                    self.silence_run_ms = 0
                    self.voiced_run = 0
                    return ("start", None)
            else:
                self.voiced_run = 0
            return None

        # 语音段内
        self.buf.append(frame)
        self.buf_ms += FRAME_MS
        if prob >= self.cfg.vad_threshold:
            self.silence_run_ms = 0
        else:
            self.silence_run_ms += FRAME_MS

        end = self.silence_run_ms >= self.cfg.vad_end_silence_ms
        force = self.buf_ms >= self.cfg.vad_max_utterance_ms
        if not (end or force):
            return None

        pcm = b"".join(self.buf)
        dur = self.buf_ms - (self.silence_run_ms if end else 0)
        self.reset()
        if dur < self.cfg.vad_min_utterance_ms:
            return None  # 太短，当噪声丢弃
        return ("end", Utterance(pcm=pcm, duration_ms=dur))


class BargeDetector:
    """speaking 态打断：概率 ≥ barge_threshold 连续 barge_hold_ms 触发；任何回落重计时。
    始终维护 barge_preroll_ms 的环形缓冲，触发时把从首次超阈值起的音频还给调用方。"""

    def __init__(self, cfg: Config, vad: SileroVoiceActivityDetector):
        self.cfg = cfg
        self.vad = vad
        self.ring: deque[bytes] = deque(maxlen=max(1, cfg.barge_preroll_ms // FRAME_MS))
        self.hold_ms = 0

    def reset(self) -> None:
        self.ring.clear()
        self.hold_ms = 0

    def feed(self, frame: bytes) -> bytes | None:
        """触发打断时返回 pre-roll PCM（含正在说的开头），否则 None。"""
        self.ring.append(frame)
        prob = self.vad(frame)
        if prob >= self.cfg.barge_threshold:
            self.hold_ms += FRAME_MS
            if self.hold_ms >= self.cfg.barge_hold_ms:
                pcm = b"".join(self.ring)
                self.reset()
                return pcm
        else:
            self.hold_ms = 0
        return None


def new_vad() -> SileroVoiceActivityDetector:
    return SileroVoiceActivityDetector()
