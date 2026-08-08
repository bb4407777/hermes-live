"""浏览器 ↔ 服务 的 WS 协议。

二进制帧（首字节 = 类型，仿 moshi rust/protocol.md 的 1 字节前缀设计）：
  上行 0x01 + PCM16LE            16kHz mono，固定 512 样本（1024 字节 = 32ms）
  下行 0x01 + turn(u8) + PCM16LE 24kHz mono。turn 字节用于打断竞态：
                                  客户端只播放 turn == 当前 turn 的帧，旧帧自然丢弃。
其余控制/文本一律 JSON 文本帧（本地回环不省带宽，省调试成本）。
"""

from __future__ import annotations

import json

BIN_AUDIO = 0x01


def pack_audio_down(turn: int, pcm: bytes) -> bytes:
    return bytes((BIN_AUDIO, turn & 0xFF)) + pcm


def unpack_audio_up(data: bytes) -> bytes | None:
    """上行二进制帧 → PCM16 字节；非音频帧返回 None。"""
    if len(data) >= 1 and data[0] == BIN_AUDIO:
        return data[1:]
    return None


def msg(type_: str, **kw) -> str:
    kw["type"] = type_
    return json.dumps(kw, ensure_ascii=False)
