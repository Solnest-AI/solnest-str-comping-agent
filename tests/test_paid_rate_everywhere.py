"""No decision or sentence may rest on AirROI's ttm_avg_rate.

Measured 2026-09-25 on 30 comps against /listings/metrics/all monthly sums:
ttm_avg_rate missed the rate guests actually paid by -13.8% to +18.9%, 9 of
30 beyond 5%. The comp cards were moved off it the same day. This file moves
everything else: the comp scorer's price-tier match and luxury gate, the
rescue pass, the narrative brief, the template copy's price range and the
revenue sanity check.

The rate paid is room revenue / nights booked, and room revenue is
ttm_revpar x ttm_total_days (within $18 of the monthly sums on 30/30).
"""

from __future__ import annotations

import pytest

from adapters.airroi_to_comp import (
    map_for_scorer,
    subject_for_scorer,
    subject_performance_from_listing,
    to_comp_property,
)
from comp_scorer import detect_subject_signals, score_comp
from generators.narrative_brief import _comp_row, _own_performance, nightly_rate_stats
from generators.narratives import template_narratives
from schema import CompProperty, PropertyBasics, SubjectPerformance
from validators.sanity import _check_revenue_sanity


def _record(revpar, adr_field, *, revenue=58_856.0, booked=211, total=365, blocked=16):
    """An AirROI record shaped like Four Corners listing 32294412, whose
    ttm_avg_rate ($305.90) was 19% above the $257.30 actually paid."""
    return {
        "listing_info": {"listing_id": 32294412, "listing_name": "3 bds townhouse"},
        "property_details": {"bedrooms": 3, "baths": 2.5, "guests": 8},
        "ratings": {"rating_overall": 4.8, "num_reviews": 150},
        "performance_metrics": {
            "ttm_revenue": revenue, "ttm_avg_rate": adr_field, "ttm_revpar": revpar,
            "ttm_total_days": total, "ttm_blocked_days": blocked,
            "ttm_days_reserved": booked, "ttm_available_days": total - booked,
            "ttm_occupancy": booked / total,
            "ttm_adjusted_occupancy": booked / (total - blocked),
        },
    }


# ── The rate paid, derived once ──

def test_comp_nightly_rate_is_room_revenue_over_nights():
    comp = to_comp_property(map_for_scorer(_record(148.7, 305.9)))
    assert comp.nightly_rate == pytest.approx(148.7 * 365 / 211)     # $257.23
    assert abs(comp.nightly_rate - 257.30) < 0.10                    # paid, per monthly sums


def test_no_room_revenue_means_no_rate_not_the_adr_field():
    comp = to_comp_property(map_for_scorer(_record(None, 305.9)))
    assert comp.nightly_rate is None


def test_subject_performance_carries_the_rate_paid():
    sp = subject_performance_from_listing(_record(148.7, 305.9))
    assert sp.nightly_rate == pytest.approx(148.7 * 365 / 211)


# ── Scorer ──

def _scorer_comp(nightly_rate, adr_field):
    m = map_for_scorer(_record(148.7, adr_field))
    m["nightly_rate"] = nightly_rate
    return m


def _score(comp, subject_adr=260.0):
    subject = {"title": "Townhouse", "amenities": [], "bedrooms": 3, "max_guests": 8,
               "adr": subject_adr}
    return score_comp(dict(comp), subject_signals=detect_subject_signals(subject),
                      subject_adr=subject_adr, subject_bedrooms=3, subject_guests=8)


def test_scorer_ignores_the_adr_field():
    a = _score(_scorer_comp(257.3, adr_field=305.9))
    b = _score(_scorer_comp(257.3, adr_field=9_999.0))
    assert a["score"] == b["score"]
    assert a["category_scores"] == b["category_scores"]


def test_scorer_price_match_uses_the_rate_paid():
    near = _score(_scorer_comp(257.3, adr_field=305.9))     # 1% from $260
    far = _score(_scorer_comp(500.0, adr_field=258.0))      # field says near, paid says far
    assert near["category_scores"]["financial"] > far["category_scores"]["financial"]


