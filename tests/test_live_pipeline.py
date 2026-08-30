"""
End-to-end pipeline test against REAL AirROI responses.

Before this file, zero tests parsed an actual API response. The suite was 50/50
green while the tool exited 2 before writing a report in 5 of 6 real markets,
because every fixture was hand-written to match what the code already believed.

Path exercised here is the one agent.py runs (agent.py:569-755):

    map_batch_for_scorer(raw listings)   ->  scorer input dicts
    rank_comps(subject, mapped)          ->  selected / ranked / hard_fails
    to_comp_property(selected)           ->  the models the template renders

Run: pytest tests/test_live_pipeline.py -v
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from _fixtures import MARKETS, load_json, market_listings

from adapters.airroi_to_comp import (
    map_for_scorer, map_batch_for_scorer, to_comp_property, subject_for_scorer,
)
from comp_scorer import rank_comps
from schema import CompProperty, PropertyBasics
from validators.sanity import _check_comp_fields, _check_revenue_sanity


def _median_int(vals) -> int:
    return int(round(statistics.median(vals)))


def _subject_from_pool(mapped: list[dict], market: str) -> dict:
    """Build a plausible subject sitting in the middle of the real pool.

    Using the pool's own medians means the subject is a property that could
    genuinely exist in this market, so a run that finds no comps is a real
    signal rather than an artefact of a made-up subject.
    """
    beds = _median_int([m["bedrooms"] for m in mapped if m.get("bedrooms") is not None])
    guests = _median_int([m["sleeps"] for m in mapped if m.get("sleeps") is not None])
    lats = [m["latitude"] for m in mapped if m.get("latitude") is not None]
    lons = [m["longitude"] for m in mapped if m.get("longitude") is not None]
    prop = PropertyBasics(
        address=f"1 Test Way, {market.title()}",
        short_address="1 Test Way",
        market=market.title(),
        bedrooms=beds,
        bathrooms=2.0,
        max_guests=guests,
        property_type="Vacation Home",
        title=f"{beds}BR home in {market.title()}",
        amenities=["Hot tub"],
        latitude=statistics.median(lats) if lats else None,
        longitude=statistics.median(lons) if lons else None,
    )
    adr = statistics.median([m["adr"] for m in mapped if m.get("adr")])
    return subject_for_scorer(prop, {"average_daily_rate": adr})


# ── Raw response shape ───────────────────────────────────────────────────

def test_comparables_response_shape():
    """`GET /listings/comparables` -> {listings: [...]} with 25 records."""
    for market in MARKETS:
        body = load_json(f"comps_{market}.json")
        assert set(body) >= {"listings"}, (market, sorted(body))
        assert len(body["listings"]) == 25, market
        first = body["listings"][0]
        assert {"listing_info", "property_details", "performance_metrics",
                "location_info", "ratings", "host_info", "pricing_info"} <= set(first), market


def test_listing_detail_and_metrics_fixtures_parse():
    detail = load_json("listing_detail.json")
    assert "listing_info" in detail or "listing_id" in str(detail)[:200]
    metrics = load_json("metrics.json")
    assert "results" in metrics and isinstance(metrics["results"], list)


# ── map_batch_for_scorer over real data ──────────────────────────────────

@pytest.mark.parametrize("market", MARKETS)
def test_mapping_a_real_market_produces_every_field_the_scorer_reads(market):
    mapped = map_batch_for_scorer(market_listings(market))
    assert len(mapped) == 25

    required_numeric = ("adr", "annual_revenue", "occupancy_pct", "revpar",
                        "nights_booked", "nights_listed")
    for m in mapped:
        for key in required_numeric:
            assert key in m, (market, key)
            assert m[key] is not None, (market, key, m["name"][:30])
        assert 0 <= m["nights_booked"] <= 365
        assert 0 < m["nights_listed"] <= 365
        assert m["nights_booked"] <= m["nights_listed"], (
            f"{market}/{m['name'][:30]}: booked {m['nights_booked']} > "
            f"open {m['nights_listed']}"
        )
        assert 0 <= m["occupancy_pct"] <= 100
        assert isinstance(m["amenities"], list)
        assert isinstance(m["description"], str)
        assert m["rating"] is None or 0 < m["rating"] <= 5


def test_mapping_is_pure_and_repeatable():
    """map_for_scorer must not mutate the caller's listing dict."""
    raw = market_listings("sunpeaks")[0]
    before = load_json("comps_sunpeaks.json")["listings"][0]
    map_for_scorer(raw)
    assert raw["performance_metrics"] == before["performance_metrics"]
    assert raw["property_details"]["amenities"] == before["property_details"]["amenities"]


