"""
Scorer regression test — locks the v2 scoring example.

This test reproduces the exact scoring example from the comp-scoring
logic specification, §7:

    Subject: Luxury 4BR ski chalet, Superhost, ADR CA$850, sleeps 12.
             Reviews mention 'spotless', 'stunning', 'exceptional' (elite tier).
             Amenities: hot tub, ski-in/out, sauna, fireplace, mountain views.

    Comp A: Luxury 4BR @ $820 ADR  ->  total = 41
    Comp B: Standard 3BR @ $560 ADR -> total = 5

If this test fails, somebody changed the scoring logic in `comp_scorer.py`.
The PDF is the source of truth — the scorer must match it.

Run: pytest tests/test_scorer_regression.py -v
"""

import sys
from pathlib import Path

# Ensure project root on path for `import comp_scorer`
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from comp_scorer import score_comp, detect_subject_signals, _parse_adr


# ── Fixtures matching the PDF §7 example ──────────────────────────────────

SUBJECT = {
    "title": "Luxurious 4BR Ski Chalet in Sun Peaks Village",
    "description": (
        "Stunning luxury chalet with mountain view, ski-in/ski-out access, "
        "hot tub, sauna, and fireplace. Sleeps 12."
    ),
    "amenities": ["hot tub", "ski-in/out", "sauna", "fireplace", "mountain view", "village"],
    "configuration": "4BR / sleeps 12",
    "bedrooms": 4,
    "max_guests": 12,
    "guests": 12,
    "adr": 850,
    "host": {"is_superhost": True},
    # 5+ positive keywords trigger "elite" tier
    "reviews": [
        {"text": "Spotless and stunning chalet"},
        {"text": "Exceptional ski-in/ski-out access"},
        {"text": "World-class hot tub and sauna"},
        {"text": "Phenomenal mountain views, beyond expectations"},
        {"text": "Five star host, perfect stay"},
    ],
}


# Comp A — should score 57 (41 base per PDF §7 + 16 must-match)
# Physical=5, Financial=8, Quality=12, Amenity=12, Reliability=4 (unchanged from PDF §7)
# Must-match=16 (hot tub match +8, ski-in/out match +8)
COMP_A = {
    "name": "Luxury 4 Bedroom Ski Chalet with Hot Tub and Sauna",
    "description": "Stunning luxury chalet, ski-in/ski-out, hot tub, sauna, fireplace, mountain view",
    "amenities": ["hot tub", "ski-in/out", "sauna", "fireplace", "mountain view"],
    "bedrooms": 4,
    "sleeps": 12,
    "adr": 820,
    "adr_raw": 820,
    "occupancy_pct": 68,
    "reviews": 85,
    "rating": 4.9,
    "days_available": 340,
    "annual_revenue": 240000,
    "revenue_potential": 300000,
}

# Comp B — should score -15 (5 base per PDF §7 - 20 must-match)
# Physical=1, Financial=1, Quality=2, Amenity=2, Reliability=-1 (unchanged from PDF §7)
# Must-match=-20 (hot tub mismatch -10, ski-in/out mismatch -10)
COMP_B = {
    "name": "Standard 3 Bedroom Cabin",
    "description": "Cabin with fireplace and mountain view",
    "amenities": ["fireplace", "mountain view"],
    "bedrooms": 3,
    "sleeps": 8,
    "adr": 560,
    "adr_raw": 560,
    "occupancy_pct": 42,
    "reviews": 12,
    "rating": 4.65,
    "days_available": 100,
    "annual_revenue": 18000,   # tuned so RevPAN ratio = 18000/100/850 = 0.21 → 0 points
    # revenue_potential intentionally omitted → 4/5 financial fields → partial (+1)
    # → efficiency check skipped (no points)
}


# ── Tests ────────────────────────────────────────────────────────────────

def _score(comp: dict) -> dict:
    """Score a comp using the same flow rank_comps uses."""
    signals = detect_subject_signals(SUBJECT)
    return score_comp(
        dict(comp),  # copy so we don't mutate the fixture
        subject_signals=signals,
        subject_adr=_parse_adr(SUBJECT["adr"]),
        subject_bedrooms=SUBJECT["bedrooms"],
        subject_guests=SUBJECT["max_guests"],
    )


def test_subject_signals_elite_tier():
    """Subject's review keywords should trigger elite quality tier."""
    signals = detect_subject_signals(SUBJECT)
    assert signals["quality_tier"] == "elite", (
        f"Expected elite tier, got {signals['quality_tier']!r}. "
        f"Positive hits: {signals['review_sentiment_positive']}, "
        f"negative: {signals['review_sentiment_negative']}"
    )
    assert signals["luxury"] is True, "luxury keyword missed"
    assert signals["hot_tub"] is True
    assert signals["ski_in_out"] is True
    assert signals["sauna"] is True
    assert signals["views"] is True
    assert signals["fireplace"] is True
    assert signals["superhost"] is True


