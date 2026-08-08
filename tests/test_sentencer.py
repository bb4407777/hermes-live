import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sentencer import CODE_PLACEHOLDER, SentenceAssembler


def feed_all(a: SentenceAssembler, deltas):
    out = []
    for d in deltas:
        out += a.feed(d)
    out += a.flush()
    return out


def test_hard_breaks():
    a = SentenceAssembler()
    out = feed_all(a, ["你好。今天", "天气不错！要出门吗？"])
    assert out == ["你好。", "今天天气不错！", "要出门吗？"]


def test_first_sentence_acceleration():
    a = SentenceAssembler(first_min=10)
    # 首句 ≥10 字遇逗号即切
    out = a.feed("这个问题我先说结论，然后再展开细节说")
    assert out and out[0].endswith("，")
    assert "结论" in out[0]


def test_no_acceleration_after_first():
    a = SentenceAssembler(max_buffer=50, first_min=10)
    out = a.feed("先来第一句话完整收尾。")
    out += a.feed("之后短逗号，不该切。")
    out += a.flush()
    assert "，不该切" in out[-1]  # 第二句没有在逗号处被切开


def test_long_buffer_soft_break():
    a = SentenceAssembler(max_buffer=20, first_min=10)
    a.emitted_any = True  # 关掉首句加速，单测软切
    out = a.feed("一二三四五六七八九十一二三四五六七八九十多字了，该切了吧继续")
    assert out and out[0].endswith("，")


def test_markdown_cleanup():
    a = SentenceAssembler()
    out = feed_all(a, ["**重点**：见[文档](https://x.com)和`代码`。"])
    assert out == ["重点：见文档和代码。"]


def test_code_block_replaced():
    a = SentenceAssembler()
    out = feed_all(a, ["示例如下：\n```python\nprint(1)\n```", "结束。"])
    text = "".join(out)
    assert CODE_PLACEHOLDER in text
    assert "print" not in text
    assert "结束。" in text


def test_fence_split_across_deltas():
    a = SentenceAssembler()
    out = feed_all(a, ["代码`", "``\nx=1\n``", "`说完了。"])
    text = "".join(out)
    assert "x=1" not in text
    assert "说完了。" in text


def test_flush_remainder():
    a = SentenceAssembler()
    a.feed("没有结束符的尾巴")
    out = a.flush()
    assert out == ["没有结束符的尾巴"]


def test_emoji_stripped():
    a = SentenceAssembler()
    out = feed_all(a, ["好的😄，马上办🚀。"])
    assert out == ["好的，马上办。"]