# ── Full pipeline per market ─────────────────────────────────────────────

@pytest.mark.parametrize("market", MARKETS)
def test_full_pipeline_selects_six_renderable_comps(market):
    """map_batch_for_scorer -> rank_comps -> to_comp_property, on live data.

    Six comps is the report contract (validators.sanity blocks below six).
    """
    mapped = map_batch_for_scorer(market_listings(market))
    subject = _subject_from_pool(mapped, market)

    result = rank_comps(subject, mapped, top_n=6)

    assert result["total_candidates"] == 25
    assert len(result["ranked"]) + len(result["hard_fails"]) == 25
    assert len(result["selected"]) == 6, (
        f"{market}: only {len(result['selected'])} comps survived scoring; "
        f"{len(result['hard_fails'])} hard-failed"
    )

    comps = [to_comp_property(c) for c in result["selected"]]
    assert all(isinstance(c, CompProperty) for c in comps)

    for c in comps:
        assert c.name and c.name != "Unnamed listing"
        assert c.airbnb_url.startswith("https://www.airbnb.com/rooms/")
        assert c.adr > 0
        assert c.annual_revenue > 0
        assert c.nights_booked > 0, "a selected comp must have booked something"
        assert 0 < c.occupancy_pct <= 100
        assert c.revenue_potential >= c.annual_revenue
        assert c.rating is None or 0 < c.rating <= 5
        assert len(c.feature_badges) == len(c.badge_emojis)


@pytest.mark.parametrize("market", MARKETS)
def test_selected_comps_clear_the_phase_a_numeric_gates(market):
    """The gates that used to call sys.exit(2) on healthy comps.

    Only the numeric checks are asserted here: image/URL liveness is a network
    concern and review_count gaps are genuine data gaps in AirROI.
    """
    mapped = map_batch_for_scorer(market_listings(market))
    result = rank_comps(_subject_from_pool(mapped, market), mapped, top_n=6)

    numeric_keys = ("adr", "annual_revenue", "revenue_potential", "occupancy",
                    "nights_booked", "nights_listed", "booked nights",
                    "bedrooms", "bathrooms", "sleeps")
    for scored in result["selected"]:
        cp = to_comp_property(scored)
        numeric_errors = [e for e in _check_comp_fields(cp)
                          if any(k in e for k in numeric_keys)]
        assert numeric_errors == [], (market, numeric_errors)
        assert _check_revenue_sanity(cp) is None, (market, cp.name)


@pytest.mark.parametrize("market", MARKETS)
def test_ranking_is_deterministic(market):
    """Same pool, same subject, same six comps — regardless of input order.

    Score ties used to be resolved by input order, so 18 of 20 input shuffles
    changed the delivered comp set.
    """
    raw = market_listings(market)
    mapped = map_batch_for_scorer(raw)
    subject = _subject_from_pool(mapped, market)
    first = [c["name"] for c in rank_comps(subject, mapped, top_n=6)["selected"]]

    shuffled = list(reversed(raw))
    mapped2 = map_batch_for_scorer(shuffled)
    second = [c["name"] for c in rank_comps(subject, mapped2, top_n=6)["selected"]]

    assert first == second, f"{market}: {first} vs {second}"