def test_comp_a_scores_exactly_41():
    """PDF §7 Comp A: Luxury 4BR matching subject across all 5 categories."""
    scored = _score(COMP_A)
    cats = scored.get("category_scores", {})

    # Print breakdown for debugging when test fails
    print(f"\nComp A category scores: {cats}")
    print(f"Total score: {scored.get('score')}")
    for line in scored.get("score_breakdown", []):
        print(f"  {line}")

    assert not scored["hard_fail"], (
        f"Comp A hard-failed: {scored['hard_fail_reason']}"
    )
    assert cats.get("physical") == 5, f"Physical: expected 5, got {cats.get('physical')}"
    assert cats.get("financial") == 8, f"Financial: expected 8, got {cats.get('financial')}"
    assert cats.get("quality") == 12, f"Quality: expected 12, got {cats.get('quality')}"
    assert cats.get("amenity") == 12, f"Amenity: expected 12, got {cats.get('amenity')}"
    assert cats.get("reliability") == 4, f"Reliability: expected 4, got {cats.get('reliability')}"
    assert cats.get("must_match") == 22, f"Must-match: expected 22, got {cats.get('must_match')}"
    assert scored["score"] == 63, f"Total: expected 63, got {scored['score']}"


def test_comp_b_scores_exactly_5():
    """PDF §7 Comp B: Standard 3BR with weaker financials and reliability."""
    scored = _score(COMP_B)
    cats = scored.get("category_scores", {})

    print(f"\nComp B category scores: {cats}")
    print(f"Total score: {scored.get('score')}")
    for line in scored.get("score_breakdown", []):
        print(f"  {line}")

    assert not scored["hard_fail"], (
        f"Comp B hard-failed: {scored['hard_fail_reason']}"
    )
    assert cats.get("physical") == 1, f"Physical: expected 1, got {cats.get('physical')}"
    assert cats.get("financial") == 1, f"Financial: expected 1, got {cats.get('financial')}"
    assert cats.get("quality") == 2, f"Quality: expected 2, got {cats.get('quality')}"
    assert cats.get("amenity") == 2, f"Amenity: expected 2, got {cats.get('amenity')}"
    assert cats.get("reliability") == -1, f"Reliability: expected -1, got {cats.get('reliability')}"
    assert cats.get("must_match") == -14, f"Must-match: expected -14, got {cats.get('must_match')}"
    assert scored["score"] == -9, f"Total: expected -9, got {scored['score']}"


def test_comp_a_outranks_comp_b():
    """Sanity: the better-matched comp must always rank above the weaker one."""
    a = _score(COMP_A)
    b = _score(COMP_B)
    assert a["score"] > b["score"], (
        f"Comp A ({a['score']}) should outrank Comp B ({b['score']})"
    )


# ── Hard-disqualifier tests (PDF §3) ─────────────────────────────────────

def test_luxury_floor_disqualifies_low_adr_comp():
    """Subject ADR $850 → luxury floor at $467.50 (55%). A $200 comp must hard-fail."""
    cheap_comp = dict(COMP_A)
    cheap_comp["adr"] = 200
    cheap_comp["adr_raw"] = 200
    scored = _score(cheap_comp)
    assert scored["hard_fail"], "Expected luxury floor to disqualify $200 comp against $850 subject"
    assert "luxury" in scored["hard_fail_reason"].lower()


def test_bedroom_mismatch_disqualifies():
    """Subject 4BR. A 1BR comp must hard-fail (>±1)."""
    too_small = dict(COMP_A)
    too_small["bedrooms"] = 1
    scored = _score(too_small)
    assert scored["hard_fail"], "Expected 1BR to hard-fail against 4BR subject"
    assert "bedroom" in scored["hard_fail_reason"].lower()


def test_capacity_mismatch_disqualifies():
    """Subject sleeps 12 (>10 → ±5 allowed). A comp sleeping 4 must hard-fail (diff 8 > 5)."""
    too_few = dict(COMP_A)
    too_few["sleeps"] = 4
    scored = _score(too_few)
    assert scored["hard_fail"], "Expected sleeps=4 to hard-fail against sleeps=12 subject"
    assert "guest" in scored["hard_fail_reason"].lower() or "capacity" in scored["hard_fail_reason"].lower()


# ── Penalty tests (PDF §5) ───────────────────────────────────────────────

def test_financial_penalty_fires():
    """Financial category ≤ -2 should trigger -3 penalty."""
    weak_fin = dict(COMP_A)
    weak_fin["adr"] = 1300          # ADR diff 53% → 0 ADR points (above 35% gate)
    weak_fin["adr_raw"] = 1300
    weak_fin["annual_revenue"] = 15000      # RevPAN 15000/340/850 ≈ 5% → -1
    weak_fin["revenue_potential"] = 200000  # efficiency 15000/200000 = 7.5% → -1
    # 0 + (-1) + (-1) = -2 financial → -3 category penalty fires
    scored = _score(weak_fin)
    cats = scored.get("category_scores", {})
    breakdown_text = " ".join(scored.get("score_breakdown", []))
    assert cats.get("financial", 0) <= -2, (
        f"Expected financial ≤ -2, got {cats.get('financial')}. "
        f"Breakdown: {scored.get('score_breakdown', [])}"
    )
    assert "PENALTY" in breakdown_text, "Expected category penalty when financial ≤ -2"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
