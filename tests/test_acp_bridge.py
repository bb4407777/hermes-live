"""acp_bridge 回归测试（2026-10-06 bug 排查轮）。

钉住的修法：HeadScrub 剥除边界（误杀正文/长回声卡死）、会话映射在 CLI 重拉后
必须清空（否则每轮对死会话发 prompt）、配置三档来源（代码默认→config.yaml→env）。

这些用例的意义在于"钉住"修法：相关代码再被改回原样时，测试要红。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server.acp_bridge as m


def collect(scrub, chunks, flush=True):
    """按序喂入分片，返回桥实际下发的全文（模拟 SSE 拼接）。"""
    out = []
    for c in chunks:
        s = scrub.feed(c)
        if s:
            out.append(s)
    if flush:
        s = scrub.flush()
        if s:
            out.append(s)
    return "".join(out)


class TestHeadScrub:
    def test_plain_reply_untouched(self):
        s = m.HeadScrub()
        text = "这是一段没有任何特殊标记的正常语音回答，请照常播报。"
        assert collect(s, [text]) == text
        assert not s.stripped

    def test_qa_echo_stripped(self):
        s = m.HeadScrub()
        assert collect(s, ["Q：什么是诉讼时效？\nA：", "三年。"]) == "三年。"
        assert s.stripped

    def test_prefix_a_stripped(self):
        s = m.HeadScrub()
        assert collect(s, ["A：", "帮您处理案件杂务。"]) == "帮您处理案件杂务。"
        assert s.stripped

    def test_mid_sentence_answer_marker_kept(self):
        # 「方案A：」在句中且无 Q 前导 → 是正文不是回声，整段放行（曾会误杀前缀）
        s = m.HeadScrub()
        text = "保险方案A：涵盖范围广。"
        assert collect(s, [text]) == text
        assert not s.stripped

    def test_long_q_echo_gives_up_not_hangs(self):
        # Q 回声超过 CAP 且迟迟无答段 → 放弃剥除整段放行（曾会在 CAP=200 处卡死体验）
        s = m.HeadScrub()
        echo = "Q：" + "很长的问题" * 60
        out = collect(s, [echo])
        assert out == echo
        assert not s.stripped

    def test_markers_split_across_chunks(self):
        # 标记被流式分片拆开（"Q" + "：…"）也要识别
        s = m.HeadScrub()
        assert collect(s, ["Q", "：问题\nA：", "答案"]) == "答案"
        assert s.stripped

    def test_after_done_streams_live(self):
        s = m.HeadScrub()
        assert collect(s, ["A：正文"], flush=False) == "正文"
        assert s.feed("后续内容直接透传") == "后续内容直接透传"

    def test_hold_flush_before_markers(self):
        # 前 HOLD 字无标记必须放行（语音首句延迟敏感），后续标记不再回头剥
        s = m.HeadScrub()
        first = "正常语音回复没有标记。" * 4  # 44 字 ≥ HOLD，确定性超阈
        assert len(first) >= m.HeadScrub.HOLD
        assert s.feed(first) == first
        assert s.done


class TestConfigPrecedence:
    def test_yaml_overrides_default(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text(
            "acp_model: glm-5.3-flash\nacp_cli: /x/ai-cli\n", encoding="utf-8")
        monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("ACP_BRIDGE_MODEL", raising=False)
        monkeypatch.delenv("ACP_BRIDGE_CLI", raising=False)
        assert m.load_acp_model() == "glm-5.3-flash"
        assert m.load_acp_cli() == "/x/ai-cli"

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text("acp_model: glm-5.3-flash\n", encoding="utf-8")
        monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("ACP_BRIDGE_MODEL", "deepseek-v4.1-flash")
        assert m.load_acp_model() == "deepseek-v4.1-flash"

    def test_missing_config_uses_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)  # 无 config.yaml
        monkeypatch.delenv("ACP_BRIDGE_MODEL", raising=False)
        assert m.load_acp_model() == "deepseek-v4.1-flash"
