"""Hermes gateway 8647 的 SSE 客户端。

事实依据（api_server.py，只读核实过）：
- 带 X-Hermes-Session-Id 头时历史从 state.db 加载、body 历史被忽略 → 每轮只发最新 user 消息
- 响应头回传 X-Hermes-Session-Id（会话压缩可能轮换 id）→ 每次响应都要更新本地保存值
- 请求内 system 消息 = 临时叠加在核心 prompt 之上（不改 Hermes 配置）
- 客户端断开 SSE → 网关 agent.interrupt() + task cancel → barge-in 直接断连接即可
- SSE 线格式：OpenAI chunk + `event: hermes.tool.progress` 自定义事件 + `: keepalive` 注释行
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class HermesEvent:
    kind: str            # "delta" | "tool" | "done"
    text: str = ""       # delta 文本
    tool: dict | None = None
    finish_reason: str | None = None


class HermesClient:
    def __init__(self, cfg: Config, http: aiohttp.ClientSession):
        self.cfg = cfg
        self.http = http
        self.session_id: str | None = None  # 跨 WS 连接保持（单用户工具，服务级共享）

    def new_session(self) -> None:
        self.session_id = None

    async def health(self) -> bool:
        try:
            async with self.http.get(
                f"{self.cfg.hermes_base_url}/v1/health",
                headers={"Authorization": f"Bearer {self.cfg.hermes_api_key}"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                return r.status == 200
        except Exception:
            return False

    async def chat_stream(self, user_text: str) -> AsyncIterator[HermesEvent]:
        """流式对话。取消迭代（task cancel / 连接关闭）= 网关中断 run，不白烧 token。"""
        headers = {
            "Authorization": f"Bearer {self.cfg.hermes_api_key}",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["X-Hermes-Session-Id"] = self.session_id
        body = {
            "model": self.cfg.hermes_model,
            "stream": True,
            "messages": [
                {"role": "system", "content": self.cfg.voice_system_prompt},
                {"role": "user", "content": user_text},
            ],
        }
        # total=None：agent 长跑合法；网关每 30s 发 keepalive，sock_read=120 足够安全
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=120)
        async with self.http.post(
            f"{self.cfg.hermes_base_url}/v1/chat/completions",
            json=body, headers=headers, timeout=timeout,
        ) as resp:
            if resp.status != 200:
                text = (await resp.text())[:500]
                raise RuntimeError(f"hermes {resp.status}: {text}")
            sid = resp.headers.get("X-Hermes-Session-Id")
            if sid:
                self.session_id = sid

            event_name = "message"
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue  # keepalive 注释
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    if line == "":
                        event_name = "message"  # 事件块结束，复位
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("bad SSE json: %.200s", data)
                    continue

                if event_name == "hermes.tool.progress":
                    yield HermesEvent(kind="tool", tool=obj)
                    continue

                choices = obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield HermesEvent(kind="delta", text=delta)
                fr = choice.get("finish_reason")
                if fr:
                    yield HermesEvent(kind="done", finish_reason=fr)
