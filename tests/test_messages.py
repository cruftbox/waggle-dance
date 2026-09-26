from waggle_dance.providers.base import (
    TRANSCRIPT_START,
    dedupe_citations,
    normalize_messages,
    normalize_with_breakpoints,
)


def u(text):
    return {"role": "user", "content": text}


def a(text):
    return {"role": "assistant", "content": text}


def roles(msgs):
    return [m["role"] for m in msgs]


def test_alternating_input_is_unchanged():
    msgs = [u("q"), a("r"), u("q2")]
    assert normalize_messages(msgs) == msgs


def test_consecutive_user_messages_merge():
    out = normalize_messages([u("[Gemini]: one"), u("[Michael]: two"), a("mine"), u("next")])
    assert roles(out) == ["user", "assistant", "user"]
    assert out[0]["content"] == "[Gemini]: one\n\n[Michael]: two"


def test_consecutive_assistant_messages_merge():
    out = normalize_messages([u("q"), a("one"), a("two")])
    assert roles(out) == ["user", "assistant"]
    assert out[1]["content"] == "one\n\ntwo"


def test_leading_assistant_gets_a_user_turn_first():
    out = normalize_messages([a("I spoke first"), u("then you")])
    assert roles(out) == ["user", "assistant", "user"]
    assert out[0]["content"] == TRANSCRIPT_START


def test_empty_messages_are_dropped_and_do_not_break_alternation():
    out = normalize_messages([u("q"), a("   "), u("q2")])
    assert out == [u("q\n\nq2")]


def test_input_is_not_mutated():
    msgs = [u("a"), u("b")]
    normalize_messages(msgs)
    assert msgs == [u("a"), u("b")]


def test_unknown_role_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        normalize_messages([{"role": "system", "content": "x"}])


def test_breakpoint_follows_merge():
    # Submission at index 0, merged with the next user message.
    out, marks = normalize_with_breakpoints([u("submission"), u("[Claude]: reply"), a("mine"), u("go")], [0, 3])
    assert roles(out) == ["user", "assistant", "user"]
    assert marks == [0, 2]


def test_breakpoint_shifts_when_transcript_start_is_inserted():
    out, marks = normalize_with_breakpoints([a("mine"), u("theirs")], [1])
    assert out[0]["content"] == TRANSCRIPT_START
    assert marks == [2]


def test_dedupe_citations_keeps_first_and_fills_title():
    out = dedupe_citations(
        [
            {"title": "A", "url": "https://a.example/"},
            {"title": "A again", "url": "https://a.example/"},
            {"title": "", "url": "https://b.example/"},
            {"title": "no url"},
        ]
    )
    assert out == [
        {"title": "A", "url": "https://a.example/"},
        {"title": "https://b.example/", "url": "https://b.example/"},
    ]
