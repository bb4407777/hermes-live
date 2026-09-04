"""Qwen3-ASR 本地批量 ASR：复用 weSaw 的 sidecar 常驻进程（MPS 推理）。

链路：hermes-live 服务 → 子进程（weSaw .venv-qwenasr + qwenasr_server.py + 本地权重）。
协议（stdin/stdout 各一行一个 JSON，与 weSaw 共用同一脚本）：
  请求：{"id": N, "wav": "/tmp/xxx.wav"}（16k 单声道 PCM16 wav）
  响应：{"id": N, "ok": true, "text": "..."} 或 {"id": N, "ok": false, "error": "..."}
  sidecar 加载完模型并预热后先发 {"ready": true}。

批量模型没有实时中间结果，asr_partial 字幕在此后端不可用；VAD 断段后整段转写。

生命周期：load() 只做路径检查；进程随首次使用或启动预热（asr_qwen_warm_at_boot）拉起，
之后常驻（asr_qwen_idle_kill_min>0 时空闲回收）；崩溃后下次调用惰性重启。
hermes-live 退出时 shutdown() 关 stdin，sidecar 读到 EOF 自然退出，不留孤儿。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import queue
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

from .config import Config, PROJECT_ROOT

logger = logging.getLogger(__name__)


class QwenSidecarASR:
    """weSaw qwenasr_server.py 的 Python 客户端：管理子进程 + JSON 行协议。

    transcribe() 为同步接口，调用方用 asyncio.to_thread 包裹（与批量 ASR 同约定）；
    load() 轻量（只查路径，不 spawn），供启动链探活。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._available = False
        self._proc: subprocess.Popen | None = None
        self._resp_q: queue.Queue[dict] = queue.Queue()
        self._ready_ev = threading.Event()
        self._proc_lock = threading.Lock()   # spawn / 崩溃重启互斥
        self._io_lock = threading.Lock()     # 请求-响应往返互斥（sidecar 串行）
        self._ids = itertools.count(1)
        self._last_used = time.monotonic()
        self._reaper_task: asyncio.Task | None = None

    # ---------- 探活 / 生命周期 ----------

    def load(self) -> bool:
        """检查后端是否可选：backend 档位匹配 + weSaw venv/脚本/权重都在。不 spawn。"""
        if self.cfg.asr_backend not in ("auto", "qwen"):
            return False
        python = Path(self.cfg.asr_qwen_python).expanduser()
        script = Path(self.cfg.asr_qwen_server).expanduser()
        model = Path(self.cfg.asr_qwen_model).expanduser()
        for p in (python, script, model):
            if not p.exists():
                logger.info("qwen3-asr 依赖不存在（%s），跳过", p)
                return False
        self._available = True
        logger.info("qwen3-asr sidecar 就绪（%s，懒加载/预热后台进行）", model.name)
        return True

    @property
    def available(self) -> bool:
        return self._available

    async def ensure_started(self) -> bool:
        """拉起 sidecar 并等到 ready（模型加载+预热）。可在事件循环里 create_task 调用。"""
        if not self._available:
            return False
        ok = await asyncio.to_thread(self._ensure_proc)
        if ok and self.cfg.asr_qwen_idle_kill_min > 0 and self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
        return ok

    def _ensure_proc(self) -> bool:
        """确保子进程活着且已 ready（阻塞，供 to_thread）。"""
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None and self._ready_ev.is_set():
                return True
            return self._start_locked()

    def _start_locked(self) -> bool:
        """spawn sidecar 并等 {"ready": true}。必须在 _proc_lock 内调用。"""
        self._stop_locked()
        python = str(Path(self.cfg.asr_qwen_python).expanduser())
        script = str(Path(self.cfg.asr_qwen_server).expanduser())
        model = str(Path(self.cfg.asr_qwen_model).expanduser())
        logger.info("启动 qwen3-asr sidecar（模型加载约需数十秒）...")
        try:
            self._proc = subprocess.Popen(
                [python, script, model],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
        except Exception as exc:
            logger.error("qwen3-asr sidecar 启动失败：%s", exc)
            self._proc = None
            return False
        self._ready_ev.clear()
        self._resp_q = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        if not self._ready_ev.wait(timeout=self.cfg.asr_qwen_start_timeout):
            logger.error("qwen3-asr sidecar ready 超时（%.0fs）", self.cfg.asr_qwen_start_timeout)
            self._stop_locked()
            return False
        if self._proc.poll() is not None:
            logger.error("qwen3-asr sidecar 在预热阶段退出（code=%s）", self._proc.returncode)
            self._proc = None
            return False
        logger.info("qwen3-asr sidecar ready")
        return True

    def _pump_stdout(self) -> None:
        """后台读 stdout：ready 行置事件，其余响应进队列；EOF 哨兵唤醒等待方。"""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("qwen3-asr 非法响应行：%r", line[:100])
                    continue
                if msg.get("ready"):
                    self._ready_ev.set()
                    logger.info("qwen3-asr sidecar 报告 ready")
                else:
                    self._resp_q.put(msg)
        except Exception as exc:
            logger.warning("qwen3-asr stdout 读取异常：%s", exc)
        finally:
            self._ready_ev.set()          # 放行卡在等待 ready 的调用方
            self._resp_q.put({"_eof": True})

    def _pump_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                logger.info("qwenasr: %s", line[:200])

    def _stop_locked(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()  # 关 stdin → sidecar 读到 EOF 退出
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None
        self._ready_ev.clear()

    def shutdown(self) -> None:
        """服务退出时调用：结束 sidecar，避免 launchd 重启窗口期双模型驻留。"""
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        with self._proc_lock:
            self._stop_locked()

    async def _reaper_loop(self) -> None:
        limit = self.cfg.asr_qwen_idle_kill_min * 60
        while True:
            await asyncio.sleep(60)
            if time.monotonic() - self._last_used > limit:
                logger.info("qwen3-asr sidecar 空闲 %.0f 分钟，回收省内存", limit / 60)
                with self._proc_lock:
                    self._stop_locked()

    # ---------- 转写 ----------

    def transcribe(self, pcm16: bytes) -> str:
        """16k mono PCM16 → 文本。空串 = 无有效语音或 sidecar 不可用。"""
        if not self._available:
            return ""
        self._last_used = time.monotonic()
        if not self._ensure_proc():
            return ""
        wav_path = self._write_wav(pcm16)
        req_id = next(self._ids)
        req = json.dumps({"id": req_id, "wav": str(wav_path)}, ensure_ascii=False)
        try:
            with self._io_lock:
                proc = self._proc
                if proc is None or proc.poll() is not None or proc.stdin is None:
                    logger.error("qwen3-asr sidecar 已死且重启失败，丢弃本轮")
                    return ""
                proc.stdin.write(req + "\n")
                proc.stdin.flush()
                deadline = time.monotonic() + self.cfg.asr_qwen_timeout
                while True:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        logger.error("qwen3-asr 转写超时（%.0fs）", self.cfg.asr_qwen_timeout)
                        return ""
                    try:
                        msg = self._resp_q.get(timeout=remain)
                    except queue.Empty:
                        continue
                    if msg.get("_eof"):
                        logger.error("qwen3-asr sidecar 中途退出（code=%s）",
                                     proc.poll() if proc else None)
                        return ""
                    if msg.get("id") != req_id:
                        continue  # 过期响应，跳过
                    if not msg.get("ok"):
                        logger.error("qwen3-asr 转写失败：%s", msg.get("error"))
                        return ""
                    return self._finish_ok(msg.get("text") or "")
        finally:
            wav_path.unlink(missing_ok=True)

    @staticmethod
    def _write_wav(pcm16: bytes) -> Path:
        """PCM16 bytes → 16k 单声道 wav 临时文件（sidecar 的 wave 模块只吃带头的 wav）。"""
        try:
            fd, name = tempfile.mkstemp(prefix="hl-asr-", suffix=".wav",
                                        dir=str(PROJECT_ROOT / "tmp"))
        except OSError:
            fd, name = tempfile.mkstemp(prefix="hl-asr-", suffix=".wav")
        import os
        os.close(fd)
        path = Path(name)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm16)
        return path

    def _finish_ok(self, text: str) -> str:
        from .asr import _is_hallucination
        if _is_hallucination(text):
            logger.debug("幻觉丢弃: %r", text)
            return ""
        return text.strip()
