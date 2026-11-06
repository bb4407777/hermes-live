"""Session 协议回归：文字 turn 的出站顺序、tts_end/playback_done 收口、
打断与 new_session 的 turn 递增。

不碰真实 ASR/TTS/网关：只喂假事件流，专验 2026-09-19 修的那几处出站时序
（缺 tts_end 时客户端只能靠缓冲欠载猜"播完"，句间断流就被误判 → 提前开麦把
喇叭里的 TTS 录进下一轮；new_session 不 bump turn 则旧音频残留）。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.asr import ASR
from server.config import Config
from server.hermes_client import HermesEvent, mint_session_id
from server.session import FRAME_BYTES, Session

CHUNK = b"\x01\x00" * 2400        # 24k mono PCM16，2400 样本 = 0.1s
REPLY = ["收到，", "我把材料放桌上了。"]


class FakeHermes:
    def __init__(self, reply=None):
        self.session_id = mint_session_id()
        self.reply = reply if reply is not None else REPLY
        self.prompts: list[str] = []

    def new_session(self):
        self.session_id = mint_session_id()
        return self.session_id

    async def chat_stream(self, text):
        self.prompts.append(text)
        for piece in self.reply:
            yield HermesEvent("delta", piece)
            await asyncio.sleep(0)
        yield HermesEvent("done", finish_reason="stop")


class FakeTTS:
    """chunk_gap>0 模拟"音频到达比播放慢"（edge-tts 抽风 / 隧道抖动）：
    这种情况下 first_send + 音频总时长 会早于最后一帧，估算必须改用最后帧时刻。"""

    def __init__(self, chunk_gap=0.0, chunks=2):
        self.chunk_gap = chunk_gap
        self.chunks = chunks

    async def synth_stream(self, sentence):
        for i in range(self.chunks):
            if i and self.chunk_gap:
                await asyncio.sleep(self.chunk_gap)
            yield CHUNK


class FlakyTTS:
    """第一句正常出声、第二句合成失败（edge-tts 重试后仍挂）：
    异常会从 _tts_consumer 逃出去，此时音频已经发了一半。"""

    def __init__(self, fail_from=2, chunks=2):
        self.fail_from = fail_from
        self.chunks = chunks
        self.n = 0

    async def synth_stream(self, sentence):
        self.n += 1
        if self.n >= self.fail_from:
            raise RuntimeError("edge-tts 挂了")
        for _ in range(self.chunks):
            yield CHUNK


def _new(tts_chunk_gap=0.0, reply=None, tts=None):
    outbox: asyncio.Queue = asyncio.Queue()
    hermes = FakeHermes(reply)
    cfg = Config()
    # asr 传真 ASR 对象（构造不加载权重）：Session.asr_label() 会读它，None 会让 hello 发送崩
    return (Session(cfg, ASR(cfg), tts or FakeTTS(tts_chunk_gap), hermes, outbox),
            outbox, hermes)


async def _until(outbox, events, pred, timeout, what):
    """逐条取出站消息直到 pred 命中；超时直接判失败（不留"静默继续"的空洞）。
    每条连带到达时刻，用于断言事件间的时间关系。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            raise AssertionError(f"等 {what} 超时；已收到 {_kinds(events)}")
        kind, payload = await asyncio.wait_for(outbox.get(), timeout=remain)
        events.append((kind, json.loads(payload) if kind == "json" else payload,
                       loop.time()))
        if pred(events[-1]):
            return len(events) - 1