def test_subject_rate_for_scoring_is_its_own_paid_rate_when_stabilized():
    sp = SubjectPerformance(annual_revenue=20_121, occupancy_pct=23.6, adr=223,
                            nights_booked=82, nights_listed=348, months_with_data=12,
                            room_revenue=18_041)
    prop = PropertyBasics(address="a", short_address="a", market="m", bedrooms=3,
                          bathrooms=2.5, max_guests=10, subject_performance=sp)
    assert subject_for_scorer(prop, {"average_daily_rate": 233.3})["adr"] == pytest.approx(18_041 / 82)


@pytest.mark.parametrize("sp", [
    None,
    SubjectPerformance(annual_revenue=40_000, occupancy_pct=20, adr=700, nights_booked=49,
                       nights_listed=245, months_with_data=3, room_revenue=35_000),
])
def test_subject_without_a_stabilized_year_uses_the_estimate(sp):
    prop = PropertyBasics(address="a", short_address="a", market="m", bedrooms=3,
                          bathrooms=3, max_guests=8, subject_performance=sp)
    assert subject_for_scorer(prop, {"average_daily_rate": 393.0})["adr"] == 393.0


# ── Brief and copy ──

def _comp(rate_room, nights, adr_field):
    return CompProperty(name="C", sleeps=8, bedrooms=3, bathrooms=2.5, rating=4.8,
                        review_count=50, annual_revenue=rate_room * 1.15,
                        occupancy_pct=60, adr=adr_field, nights_booked=nights,
                        nights_listed=365, room_revenue=rate_room)


def test_brief_comp_row_shows_the_rate_paid_and_not_the_field():
    row = _comp_row(1, _comp(54_275, 211, adr_field=305.9))
    assert "adr" not in row
    assert row["nightly_rate"] == round(54_275 / 211)


def test_brief_rate_summary_uses_rates_paid():
    comps = [_comp(54_275, 211, 305.9), _comp(87_235, 198, 392.6), _comp(62_123, 279, 222.2)]
    s = nightly_rate_stats(comps)
    assert s["low"] == pytest.approx(62_123 / 279)
    assert s["high"] == pytest.approx(87_235 / 198)


def test_brief_subject_rate_is_the_rate_paid():
    sp = SubjectPerformance(annual_revenue=20_121, occupancy_pct=23.6, adr=223,
                            nights_booked=82, nights_listed=348, months_with_data=12,
                            room_revenue=18_041)
    prop = PropertyBasics(address="a", short_address="a", market="m", bedrooms=3,
                          bathrooms=2.5, max_guests=10, subject_performance=sp)
    own = _own_performance(prop, {"median": 60})
    assert "adr" not in own
    assert own["nightly_rate"] == round(18_041 / 82)


def test_template_price_range_uses_rates_paid():
    comps = [_comp(54_275, 211, 305.9), _comp(87_235, 198, 392.6)]
    prop = PropertyBasics(address="a", short_address="a", market="Four Corners",
                          bedrooms=3, bathrooms=2.5, max_guests=8)
    card = template_narratives(prop, comps).positioning_cards[2].text
    assert "$257" in card and "$441" in card        # 54,275/211 and 87,235/198
    assert "$306" not in card and "$393" not in card


# ── Sanity ──

def test_revenue_sanity_uses_room_revenue_not_the_field():
    """An ADR field 70% too high used to fail this gate on correct data."""
    comp = _comp(54_275, 211, adr_field=305.9 * 1.7)
    comp.annual_revenue = 58_856
    assert _check_revenue_sanity(comp) is None


def test_revenue_sanity_still_catches_revenue_below_room_revenue():
    comp = _comp(54_275, 211, adr_field=257.0)
    comp.annual_revenue = 20_000
    assert _check_revenue_sanity(comp) is not None