def test_hard_fails_carry_a_reason_and_score_zero():
    mapped = map_batch_for_scorer(market_listings("scottsdale"))
    result = rank_comps(_subject_from_pool(mapped, "scottsdale"), mapped, top_n=6)
    for c in result["hard_fails"]:
        assert c["hard_fail"] is True
        assert c["hard_fail_reason"], c["name"]
        assert c["score"] == 0


def test_pipeline_averages_are_on_the_declared_bases():
    """rank_comps averages must use the same fields the comp cards print."""
    mapped = map_batch_for_scorer(market_listings("destin"))
    result = rank_comps(_subject_from_pool(mapped, "destin"), mapped, top_n=6)
    avg = result["averages"]
    selected = result["selected"]

    assert avg["nights_booked"] == pytest.approx(
        sum(c["nights_booked"] for c in selected) / len(selected), abs=0.01)
    assert avg["annual_revenue"] == pytest.approx(
        sum(c["annual_revenue_raw"] for c in selected) / len(selected), abs=0.01)
    assert avg["revenue_potential"] >= avg["annual_revenue"]
    assert 0 < avg["occupancy_pct"] <= 100


def test_studio_subject_disqualifies_an_all_2br_pool():
    """A 0BR subject must not quietly adopt a pool of 2BR houses.

    Nashville's 25 comparables are all 2BR, so the correct outcome is that
    every one of them hard-fails on bedroom count and the run produces no
    report — not that six of them slip through with zero disqualifications.
    """
    mapped = map_batch_for_scorer(market_listings("nashville"))
    assert {m["bedrooms"] for m in mapped} == {2}
    # max_guests=4 keeps every comp inside the guest tolerance (they sleep 6),
    # so the bedroom rule is the one under test and its reason is the one that
    # survives on the failing comps.
    prop = PropertyBasics(
        address="1 Studio Ln, Nashville", short_address="1 Studio Ln",
        market="Nashville", bedrooms=0, bathrooms=1.0, max_guests=4,
        property_type="Studio",
    )
    adr = statistics.median([m["adr"] for m in mapped if m.get("adr")])
    result = rank_comps(subject_for_scorer(prop, {"average_daily_rate": adr}),
                        mapped, top_n=6)
    assert result["selected"] == []
    assert len(result["hard_fails"]) == 25
    assert all("edroom" in c["hard_fail_reason"] for c in result["hard_fails"]), (
        [c["hard_fail_reason"] for c in result["hard_fails"][:3]]
    )


def test_bedroom_tolerance_holds_on_a_mixed_pool():
    """Each captured market is bedroom-homogeneous, so mix two of them.

    2BR subject + a pool of 2BR (Nashville) and 5BR (Scottsdale) listings:
    every 5BR must be disqualified and none may reach the selected six.
    """
    mixed = map_batch_for_scorer(
        market_listings("nashville") + market_listings("scottsdale"))
    assert {m["bedrooms"] for m in mixed} == {2, 5}

    prop = PropertyBasics(
        address="1 Test Way, Nashville", short_address="1 Test Way",
        market="Nashville", bedrooms=2, bathrooms=2.0, max_guests=6,
        property_type="Condo",
    )
    adr = statistics.median([m["adr"] for m in mixed if m["bedrooms"] == 2 and m["adr"]])
    result = rank_comps(subject_for_scorer(prop, {"average_daily_rate": adr}),
                        mixed, top_n=6)

    assert len(result["selected"]) == 6
    assert all(c["bedrooms"] == 2 for c in result["selected"]), (
        [(c["bedrooms"], c["name"][:30]) for c in result["selected"]]
    )
    five_br = [c for c in result["hard_fails"] if c["bedrooms"] == 5]
    assert len(five_br) == 25
    assert all("edroom" in c["hard_fail_reason"] or "uest" in c["hard_fail_reason"]
               for c in five_br)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
