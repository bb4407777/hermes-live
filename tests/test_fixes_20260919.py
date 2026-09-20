"""回归测试：2026-09-19 排查出的几个真 bug（跨案串历史、出站协程即死、token 明文入日志）。

这些用例的意义在于"钉住"修法：相关代码再被改回原样时，测试要红。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_log import AccessLogger

from server.config import Config
from server.hermes_client import HermesClient, mint_session_id
from server.main import TokenMaskingAccessLogger, _check_token, ws_writer


# ---------- 会话身份：绝不能让网关按内容指纹派生 ----------

def test_mint_session_id_never_empty():
    ids = {mint_session_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i for i in ids)


def _client() -> HermesClient:
    return HermesClient(Config(), http=None)


def test_headers_always_carry_session_id():
    """空/缺 X-Hermes-Session-Id 时网关用 sha256(system_prompt+首句) 当会话身份，
    同一句开场白会复活几个星期前的会话历史（律师场景 = 跨案串内容）。"""
    c = _client()
    assert c.headers()["X-Hermes-Session-Id"] == c.session_id


def test_new_session_changes_identity():
    c = _client()
    old = c.session_id
    new = c.new_session()
    assert new and new != old
    assert c.headers()["X-Hermes-Session-Id"] == new


def test_two_sessions_do_not_share_history_key():
    assert _client().session_id != _client().session_id


# ---------- 出站协程：写失败要回告 Session，不能悄悄死掉 ----------

class _BrokenWS:
    def __init__(self):
        self.sent = []
        self.fail_after = 0

    async def send_str(self, payload):
        if len(self.sent) >= self.fail_after:
            raise ConnectionResetError("隧道断了")
        self.sent.append(payload)

    send_bytes = send_str


class _SpySession:
    def __init__(self):
        self.conn_lost = 0

    def on_conn_lost(self):
        self.conn_lost += 1


def _drain(outbox, items):
    for it in items:
        outbox.put_nowait(it)
    outbox.put_nowait(None)


def test_ws_writer_notifies_session_on_send_failure():
    ws, outbox, session = _BrokenWS(), asyncio.Queue(), _SpySession()
    ws.fail_after = 1                     # 第一条写成功，第二条起报错
    _drain(outbox, [("json", "a"), ("json", "b"), ("json", "c")])

    async def run():
        await ws_writer(ws, outbox, session)

    asyncio.run(run())
    assert ws.sent == ["a"]              # 失败后不再往上写
    assert session.conn_lost == 1        # 且必须回告，否则 turn 卡在等 playback_done


def test_ws_writer_finishes_cleanly_on_sentinel():
    ws, outbox, session = _BrokenWS(), asyncio.Queue(), _SpySession()
    ws.fail_after = 99
    _drain(outbox, [("json", "a"), ("json", "b")])

    async def run():
        await ws_writer(ws, outbox, session)

    asyncio.run(run())
    assert ws.sent == ["a", "b"]
    assert session.conn_lost == 0


# ---------- 鉴权与访问日志 ----------

class _CaptureLogger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *args, **kw):
        self.lines.append(msg % args if args else msg)

    def isEnabledFor(self, level):
        return True


def _app_with_token(token="s3cr3t-value"):
    app = web.Application()
    app["cfg"] = Config(auth_token=token)
    return app


def test_access_log_masks_token():
    sink = _CaptureLogger()
    access = TokenMaskingAccessLogger(sink, AccessLogger.LOG_FORMAT)
    req = make_mocked_request("GET", "/ws?token=s3cr3t-value", app=_app_with_token())
    access.log(req, web.json_response({"ok": True}), 0.12)
    assert sink.lines, "访问日志一行都没写，遮罩实现挂了"
    joined = "\n".join(sink.lines)
    assert "s3cr3t-value" not in joined
    assert "***" in joined


def test_access_log_survives_request_none():
    """aiohttp 对畸形请求会以 request=None 记日志，取值函数不能因此抛异常。"""
    sink = _CaptureLogger()
    access = TokenMaskingAccessLogger(sink, AccessLogger.LOG_FORMAT)
    access.log(None, web.json_response({"ok": True}), 0.1)
    assert sink.lines


def _remote(req, ip):
    req._transport_peername = (ip, 0)     # BaseRequest.remote 取 peername[0]
    return req


def test_check_token_accepts_bearer_header():
    req = make_mocked_request(
        "POST", "/api/restart", headers={"Authorization": "Bearer s3cr3t-value"},
        app=_app_with_token(),
    )
    _check_token(_remote(req, "10.0.0.5"))     # 非回环：必须鉴权


def test_check_token_rejects_wrong_and_bare_remote():
    app = _app_with_token()
    with pytest.raises(web.HTTPUnauthorized):
        _check_token(_remote(make_mocked_request("GET", "/ws?token=nope", app=app), "10.0.0.5"))
    with pytest.raises(web.HTTPUnauthorized):
        _check_token(_remote(make_mocked_request("GET", "/ws", app=app), "10.0.0.5"))


def test_check_token_forces_token_on_tunnel_even_from_loopback():
    """cloudflared 就在本机经回环转发，只看 remote 会把公网流量当本机放过。"""
    app = _app_with_token()
    with pytest.raises(web.HTTPUnauthorized):
        _check_token(_remote(
            make_mocked_request("GET", "/ws", headers={"CF-Ray": "abc123"}, app=app),
            "127.0.0.1"))
    # 带上 token 才放行
    _check_token(_remote(
        make_mocked_request("GET", "/ws?token=s3cr3t-value",
                            headers={"CF-Ray": "abc123"}, app=app), "127.0.0.1"))


# ---------- 批量 ASR 兜底：用本机权重，不许静默拉 3 GB ----------

def _asr_cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_batch_asr_prefers_local_ggml_for_every_backend(monkeypatch):
    """旧逻辑只在 asr_backend=auto/whispercpp 时试 whisper.cpp，钉 doubao 就一路踢到
    faster-whisper —— 冒烟 4/4 因此卡在下载 model.bin 上 7 分钟没返回。"""
    from server import asr as asr_mod
    picked = []
    monkeypatch.setattr(asr_mod.ASR, "_try_load_whispercpp",
                        lambda self: picked.append("cpp") or True)
    monkeypatch.setattr(asr_mod.ASR, "_load_faster_whisper",
                        lambda self: picked.append("faster"))
    for backend in ("auto", "qwen", "doubao", "sherpa", "whispercpp"):
        picked.clear()
        asr_mod.ASR(_asr_cfg(asr_backend=backend)).load()
        assert picked == ["cpp"], backend
    picked.clear()
    asr_mod.ASR(_asr_cfg(asr_backend="faster")).load()
    assert picked == ["faster"]      # 显式点名 faster 才跳过 whisper.cpp


def test_faster_whisper_uses_complete_local_snapshot(monkeypatch, tmp_path):
    """传模型名只走 hub 缓存布局，那里少一个 model.bin 就整份重下；
    download_root 下已有完整目录时必须按本地目录加载。"""
    import types
    from server import asr as asr_mod
    seen = []

    class FakeWhisper:
        def __init__(self, model, **kw):
            seen.append(model)

        def transcribe(self, *_a, **_kw):
            return [], None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisper
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)

    snap = tmp_path / "faster-whisper-large-v3"
    cfg = _asr_cfg(asr_backend="faster", asr_download_root=str(tmp_path), asr_model="large-v3")
    asr_mod.ASR(cfg).load()
    assert seen == ["large-v3"]                    # 目录不存在 → 按原名走缓存/下载

    snap.mkdir()
    (snap / "model.bin").write_bytes(b"x")
    seen.clear()
    asr_mod.ASR(cfg).load()
    assert seen == [str(snap)]


def test_asr_label_matches_the_tier_actually_used():
    """hello/health 回显的档位必须是真会用的那一档：cfg.asr_model 只是 faster-whisper 的档位名，
    线上实配豆包时网页状态条显示的是 "ASR large-v3"，照它排查必然走错方向。"""
    from server.asr import ASR
    from server.session import Session

    class _Doubao:                 # asr_label 只按"这档在不在"判断，不需要真实现
        pass

    cfg = _asr_cfg()
    batch = ASR(cfg)
    s = Session(cfg, batch, None, None, asyncio.Queue())
    assert s.asr_label() == cfg.asr_model                      # 未加载 → 回落配置名
    batch._backend = "whispercpp"
    assert s.asr_label() == "whisper.cpp:" + Path(cfg.asr_ggml_model).name
    batch._backend = "faster"
    assert s.asr_label() == "faster-whisper:" + cfg.asr_model
    s.doubao_asr = _Doubao()
    assert s.asr_label() == "doubao"                           # 与 _run_turn 的取用序一致
