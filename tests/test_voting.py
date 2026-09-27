import pytest

from waggle_dance.voting import VoteError, borda, parse_ballot, parse_candidates, tally_text

C = ["Alpha", "Beta", "Gamma"]


def test_parse_ballot_accepts_code_fence_and_case():
    text = '```json\n{"ranking": ["beta", "Alpha", "GAMMA"], "reason": "Beta is cheaper."}\n```'
    assert parse_ballot(text, C) == (["Beta", "Alpha", "Gamma"], "Beta is cheaper.")


def test_parse_ballot_accepts_numbers_from_the_prompt():
    assert parse_ballot('{"ranking": ["2", "1. Alpha"]}', C) == (["Beta", "Alpha"], "")


@pytest.mark.parametrize(
    "text,match",
    [
        ("no json here", "no JSON"),
        ('{"ranking": "Alpha"}', "list"),
        ('{"ranking": ["Alpha", "Delta"]}', "Delta"),
        ('{"ranking": ["Alpha", "alpha"]}', "more than once"),
    ],
)
def test_bad_ballots_raise(text, match):
    with pytest.raises(VoteError, match=match):
        parse_ballot(text, C)


def test_parse_candidates_dedupes_and_limits():
    text = '{"candidates": ["A", "a", "B", "C", "D", "E", "F", "G", "H", "I"]}'
    assert parse_candidates(text) == ["A", "B", "C", "D", "E", "F", "G", "H"]
    with pytest.raises(VoteError):
        parse_candidates('{"candidates": ["only one"]}')


def test_borda_points_and_order():
    rows = borda(C, {"m1": ["Alpha", "Beta", "Gamma"], "m2": ["Beta", "Alpha", "Gamma"], "m3": ["Alpha", "Gamma", "Beta"]})
    assert [(r.candidate, r.points, r.place) for r in rows] == [("Alpha", 5, 1), ("Beta", 3, 2), ("Gamma", 1, 3)]
    assert rows[0].ranks == {"m1": 1, "m2": 2, "m3": 1}


def test_borda_ties_share_a_place_and_keep_candidate_order():
    rows = borda(C, {"m1": ["Alpha", "Beta", "Gamma"], "m2": ["Beta", "Alpha", "Gamma"]})
    assert [(r.candidate, r.points, r.place) for r in rows] == [("Alpha", 3, 1), ("Beta", 3, 1), ("Gamma", 0, 3)]


def test_partial_ballot_scores_unranked_as_zero():
    rows = borda(C, {"m1": ["Gamma"]})
    assert {r.candidate: r.points for r in rows} == {"Gamma": 2, "Alpha": 0, "Beta": 0}
    assert next(r for r in rows if r.candidate == "Alpha").ranks == {"m1": None}


def test_dropped_vote_is_left_out_of_the_tally_and_reported():
    ballots = {"m1": ["Alpha", "Beta", "Gamma"]}
    rows = borda(C, ballots)
    assert all(set(r.ranks) == {"m1"} for r in rows)
    text = tally_text(rows, ballots, {"m1": "why"}, {"m1": "One", "m2": "Two"}, {"m2": "invalid JSON"})
    assert "1. Alpha: 2 points (One #1)" in text
    assert "Two's vote was dropped: invalid JSON" in text
