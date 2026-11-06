import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.asr import DoubaoStreamingASR
from server.config import Config


def _asr():
    cfg = Config(asr_backend="doubao")
    asr = DoubaoStreamingASR(cfg)
    asr._available = True
    return asr


def test_reconnect_backs_off_and_preserves_audio(monkeypatch):
    async def scenario():
        calls = 0
        received = []
        delays = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            delays.append(delay)
            await real_sleep(0)

        async def transcribe_realtime(pcm, config):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("concurrency quota exceeded")
            async for chunk in pcm:
                received.append(chunk)
            yield SimpleNamespace(type=SimpleNamespace(name="FINAL_RESULT"), text="识别成功")

        monkeypatch.setitem(sys.modules, "doubaoime_asr",
                            SimpleNamespace(transcribe_realtime=transcribe_realtime))
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        asr = _asr()
        asr.start_turn()
        await real_sleep(0)
        await real_sleep(0)

        asr.feed(b"a" * 1024)
        asr.feed(b"b" * 1024)
        await real_sleep(0)
        assert delays == [1.0]
        assert calls == 1
        assert await asr.finish() == "识别成功"
        assert received == [b"a" * 1024, b"b" * 1024]
        assert calls == 2
        asr.reset()

    asyncio.run(scenario())


def test_reset_cancels_pending_reconnect(monkeypatch):
    async def scenario():
        calls = 0

        async def transcribe_realtime(pcm, config):
            nonlocal calls
            calls += 1
            raise RuntimeError("concurrency quota exceeded")
            yield

        monkeypatch.setitem(sys.modules, "doubaoime_asr",
                            SimpleNamespace(transcribe_realtime=transcribe_realtime))
        asr = _asr()
        monkeypatch.setattr(asr, "_RECONNECT_DELAYS", (60.0, 60.0, 60.0))
        asr.start_turn()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        asr.feed(b"a" * 1024)
        pending = asr._task
        assert pending is not None and not pending.done()

        asr.reset()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert calls == 1
        assert asr._pcm_queue is None

    asyncio.run(scenario())
