"""每 turn 延迟打点。M0 验收看这张表。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("metrics")


@dataclass
class TurnMetrics:
    turn: int
    t0: float = field(default_factory=time.monotonic)  # 用户停口（utterance 判定完成）
    asr_done: float | None = None
    first_delta: float | None = None
    first_sentence: float | None = None
    first_audio: float | None = None
    done: float | None = None
    asr_text: str = ""

    def mark(self, name: str) -> None:
        if getattr(self, name, None) is None:
            setattr(self, name, time.monotonic())

    def _d(self, v: float | None) -> str:
        return f"{v - self.t0:6.2f}s" if v else "   -  "

    def report(self) -> str:
        return (
            f"[turn {self.turn}] 停口→ASR {self._d(self.asr_done)} | 首delta {self._d(self.first_delta)}"
            f" | 首句 {self._d(self.first_sentence)} | ★首音 {self._d(self.first_audio)}"
            f" | 完 {self._d(self.done)} | asr={self.asr_text[:40]!r}"
        )

    def log(self) -> None:
        logger.info(self.report())
