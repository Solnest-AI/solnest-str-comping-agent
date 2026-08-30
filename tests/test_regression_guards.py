"""
Permanent regression guards for the AirROI comping pipeline.

Every test in this file FAILS against the pre-2026-08-29 code. That is the
point. They are cheap, they run against 150 real AirROI records, and each one
is aimed at a specific way the tool was wrong while the old suite was 50/50
green:

  a. every performance_metrics fixture satisfies the AirROI night invariants
  b. corr(occupancy, reliability score) over the live corpus is POSITIVE
     (it was -0.910: the scorer paid a bonus for sitting empty)
  c. a high-occupancy comp outscores a barely-booked one
  d. every comp's revenue_potential >= its annual_revenue
     (62 of 150 live cards printed a "potential" below the actual above it)
  g. the Phase A revenue gate rejects 0 of the 150 live listings
     (it rejected 37, and blocked report generation in 5 of 6 markets)

Run: pytest tests/test_regression_guards.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from _fixtures import MARKETS, all_live_listings, market_listings, pearson

from adapters.airroi_to_comp import (
    map_for_scorer, map_batch_for_scorer, to_comp_property,
)
from comp_scorer import _score_data_reliability, score_comp, detect_subject_signals
from validators.sanity import _check_comp_fields, _check_revenue_sanity


LIVE = all_live_listings()

# AirROI reports occupancy ratios rounded to 3 decimals.
_ROUNDING_TOL = 5e-4


def test_corpus_size():
    """6 markets x 25 comparables. If this changes, the thresholds below shift."""
    assert len(LIVE) == 150
    for m in MARKETS:
        assert len(market_listings(m)) == 25, m


# ── (a) AirROI night-accounting invariants ───────────────────────────────

def test_every_live_fixture_satisfies_airroi_night_invariants():
    """available == total - reserved, and occupancy == reserved / 365.

    Verified 200/200 on the live corpus. These two identities are what make
    `ttm_available_days` UNSOLD nights rather than open inventory. Any fixture
    anywhere in this suite that violates them is describing a listing that
    cannot exist — which is exactly how `available=350 + reserved=215` (565
    nights in a 365-day year) sat in tests/test_adapter.py for months.
    """
    problems = []
    for listing in LIVE:
        pm = listing.get("performance_metrics") or {}
        name = (listing.get("listing_info") or {}).get("listing_id")
        total = pm["ttm_total_days"]
        reserved = pm["ttm_days_reserved"]
        available = pm["ttm_available_days"]
        blocked = pm["ttm_blocked_days"]

        if total != 365:
            problems.append(f"{name}: ttm_total_days={total}, expected 365")
        if available != total - reserved:
            problems.append(
                f"{name}: available={available} != total({total}) - reserved({reserved})")
        # AirROI rounds the occupancy ratios to 3 decimals, so these two
        # identities hold to within half a ulp (max observed 4.93e-4).
        if abs(pm["ttm_occupancy"] - reserved / 365) > _ROUNDING_TOL:
            problems.append(
                f"{name}: ttm_occupancy={pm['ttm_occupancy']} != {reserved}/365")
        open_nights = total - blocked
        if open_nights > 0 and abs(pm["ttm_adjusted_occupancy"] - reserved / open_nights) > _ROUNDING_TOL:
            problems.append(
                f"{name}: ttm_adjusted_occupancy != reserved/(total-blocked)")
        if pm["l90d_available_days"] != pm["l90d_total_days"] - pm["l90d_days_reserved"]:
            problems.append(f"{name}: l90d available != total - reserved")

    assert problems == [], problems[:10]


def test_available_days_is_high_exactly_when_occupancy_is_low():
    """The semantic that broke everything, stated as a test.

    A HIGH ttm_available_days means the listing barely booked. Anyone reading
    it as "days the listing was live" gets the ranking exactly backwards.
    """
    occ, avail = [], []
    for listing in LIVE:
        pm = listing["performance_metrics"]
        occ.append(pm["ttm_occupancy"])
        avail.append(pm["ttm_available_days"])
    r = pearson(occ, avail)
    assert r < -0.99, f"expected a near-perfect NEGATIVE correlation, got {r:.3f}"


# ── (b) reliability must reward booking, not idleness ────────────────────

def test_reliability_score_correlates_positively_with_occupancy():
    """corr(adjusted occupancy, reliability points) over 150 live comps > 0.

    Was -0.910. `_score_data_reliability` banded on `ttm_available_days`, so
    the emptier a listing sat the bigger its "Full-time rental +2" bonus. This
    single number is the cheapest permanent guard against reintroducing it.
    """
    mapped = map_batch_for_scorer(LIVE)
    occ = [m["occupancy_pct"] for m in mapped]
    rel = [_score_data_reliability(m, m["nights_booked"])[0] for m in mapped]
    r = pearson(occ, rel)
    print(f"corr(occupancy_pct, reliability_score) = {r:.3f}")
    assert r > 0.30, (
        f"reliability scoring is inverted or uncorrelated (r={r:.3f}). "
        "It must reward NIGHTS BOOKED, never unsold nights."
    )


def test_full_time_rental_bonus_goes_to_the_busiest_comps():
    """The +2 reliability bonus must land on high-occupancy listings."""
    mapped = map_batch_for_scorer(LIVE)
    bonused, plain = [], []
    for m in mapped:
        pts, lines = _score_data_reliability(m, m["nights_booked"])
        (bonused if any("Full-time rental" in l for l in lines) else plain).append(
            m["occupancy_pct"])
    assert bonused, "no comp earned the full-time bonus — banding is broken"
    # 36 of 150 live comps qualify; the quietest of them books 55.6% of its
    # open nights. Under the old (inverted) banding the bonus went to listings
    # with the MOST unsold nights, i.e. the lowest occupancy in the pool.
    assert min(bonused) > 50.0, (
        f"a comp with only {min(bonused):.1f}% occupancy earned the full-time "
        "bonus — the band is reading unsold nights again"
    )
    assert sum(bonused) / len(bonused) > sum(plain) / len(plain), (
        f"bonused mean occ {sum(bonused)/len(bonused):.1f}% vs "
        f"plain {sum(plain)/len(plain):.1f}%"
    )


# ── (c) a busy comp must beat a barely-booked one ────────────────────────

def _twin_comp(booked: int, blocked: int = 15, adr: float = 500.0) -> dict:
    """Two comps identical in every way except how much they booked."""
    listed = 365 - blocked
    return {
        "name": f"Twin comp ({booked} nights booked)",
        "description": "Chalet with hot tub and fireplace",
        "amenities": ["hot tub", "fireplace"],
        "amenities_raw": ["Hot tub", "Indoor fireplace"],
        "bedrooms": 3, "sleeps": 8,
        "reviews": 40, "rating": 4.8, "rating_is_unrated": False,
        "superhost": False, "professional_management": False, "guest_favorite": False,
        "nights_booked": booked,
        "nights_listed": listed,
        "occupancy_pct": round(booked / listed * 100, 2),
        "adr": adr, "adr_raw": adr,
        "annual_revenue": round(adr * booked * 1.19, 2),
        "revenue_potential": round(adr * listed * 0.65 * 1.19, 2),
        "revpar": round(adr * booked / 365, 2),
        "l90d_nights_booked": max(1, round(booked * 90 / 365)),
    }


_TWIN_SUBJECT = {
    "title": "3BR chalet", "description": "Chalet with hot tub and fireplace",
    "amenities": ["Hot tub", "Indoor fireplace"], "configuration": "3BR / sleeps 8",
    "bedrooms": 3, "max_guests": 8, "adr": 500, "host": {}, "reviews": [],
}


def _twin_score(comp: dict) -> dict:
    return score_comp(dict(comp), detect_subject_signals(_TWIN_SUBJECT),
                      500.0, 3, 8)


def test_high_occupancy_comp_outscores_dormant_comp():
    """The headline inversion, as one assertion.

    Everything about these two listings is identical except that one booked
    280 nights and the other booked 80. Under the old scorer the quiet one
    collected the bigger reliability bonus (more "available" days) and could
    outrank the busy one.
    """
    busy = _twin_score(_twin_comp(booked=280))
    quiet = _twin_score(_twin_comp(booked=80))
    print(f"busy={busy['score']} {busy['category_scores']}")
    print(f"quiet={quiet['score']} {quiet['category_scores']}")
    assert not busy["hard_fail"], busy["hard_fail_reason"]
    assert not quiet["hard_fail"], quiet["hard_fail_reason"]
    assert busy["score"] > quiet["score"], (
        f"280-night comp scored {busy['score']}, 80-night comp scored "
        f"{quiet['score']} — occupancy is being scored backwards"
    )
    assert busy["category_scores"]["reliability"] > quiet["category_scores"]["reliability"]


def test_truly_dormant_comp_is_disqualified_not_merely_ranked_low():
    """Below the adjusted-occupancy floor a listing is not a comp at all."""
    dead = _twin_score(_twin_comp(booked=30))   # 8.6% adjusted occupancy
    assert dead["hard_fail"], "a 30-night listing must not reach a client report"


def test_selected_comps_beat_the_pool_on_occupancy():
    """End-to-end: ranking must not systematically prefer quieter listings."""
    from comp_scorer import rank_comps

    mapped = map_batch_for_scorer(market_listings("gatlinburg"))
    subject = {
        "title": "3BR cabin", "description": "Cabin with hot tub",
        "amenities": ["Hot tub"], "configuration": "3BR / sleeps 8",
        "bedrooms": 3, "max_guests": 8,
        "adr": sorted(m["adr"] for m in mapped if m["adr"])[len(mapped) // 2],
        "host": {}, "reviews": [],
    }
    result = rank_comps(subject, mapped, top_n=6)
    selected = result["selected"]
    assert len(selected) == 6
    sel_occ = sum(c["occupancy_pct"] for c in selected) / len(selected)
    pool_occ = sum(m["occupancy_pct"] for m in mapped) / len(mapped)
    assert sel_occ >= pool_occ, (
        f"selected comps average {sel_occ:.1f}% occupancy vs pool {pool_occ:.1f}% — "
        "the scorer is picking the quiet listings"
    )


# ── (d) revenue potential is a ceiling ───────────────────────────────────

def test_every_live_comp_has_potential_at_or_above_actual_revenue():
    """"Revenue Potential" printed below "Annual Revenue" is nonsense to a client.

    62 of 150 live comp cards did exactly that. The pool-level mapper is the
    production entry point (agent.py calls map_batch_for_scorer), so the
    guarantee is enforced there.
    """
    violations = []
    for market in MARKETS:
        for m in map_batch_for_scorer(market_listings(market)):
            pot, actual = m.get("revenue_potential"), m.get("annual_revenue")
            if pot is None or not actual:
                continue
            if pot < actual:
                violations.append(
                    f"{market}/{m['name'][:32]}: potential {pot:,.0f} < actual {actual:,.0f}")
    assert violations == [], f"{len(violations)} of 150: {violations[:8]}"


def test_comp_property_preserves_the_potential_ceiling():
    """The guarantee must survive the CompProperty conversion the template reads."""
    for market in MARKETS:
        for m in map_batch_for_scorer(market_listings(market)):
            cp = to_comp_property(m)
            if cp.annual_revenue <= 0:
                continue
            assert cp.revenue_potential >= cp.annual_revenue, (
                f"{market}/{cp.name[:40]}: {cp.revenue_potential} < {cp.annual_revenue}")


# ── (g) the Phase A gate must not reject real listings ───────────────────

def test_phase_a_revenue_gate_rejects_no_live_listing():
    """0 of 150. It used to reject 37, all of them well-booked properties.

    `_check_revenue_sanity` divided revenue by UNSOLD nights, which made it an
    occupancy filter wearing a data-integrity label: the better a comp
    performed, the more likely it was thrown out. Any comp in the selected six
    tripping it called sys.exit(2), so the tool produced no report at all in 5
    of 6 real markets.
    """
    rejected = []
    for market in MARKETS:
        for m in map_batch_for_scorer(market_listings(market)):
            msg = _check_revenue_sanity(to_comp_property(m))
            if msg:
                rejected.append(f"{market}: {msg}")
    assert rejected == [], f"{len(rejected)} of 150 rejected: {rejected[:8]}"


def test_phase_a_field_checks_reject_no_live_listing_for_night_accounting():
    """No live comp may fail on night accounting or occupancy range.

    Image/URL/review-count gaps are real data gaps and stay in scope for the
    gate; the numeric night fields are ours to get right.
    """
    numeric_keys = ("nights_booked", "nights_listed", "occupancy", "booked nights")
    offenders = []
    for market in MARKETS:
        for m in map_batch_for_scorer(market_listings(market)):
            for err in _check_comp_fields(to_comp_property(m)):
                if any(k in err for k in numeric_keys):
                    offenders.append(f"{market}: {err}")
    assert offenders == [], f"{len(offenders)}: {offenders[:8]}"


# ── Currency ─────────────────────────────────────────────────────────────

def test_currency_resolves_from_country_code_not_a_hardcoded_dollar():
    """Sun Peaks is in BC — its 25 comps are CAD, and the adapter must say so."""
    mapped = [map_for_scorer(c) for c in market_listings("sunpeaks")]
    assert {m["country_code"] for m in mapped} == {"CA"}
    us = [map_for_scorer(c) for c in market_listings("nashville")]
    assert {m["country_code"] for m in us} == {"US"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
