import pytest

from waggle_dance.costs import estimate_calls, format_usd, reply_cost
from waggle_dance.discord_io import split_text
from waggle_dance.providers.base import Reply

PRICES = {"input": 4.0, "output": 20.0, "cached_input": 0.2, "cache_write": 5.0, "per_search": 0.01}


def test_reply_cost():
    r = Reply(text="", input_tokens=1_000_000, output_tokens=100_000, cached_tokens=500_000,
              cache_write_tokens=200_000, search_calls=3)
    # 4.00 + 2.00 + 0.10 + 1.00 + 0.03
    assert reply_cost(PRICES, r) == pytest.approx(7.13)


def test_cache_write_defaults_to_input_price():
    prices = {k: v for k, v in PRICES.items() if k != "cache_write"}
    assert reply_cost(prices, Reply(text="", cache_write_tokens=1_000_000)) == pytest.approx(4.0)


def test_estimate_calls():
    assert estimate_calls({"a": 2, "b": 1}, {"a": 0.5, "b": 0.25}) == pytest.approx(1.25)


def test_format_usd():
    assert format_usd(1.234) == "$1.23"
    assert format_usd(0.0123) == "$0.012"


def test_short_text_is_one_chunk():
    assert split_text("hello") == ["hello"]
    assert split_text("   ") == []


def test_split_at_paragraph_boundaries():
    paras = [("p%d " % i) * 300 for i in range(6)]  # about 1,200 characters each
    text = "\n\n".join(p.strip() for p in paras)
    chunks = split_text(text, 4096)
    assert len(chunks) == 2
    assert all(len(c) <= 4096 for c in chunks)
    # No paragraph is cut in half.
    assert "\n\n".join(chunks) == text


def test_long_paragraph_splits_at_sentences_then_words():
    sentence = "This is one sentence of moderate length. "
    para = (sentence * 200).strip()  # about 8,200 characters, no blank lines
    chunks = split_text(para, 4096)
    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)
    assert all(c.endswith(".") for c in chunks)


def test_unbreakable_text_is_cut_hard():
    chunks = split_text("x" * 5000, 4096)
    assert [len(c) for c in chunks] == [4096, 904]
