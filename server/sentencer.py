"""delta 流 → 可朗读句子流。纯函数逻辑，可单测。

规则：
- 代码块 ``` … ``` 整块替换为「（这里有段代码，略过。）」
- 行内清洗：去 markdown 记号/emoji，链接保留文字
- 主切分符：。！？!?；;…및换行；缓冲 > max_buffer 时逗号也可切
- 首句加速：本轮尚未出声且缓冲 ≥ first_min 字时遇逗号即切（压首包延迟）
"""

from __future__ import annotations

import re

_MD_PATTERNS = [
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # [文字](url) → 文字
    # 行内 ` 直接删（内容照读；围栏 ``` 在 feed 里先于 _clean 摘除，不受影响）
    (re.compile(r"`"), ""),
    (re.compile(r"\*\*|__|\*|~~"), ""),
    (re.compile(r"^#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),
    (re.compile(r"^\s*\d+\.\s+", re.M), ""),
]
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002600-\U000026FF\U0000FE0F\U0001F900-\U0001F9FF]+"
)

_HARD_BREAKS = "。！？!?；;…\n"
_SOFT_BREAKS = "，、,"

CODE_PLACEHOLDER = "（这里有段代码，略过。）"


def _clean(text: str) -> str:
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    text = _EMOJI.sub("", text)
    return text


class SentenceAssembler:
    def __init__(self, max_buffer: int = 50, first_min: int = 10):
        self.max_buffer = max_buffer
        self.first_min = first_min
        self.buf = ""
        self.raw = ""          # 未消费的原始增量（处理跨 delta 的 ``` 围栏）
        self.in_code = False
        self.emitted_any = False

    def feed(self, delta: str) -> list[str]:
        self.raw += delta
        out: list[str] = []
        # 围栏码块处理：成对消费 ```
        while True:
            idx = self.raw.find("```")
            if idx < 0:
                break
            head, self.raw = self.raw[:idx], self.raw[idx + 3:]
            if not self.in_code:
                self.buf += _clean(head)
                self.in_code = True
            else:
                # 丢弃代码内容，替换为占位句
                self.in_code = False
                self.buf += CODE_PLACEHOLDER
        if not self.in_code:
            # 尾部留 2 个字符不消费，防止 "```" 恰好断在 delta 边界
            if len(self.raw) > 2:
                safe, self.raw = self.raw[:-2], self.raw[-2:]
                self.buf += _clean(safe)
        out.extend(self._drain())
        return out

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            cut = -1
            for i, ch in enumerate(self.buf):
                if ch in _HARD_BREAKS:
                    cut = i
                    break
                if ch in _SOFT_BREAKS:
                    if len(self.buf) > self.max_buffer:
                        cut = i
                        break
                    if not self.emitted_any and not out and i + 1 >= self.first_min:
                        cut = i
                        break
            if cut < 0:
                break
            sent = self.buf[: cut + 1].strip()
            self.buf = self.buf[cut + 1:]
            if sent.strip("".join(_HARD_BREAKS) + _SOFT_BREAKS + " \t"):
                out.append(sent)
                self.emitted_any = True
        return out

    def flush(self) -> list[str]:
        """turn 结束：清洗残余并整体吐出。"""
        tail = self.raw if not self.in_code else ""
        self.raw = ""
        self.in_code = False
        self.buf += _clean(tail)
        out = self._drain()
        rest = self.buf.strip()
        self.buf = ""
        if rest:
            out.append(rest)
            self.emitted_any = True
        return out
