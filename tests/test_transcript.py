from waggle_dance.transcript import Entry, apply_style_filters, build_view, rotate

NAMES = {"claude": "Claude", "gemini": "Gemini"}


def entries():
    return [
        Entry(seq=1, speaker="claude", kind="model", phase="opening", round=0, text="claude opening"),
        Entry(seq=2, speaker="gemini", kind="model", phase="opening", round=0, text="gemini opening"),
        Entry(seq=3, speaker="owner", kind="owner", phase="owner", round=0, text="why?"),
        Entry(seq=4, speaker="moderator", kind="system", phase="vote", round=0, text="tally"),
    ]


def test_own_turns_are_assistant_and_others_are_prefixed():
    msgs, _ = build_view("claude", "SUBMISSION", entries(), "DO IT", NAMES, "Michael")
    assert msgs[0] == {"role": "user", "content": "SUBMISSION"}
    assert msgs[1] == {"role": "assistant", "content": "claude opening"}
    assert msgs[2] == {"role": "user", "content": "[Gemini]: gemini opening"}
    assert msgs[3] == {"role": "user", "content": "[Michael]: why?"}
    assert msgs[4] == {"role": "user", "content": "[Moderator]: tally"}
    assert msgs[-1] == {"role": "user", "content": "DO IT"}


def test_other_model_sees_claude_prefixed():
    msgs, _ = build_view("gemini", "S", entries(), "I", NAMES, "Michael")
    assert msgs[1] == {"role": "user", "content": "[Claude]: claude opening"}
    assert msgs[2] == {"role": "assistant", "content": "gemini opening"}


def test_breakpoints_mark_submission_and_transcript_end():
    msgs, bps = build_view("claude", "S", entries(), "I", NAMES, "Michael")
    assert bps == [0, len(msgs) - 2]
    _, bps_empty = build_view("claude", "S", [], "I", NAMES, "Michael")
    assert bps_empty == [0]


def test_rotation_changes_the_first_speaker_each_round():
    models = ["claude", "chatgpt", "gemini", "muse"]
    firsts = [rotate(models, r)[0] for r in range(4)]
    assert firsts == models
    assert rotate(models, 5) == ["chatgpt", "gemini", "muse", "claude"]
    assert rotate([], 3) == []


def test_style_filters_apply_in_order():
    filters = [{"pattern": r"\s*\u2014\s*", "replace": ", "}, {"pattern": "colour", "replace": "color"}]
    assert apply_style_filters("A \u2014 B colour", filters) == "A, B color"
    assert apply_style_filters("unchanged", []) == "unchanged"


def test_today_text_uses_tz(monkeypatch):
    from datetime import datetime, timezone

    from waggle_dance.config import today_text

    moment = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    assert today_text(moment) == "Saturday, September 26, 2026"
    monkeypatch.delenv("TZ")
    assert today_text(moment) == "Sunday, September 27, 2026"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert today_text(moment) == "Sunday, September 27, 2026"


def test_rules_state_the_date():
    from waggle_dance.prompts import discussion_rules

    rules = discussion_rules("Claude", ["Gemini"], "Michael", 1000, False, today="Sunday, September 27, 2026")
    assert "Today's date is Sunday, September 27, 2026." in rules
