"""
Scorer regression test — locks the calibrated scoring for two reference comps.

HISTORY / WHY THE NUMBERS MOVED (2026-08-29)
--------------------------------------------
This file used to lock the worked example from
the original scoring spec at totals 41 and 5. Those totals are no
longer reachable, and the fixtures that produced them were impossible:

  * `COMP_A` had `days_available=340` with `occupancy_pct=68`, `COMP_B` had
    `days_available=100` with `occupancy_pct=42`. Both describe a listing that
    is simultaneously idle and booked. Those two fixtures were the ONLY reason
    the inverted reliability scoring looked correct: the scorer banded on
    `ttm_available_days` (UNSOLD nights) and paid +2 to dormant listings.
  * Since the PDF, the scorer legitimately changed:
      - `days_available` deleted; reliability now bands on NIGHTS BOOKED.
      - hand-rolled "RevPAN = revenue / days_available" replaced by AirROI's
        own `ttm_revpar` (the old one divided revenue by unsold nights).
      - revenue efficiency is now measured against a MARKET-RELATIVE ceiling.
      - the "+3 luxury" amenity award was removed: it fired on 61 of 125 live
        comps purely from host-written marketing adjectives.
      - a sixth category, `distance`, was added.

Everything below is re-derived from those rules, with the arithmetic spelled
out per category. Both fixtures are now internally consistent: every financial
field is computed from `nights_booked`, and
`test_reference_fixtures_are_physically_possible` enforces it.

Run: pytest tests/test_scorer_regression.py -v
"""

import sys
from pathlib import Path

import pytest

# Ensure project root on path for `import comp_scorer`
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from comp_scorer import score_comp, detect_subject_signals, _parse_adr


# Corpus fee multiplier: ttm_revenue / (ttm_avg_rate x nights_booked),
# median 1.192 over 200 live AirROI records.
FEE_FACTOR = 1.19


def _financials(booked: int, blocked: int, adr: float) -> dict:
    """Derive every financial field from nights booked, AirROI-style.

        nights_listed = 365 - blocked                 (open inventory)
        occupancy_pct = booked / nights_listed        (ADJUSTED occupancy)
        annual_revenue = adr x booked x fee factor    (fee-INCLUSIVE)
        revpar        = adr x (booked / 365)          (room RevPAR, fees out)
    """
    listed = 365 - blocked
    assert 0 < booked <= listed <= 365
    return {
        "nights_booked": booked,
        "nights_listed": listed,
        "occupancy_pct": round(booked / listed * 100, 2),
        "adr": adr,
        "adr_raw": adr,
        "annual_revenue": round(adr * booked * FEE_FACTOR, 2),
        "revpar": round(adr * (booked / 365), 2),
    }


# ── Subject: the PDF §7 example, amenities in the real AirROI vocabulary ──