async def _so_far(outbox, events, seconds):
    """再收 seconds 秒（用于断言"这段时间不该有新出站"）。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + seconds
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            return
        try:
            kind, payload = await asyncio.wait_for(outbox.get(), timeout=remain)
        except asyncio.TimeoutError:
            return
        events.append((kind, json.loads(payload) if kind == "json" else payload,
                       loop.time()))


def _types(events):
    return [e[1]["type"] for e in events if e[0] == "json"]


def _kinds(events):
    return [(e[0] if e[0] == "bin" else e[1]["type"]) for e in events]


def _is(typ, state=None):
    return lambda it: it[0] == "json" and it[1]["type"] == typ and (
        state is None or it[1].get("state") == state)


def _run(scenario, tts_chunk_gap=0.0, reply=None, tts=None):
    async def main():
        session, outbox, hermes = _new(tts_chunk_gap, reply, tts)
        try:
            await scenario(session, outbox, hermes)
        finally:
            await session.close()
    asyncio.run(main())


def test_audio_then_tts_end_then_listening():
    async def scenario(s, outbox, hermes):
        events: list = []
        await s.on_control({"type": "start"})
        await _until(outbox, events, _is("state", "listening"), 2, "listening")
        await s.on_control({"type": "text", "text": "在吗"})

        await _until(outbox, events, _is("state", "thinking"), 2, "thinking")
        end = await _until(outbox, events, _is("tts_end"), 5, "tts_end")
        turn = events[end][1]["turn"]
        assert _types(events).count("asr_final") == 0, "文字轮不该冒 asr_final"
        assert "agent_done" in _types(events)
        assert "tts_sentence" in _types(events)

        bins = [i for i, e in enumerate(events) if e[0] == "bin"]
        assert all(events[i][1][1] == turn for i in bins), "音频帧 turn 字节必须统一"
        # speaking 必须早于第一帧音频：客户端据此暂停上行（半双工）
        speaking = next(i for i, e in enumerate(events)
                        if e[0] == "json" and e[1].get("state") == "speaking")
        assert speaking < bins[0] < end
        assert bins[-1] < end, "tts_end 必须晚于最后一帧音频"

        # tts_end 之后不许立刻收口：客户端还在播，提前回 listening 就是开麦收自己
        await _so_far(outbox, events, 0.6)
        assert len(events) == end + 1, _kinds(events)

        await s.on_control({"type": "playback_done", "turn": turn})
        done = await _until(outbox, events, _is("state", "listening"), 3, "收口 listening")
        assert done > end
        assert events[done][1]["turn"] == turn
    _run(scenario)


def test_falls_back_to_listening_without_playback_done():
    """客户端不回 playback_done（页面被杀、旧前端）时不能永远卡在 speaking。"""
    async def scenario(s, outbox, hermes):
        events: list = []
        await s.on_control({"type": "start"})
        await s.on_control({"type": "text", "text": "在吗"})
        end = await _until(outbox, events, _is("tts_end"), 5, "tts_end")
        await _so_far(outbox, events, 0.6)
        assert len(events) == end + 1, "回执没到就该等着"
        # 音频总时长 0.4s + playback_grace_ms(0.9s) 内必须自己收口
        await _until(outbox, events, _is("state", "listening"), 3, "估算时长收口")
    _run(scenario)


def test_interrupt_bumps_turn_and_stops_old_audio():
    async def scenario(s, outbox, hermes):
        events: list = []
        await s.on_control({"type": "start"})
        await s.on_control({"type": "text", "text": "在吗"})
        await _until(outbox, events, lambda it: it[0] == "bin", 5, "音频帧")
        old_turn = max(events[i][1][1] for i in range(len(events)) if events[i][0] == "bin")
        await s.on_control({"type": "interrupt"})
        events.clear()
        last = await _until(outbox, events, _is("state", "listening"), 5, "打断后 listening")
        assert events[last][1]["turn"] > old_turn, "turn 不递增，客户端丢不掉在途旧帧"
        # 取消前已入队的几帧还会发出去（客户端按 turn 字节丢），要断言的是"不再产生新音频"
        n = len(events)
        await _so_far(outbox, events, 0.6)
        assert not any(e[0] == "bin" for e in events[n:]), "打断后旧 turn 还在继续合成下发"
    _run(scenario)


def test_new_session_rotates_identity_and_turn():
    async def scenario(s, outbox, hermes):
        events: list = []
        old_id = hermes.session_id
        await s.on_control({"type": "start"})
        await s.on_control({"type": "text", "text": "在吗"})
        end = await _until(outbox, events, _is("tts_end"), 5, "tts_end")
        await s.on_control({"type": "playback_done", "turn": events[end][1]["turn"]})
        await _until(outbox, events, _is("state", "listening"), 3, "收口")
        turn_before = s.turn
        events.clear()
        await s.on_control({"type": "new_session"})
        hi = await _until(outbox, events, _is("hello"), 3, "hello")
        assert hi >= 0
        assert events[hi][1]["session_id"] != old_id, "换会话必须换网关 session id"
        assert s.turn > turn_before, "不 bump turn 客户端会继续放上一轮残留"
    _run(scenario)


def test_slow_synthesis_waits_past_the_last_frame():
    """生成比播放慢时（模型长答 + edge-tts 抽风），旧估算按 first_send + 音频总时长算，
    会在喇叭还在播时就回 listening → 自己的 TTS 被录进下一轮。"""
    async def scenario(s, outbox, hermes):
        events: list = []
        await s.on_control({"type": "start"})
        await s.on_control({"type": "text", "text": "在吗"})
        await _until(outbox, events, _is("tts_end"), 10, "tts_end")
        last_bin = max(i for i, e in enumerate(events) if e[0] == "bin")
        li = await _until(outbox, events, _is("state", "listening"), 10, "收口")
        gap = events[li][2] - events[last_bin][2]
        # 旧估算 = first_send + 音频总时长(0.4s)，此时末帧已过 1.5s → 只剩 0.5s 底值，
        # 会在客户端还在播的时候就回 listening；新估算锚在最后帧 + grace(0.9s)。
        assert gap >= 0.7, f"末帧后只等了 {gap:.2f}s，客户端还在播就该继续等"
        assert gap < 2.0, f"等过头了：{gap:.2f}s"
    _run(scenario, tts_chunk_gap=1.5, reply=["好的。", "我这就去查。"])


def test_tts_failure_still_closes_the_turn():
    """合成中途死掉（edge-tts 重试后仍失败）：此刻半段音频已经发出去了。
    少一条 tts_end，客户端的收口门（worklet 的 ended）就永远不开——尾音不上报播完，
    而服务端按异常路径直接回了 listening 放开麦克风 → AI 把自己念的尾巴录进下一轮。"""
    async def scenario(s, outbox, hermes):
        events: list = []
        await s.on_control({"type": "start"})
        await s.on_control({"type": "text", "text": "在吗"})
        await _until(outbox, events, _is("error"), 5, "error")
        types = _types(events)
        assert any(e[0] == "bin" for e in events), "前半句音频该已经下发"
        assert "tts_end" in types, f"半段音频之后没有 tts_end，客户端永远收不了口：{types}"
        assert types.index("tts_end") < types.index("error"), "收口必须排在报错之前"
    # 三小句才够分句器切出两句：两句时第一句刚出声就炸，才有"半段音频"可收口
    _run(scenario, reply=["收到，", "我把材料放桌上了。", "第三句随后。"],
         tts=FlakyTTS(fail_from=2))


def test_uplink_dropped_once_outbound_is_dead():
    """半开隧道（手机息屏）：出站写失败后 read 侧还能继续送麦克风帧，heartbeat 判死要 ~30s。
    这期间再起话轮，就是给一个永远收不到回音的连接白烧网关 token。"""
    async def scenario(s, outbox, hermes):
        events: list = []
        await s.on_control({"type": "start"})
        await _until(outbox, events, _is("state", "listening"), 2, "listening")
        s.on_conn_lost()
        await asyncio.sleep(0)   # 让 on_conn_lost 起的取消任务跑一步
        fed: list[int] = []
        s.segmenter.feed = lambda pcm: (fed.append(1), None)[1]
        for _ in range(50):
            await s.on_audio(b"\x00" * FRAME_BYTES)
        assert not fed, f"closed 之后还在跑 VAD（{len(fed)} 帧）"
        assert s.turn == 0, "出站已死却还在起话轮"
        assert outbox.empty(), "closed 之后还在往 outbox 塞消息"
    _run(scenario)
