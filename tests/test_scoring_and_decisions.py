from __future__ import annotations

from fractions import Fraction

import pytest
from conftest import module, record


def test_ac5_ec9_bounded_blocking_rejects_overflow_without_top_k() -> None:
    matcher = module("matcher")
    left = [record("l", name="widget", category="tools")]
    right = [record(f"r{i}", name="widget", category="tools") for i in range(21)]
    with pytest.raises(matcher.ResourceBoundError):
        matcher.build_candidates(left, right)


def test_ac6_ec1_ec2_decomposed_scores_are_exact_and_missing_is_null() -> None:
    scoring = module("scoring")
    score = scoring.score_pair(
        record("l", name="Café maker", price="10.00"), record("r", name="maker cafe", price="10.00")
    )
    assert score.name is not None and score.price == Fraction(1, 1)
    assert score.brand is None and score.sku is None
    assert Fraction(0, 1) <= score.total <= Fraction(1, 1)
    assert "brand" in score.missing_components


@pytest.mark.parametrize(
    "reason",
    [
        "equal_top_score",
        "insufficient_bilateral_margin",
        "duplicate_fingerprint",
        "blocked_out_rival",
    ],
)
def test_ac7_ec5_ec6_ec7_ambiguity_never_becomes_match(reason: str) -> None:
    matcher = module("matcher")
    decision = matcher.decide_candidate(
        total=Fraction(9, 10),
        evidence_count=2,
        supported=True,
        contradiction=False,
        admitted=True,
        left_unique=reason != "equal_top_score",
        right_unique=True,
        left_margin=Fraction(7, 100)
        if reason == "insufficient_bilateral_margin"
        else Fraction(1, 1),
        right_margin=Fraction(1, 1),
        duplicate_ambiguous=reason == "duplicate_fingerprint",
        blocked_out_rival=reason == "blocked_out_rival",
    )
    assert decision.value != "MATCH"
    assert reason in decision.explanation.failed_conditions


def test_ec7_blocked_out_high_scoring_rival_prevents_auto_match() -> None:
    matcher = module("matcher")
    scoring = module("scoring")
    left = [record("l", name="abcdefghijklmnop", category="tools", price="100.00")]
    right = [
        record("admitted", name="abcdefghijklmnop", category="tools", price="96.00"),
        record("blocked", name="abcdefghijklmnoq", category="other", price="100.00"),
    ]
    resolved = matcher.resolve_split(left, right, scoring.default_config())
    by_right = {pair.right_id: pair for pair in resolved}
    assert by_right["blocked"].admitted is False
    assert by_right["admitted"].decision.value != "MATCH"
    assert "blocked_out_rival" in by_right["admitted"].decision.explanation.failed_conditions


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (
            record("l", name="Widget", sku="A1"),
            record("r", name="Widget", sku="B2"),
            "sku_disagreement",
        ),
        (
            record("l", name="Widget", brand="Acme"),
            record("r", name="Widget", brand="Zed"),
            "brand_contradiction",
        ),
        (
            record("l", name="Widget", price="1.00"),
            record("r", name="Widget", price="2.00"),
            "price_contradiction",
        ),
    ],
)
def test_ac8_ec8_contradictions_are_explained_and_prevent_match(
    left: dict[str, str], right: dict[str, str], expected: str
) -> None:
    scoring = module("scoring")
    matcher = module("matcher")
    score = scoring.score_pair(left, right)
    assert expected in score.contradictions
    assert matcher.decide_scored_pair(score).value != "MATCH"


def test_ac16_ec17_ec18_exact_fraction_boundary_and_no_evidence_abstain() -> None:
    matcher = module("matcher")
    exact_margin = matcher.decide_candidate(
        total=Fraction(86, 100),
        evidence_count=2,
        supported=True,
        contradiction=False,
        admitted=True,
        left_unique=True,
        right_unique=True,
        left_margin=Fraction(8, 100),
        right_margin=Fraction(1, 1),
        duplicate_ambiguous=False,
        blocked_out_rival=False,
    )
    none = matcher.decide_candidate(
        total=Fraction(0),
        evidence_count=0,
        supported=False,
        contradiction=False,
        admitted=True,
        left_unique=True,
        right_unique=True,
        left_margin=Fraction(1),
        right_margin=Fraction(1),
        duplicate_ambiguous=False,
        blocked_out_rival=False,
    )
    assert exact_margin.value != "MATCH"
    assert none.value == "NO_MATCH" and "no_comparable_components" in none.explanation.reasons


def test_ac21_sequence_ratio_is_exact_ordered_and_no_autojunk() -> None:
    scoring = module("scoring")
    assert scoring.exact_sequence_ratio("a" * 300 + "b", "b" + "a" * 300) == Fraction(600, 602)
    assert isinstance(scoring.exact_sequence_ratio("abc", "cba"), Fraction)