SUBJECT = {
    "title": "Luxurious 4BR Ski Chalet in Sun Peaks Village",
    "description": (
        "Stunning luxury chalet with mountain view, ski-in/ski-out access, "
        "hot tub, sauna, and fireplace. Sleeps 12."
    ),
    # "sauna" and "mountain view" are NOT amenity-vocabulary members — no
    # AirROI listing has them. They reach the scorer as free-text signals.
    "amenities": ["Hot tub", "Ski-in/Ski-out", "Indoor fireplace",
                  "sauna", "mountain view", "village"],
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


# ── Comp A — the strong match. Expected total 38. ────────────────────────
#
# 248 nights booked of 350 open (15 blocked) = 70.9% adjusted occupancy.
#   physical    5  = exact 4BR +3, exact 12 guests +2
#   financial   9  = ADR 820 vs 850, 3.5% gap +3
#                    RevPAR 557 / subject ADR 850 = 0.66 >= 0.55  +3
#                    efficiency 98% of market potential (>=0.90)   +3
#   quality    12  = 85 reviews +3, rating 4.9 with 85 reviews +3,
#                    elite tier match +2, Superhost comp +1,
#                    71% occupancy +3
#   amenity     8  = ski-in/out +3, hot tub +2, fireplace +1  (exact vocabulary
#                    membership) and sauna +2 (free text; no vocabulary key).
#                    NOTE: no "views" point. The subject's views signal comes
#                    from "mountain view", which is not in the 136-string
#                    amenity vocabulary, and exact matching is the whole point.
#   reliability 4  = all 5 financial fields +2, 248 booked nights (>=200) +2
#   distance    0  = neither fixture carries coordinates
COMP_A = {
    "name": "Luxury 4 Bedroom Ski Chalet with Hot Tub and Sauna",
    "description": "Stunning luxury chalet, ski-in/ski-out, hot tub, sauna, fireplace, mountain view",
    "amenities": ["hot tub", "ski-in/out", "fireplace", "mountain view"],
    "amenities_raw": ["Hot tub", "Ski-in/Ski-out", "Indoor fireplace"],
    "bedrooms": 4,
    "sleeps": 12,
    "reviews": 85,
    "rating": 4.9,
    "rating_is_unrated": False,
    "superhost": True,
    "professional_management": False,
    "guest_favorite": False,
    "l90d_nights_booked": 62,
    **_financials(booked=248, blocked=15, adr=820),
}
# The pool-level mapper floors revenue_potential at 1.02x actual so a card can
# never print a ceiling below the revenue directly above it. A comp already
# operating at market p75 lands exactly on that floor.
COMP_A["revenue_potential"] = round(COMP_A["annual_revenue"] * 1.02, 2)


# ── Comp B — the weak match. Expected total 8. ───────────────────────────
#
# 153 nights booked of 335 open (30 blocked) = 45.7% adjusted occupancy.
#   physical    1  = 3BR within +/-1 of 4BR +1; sleeps 8 vs 12, diff 4 > 2 -> 0
#   financial   2  = ADR 560 vs 850, 34% gap (<=35%) +1
#                    RevPAR 235 / 850 = 0.28 (>=0.25) +1
#                    efficiency skipped — revenue_potential deliberately absent
#   quality     2  = 12 reviews +1; rating 4.65 is below the 4.7 band and above
#                    the 4.3 penalty -> 0; no elite tier match; 46% occupancy +1
#   amenity     1  = fireplace +1 only
#   reliability 2  = 4 of 5 financial fields +1, 153 booked nights (>=120) +1
#                    (was -1 when the fixture claimed 100 UNSOLD nights)
#   distance    0
COMP_B = {
    "name": "Standard 3 Bedroom Cabin",
    "description": "Cabin with fireplace and mountain view",
    "amenities": ["fireplace", "mountain view"],
    "amenities_raw": ["Indoor fireplace"],
    "bedrooms": 3,
    "sleeps": 8,
    "reviews": 12,
    "rating": 4.65,
    "rating_is_unrated": False,
    "superhost": False,
    "professional_management": False,
    "guest_favorite": False,
    "l90d_nights_booked": 30,
    # revenue_potential intentionally omitted -> 4/5 financial fields -> +1
    # -> efficiency check skipped (no points)
    **_financials(booked=153, blocked=30, adr=560),
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


@pytest.mark.parametrize("comp", [COMP_A, COMP_B], ids=["comp_a", "comp_b"])
def test_reference_fixtures_are_physically_possible(comp):
    """Guard the fixtures themselves — this is how the old suite went wrong."""
    assert 0 < comp["nights_booked"] <= comp["nights_listed"] <= 365
    assert comp["occupancy_pct"] == pytest.approx(
        comp["nights_booked"] / comp["nights_listed"] * 100, abs=0.01)
    assert comp["annual_revenue"] == pytest.approx(
        comp["adr"] * comp["nights_booked"] * FEE_FACTOR, rel=1e-6)
    assert "days_available" not in comp, "the field that inverted this tool"
    if comp.get("revenue_potential"):
        assert comp["revenue_potential"] >= comp["annual_revenue"]


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


def test_comp_a_scores_exactly_38():
    """Strong comp: 4BR ski chalet matching the subject across all categories."""
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
    assert cats.get("financial") == 9, f"Financial: expected 9, got {cats.get('financial')}"
    assert cats.get("quality") == 12, f"Quality: expected 12, got {cats.get('quality')}"
    assert cats.get("amenity") == 8, f"Amenity: expected 8, got {cats.get('amenity')}"
    assert cats.get("reliability") == 4, f"Reliability: expected 4, got {cats.get('reliability')}"
    assert cats.get("distance") == 0, f"Distance: expected 0, got {cats.get('distance')}"
    assert scored["score"] == 38, f"Total: expected 38, got {scored['score']}"


def test_comp_a_reliability_rewards_nights_booked_not_idle_nights():
    """248 booked nights must earn the full-time bonus.

    Under the old scorer this comp's fixture said `days_available=340`, and the
    +2 came from having sat EMPTY for 340 nights. Same points, opposite meaning.
    """
    scored = _score(COMP_A)
    line = " ".join(scored["score_breakdown"])
    assert "+2 Full-time rental (248 nights booked)" in line, scored["score_breakdown"]


def test_comp_b_scores_exactly_8():
    """Weak comp: standard 3BR with softer financials and partial data."""
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
    assert cats.get("financial") == 2, f"Financial: expected 2, got {cats.get('financial')}"
    assert cats.get("quality") == 2, f"Quality: expected 2, got {cats.get('quality')}"
    assert cats.get("amenity") == 1, f"Amenity: expected 1, got {cats.get('amenity')}"
    assert cats.get("reliability") == 2, f"Reliability: expected 2, got {cats.get('reliability')}"
    assert scored["score"] == 8, f"Total: expected 8, got {scored['score']}"


def test_comp_a_outranks_comp_b():
    """Sanity: the better-matched comp must always rank above the weaker one."""
    a = _score(COMP_A)
    b = _score(COMP_B)
    assert a["score"] > b["score"], (
        f"Comp A ({a['score']}) should outrank Comp B ({b['score']})"
    )


def test_amenity_points_require_exact_vocabulary_membership():
    """A "mountain view" in the description earns no Views point.

    The subject's views signal is real, but the comp can only match it through
    the amenity vocabulary, and "Mountain view" is not one of the 136 real
    strings. Substring scanning of name+description is what awarded "+2 Pool
    match" to a listing whose description read "we do not have a gym, pool nor
    roof deck".
    """
    scored = _score(COMP_A)
    lines = " ".join(scored["score_breakdown"])
    assert "Views match" not in lines, scored["score_breakdown"]
    assert "+3 Ski-in/out match" in lines
    assert "+2 Hot Tub match" in lines


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


def test_dormant_listing_disqualifies():
    """A listing booking almost nothing is evidence about a failed listing.

    It is not a cheap comp, and it does not belong in a client report.
    """
    from comp_scorer import MIN_ADJUSTED_OCCUPANCY

    dormant = dict(COMP_A)
    dormant.update(_financials(booked=40, blocked=15, adr=820))   # 11.4% adjusted
    dormant["revenue_potential"] = round(dormant["annual_revenue"] * 1.02, 2)
    assert dormant["occupancy_pct"] < MIN_ADJUSTED_OCCUPANCY
    scored = _score(dormant)
    assert scored["hard_fail"], "Expected a dormant listing to be disqualified"
    assert "dormant" in scored["hard_fail_reason"].lower()


# ── Penalty tests (PDF §5) ───────────────────────────────────────────────

def test_financial_penalty_fires():
    """Financial category ≤ -2 should trigger -3 penalty."""
    weak_fin = dict(COMP_A)
    weak_fin["adr"] = 1300          # ADR diff 53% → 0 ADR points (above 35% gate)
    weak_fin["adr_raw"] = 1300
    weak_fin["revpar"] = 100        # 100/850 = 0.12 < 0.15 → -1
    weak_fin["annual_revenue"] = 15000
    weak_fin["revenue_potential"] = 200000  # efficiency 7.5% → -1
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
    sys.exit(pytest.main([__file__, "-v", "-s"]))
