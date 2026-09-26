"""Comp cards must add up on a calculator.

Ryan checked a card by hand on 2026-09-25: "Community Pool! Close to Gburg &
Hiking!" showed Annual Revenue $99.7K, ADR $392 and 198 nights, and 198 x $392
is $77.6K. Tested across 30 comps in 5 markets the same day:

* AirROI's ttm_avg_rate is not the rate guests paid. Against room revenue
  summed from /listings/metrics/all it was off by -13.8% to +18.9% (9 of 30
  beyond 5%). This card: $392.60 shown, $440.67 paid.
* Monthly nights summed to ttm_days_reserved on 30/30, and monthly revenue is
  room-only (sum of revenue / ADR hit the same nights to 0.03).
* ttm_revenue minus that room revenue was never negative (0/30) and never
  above stays x cleaning fee (median 0.88, max 0.97): it is the fees.
* A rate rounded to the dollar times nights missed revenue by up to $120; to
  the cent, by up to $1. Only a division is exact, so the card states the
  rate as total / nights.

Two layouts. With room revenue: room + fees = revenue, rate = room / nights.
Without it (not fetched): revenue / nights, fees included.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import pytest
from bs4 import BeautifulSoup

from report.template_engine import render_report, revenue_breakdown
from schema import CompProperty
from test_report_copy import _data


def _comp(nights: int, revenue: float, room: float | None = None, adr: float = 300) -> CompProperty:
    return CompProperty(
        name="C", sleeps=8, bedrooms=3, bathrooms=2.0, rating=4.8, review_count=50,
        annual_revenue=revenue, occupancy_pct=54.2, adr=adr,
        nights_booked=nights, nights_listed=365, room_revenue=room,
    )


def _calc_cents(total: int, nights: int) -> Decimal:
    """What a calculator shows, rounded half-up to the cent."""
    return (Decimal(total) / nights).quantize(Decimal("0.01"), ROUND_HALF_UP)


# Real (nights, ttm_revenue, monthly room revenue) from the 2026-09-25 test.
REAL = [
    (198, 99_748, 87_253),     # Gatlinburg, the card Ryan checked
    (211, 58_856, 54_292),     # Four Corners, ADR field 19% high
    (233, 101_758, 92_862), (246, 112_395, 102_429), (222, 119_248, 103_438),
    (238, 113_481, 100_791), (194, 75_817, 67_942),
    (147, 148_357, 140_776),   # Sun Peaks, CAD
    (186, 156_944, 156_914),   # fees of $30
]


def test_the_card_ryan_checked():
    b = revenue_breakdown(_comp(198, 99_748, 87_253, adr=392.6))
    assert b["mode"] == "split"
    assert (b["room"], b["fees"], b["total"]) == (87_253, 12_495, 99_748)
    assert b["nightly"] == Decimal("440.67")


@pytest.mark.parametrize("nights,revenue,room", REAL)
def test_split_is_exact_on_real_listings(nights, revenue, room):
    b = revenue_breakdown(_comp(nights, revenue, room))
    assert b["mode"] == "split"
    assert b["room"] + b["fees"] == b["total"] == revenue
    assert b["fees"] >= 0
    assert b["nightly"] == _calc_cents(b["room"], nights)


@pytest.mark.parametrize("nights,revenue,room", REAL)
def test_per_night_is_exact_on_real_listings(nights, revenue, room):
    b = revenue_breakdown(_comp(nights, revenue))
    assert b["mode"] == "per_night"
    assert b["total"] == revenue
    assert b["per_night"] == _calc_cents(revenue, nights)


def test_the_adr_field_is_never_used():
    a = revenue_breakdown(_comp(198, 99_748, 87_253, adr=392.6))
    b = revenue_breakdown(_comp(198, 99_748, 87_253, adr=1.0))
    assert a == b


@pytest.mark.parametrize("room", [None, 0, -5, 120_000])
def test_unusable_room_revenue_falls_back_to_per_night(room):
    """Room revenue above total would print negative fees."""
    assert revenue_breakdown(_comp(198, 99_748, room))["mode"] == "per_night"


def test_nothing_without_nights_or_revenue():
    assert revenue_breakdown(_comp(0, 99_748, 87_253)) is None
    assert revenue_breakdown(_comp(198, 0, 87_253)) is None


def test_half_cent_rounds_up_like_a_calculator():
    # 1 / 8 = 0.125 -> 0.13 half-up (banker's rounding would give 0.12)
    assert revenue_breakdown(_comp(8, 1))["per_night"] == Decimal("0.13")


# ── Room revenue for free: ttm_revpar x ttm_total_days ──
#
# The split above needs room revenue. Summing /listings/metrics/all costs $0.10
# per comp ($0.60 a report). ttm_revpar, already in every comp record, is room
# revenue / ttm_total_days rounded to 0.1: times 365 it matched the monthly sum
# within $18 on 30/30 comps (median $1), and 0.05 x 365 = $18.25 is exactly
# the rounding. (CLAUDE.md used to say ttm_revpar reconciled to nothing: its
# 0.80-0.97 ratio to revenue is the room share.)

# (ttm_revpar, ttm_total_days, monthly room revenue sum) measured 2026-09-25.
REVPAR_VS_MONTHLY = [
    (239.0, 365, 87_253), (148.7, 365, 54_292), (385.7, 365, 140_776),
    (96.3, 365, 35_132), (259.1, 365, 94_555), (170.2, 365, 62_114),
    (283.4, 365, 103_438), (257.9, 365, 94_149), (429.9, 365, 156_914),
]


@pytest.mark.parametrize("revpar,days,monthly", REVPAR_VS_MONTHLY)
def test_revpar_times_days_is_room_revenue_within_rounding(revpar, days, monthly):
    assert abs(revpar * days - monthly) <= 0.05 * days


def _airroi_record(revpar, total=365, revenue=99_748.0, booked=198):
    return {
        "listing_info": {"listing_id": 1, "listing_name": "X"},
        "property_details": {"bedrooms": 3, "baths": 3, "guests": 8},
        "performance_metrics": {
            "ttm_revenue": revenue, "ttm_avg_rate": 392.6, "ttm_revpar": revpar,
            "ttm_total_days": total, "ttm_blocked_days": 0, "ttm_days_reserved": booked,
            "ttm_available_days": total - booked, "ttm_occupancy": booked / total,
            "ttm_adjusted_occupancy": booked / total,
        },
    }


def test_adapter_takes_room_revenue_from_revpar():
    from adapters.airroi_to_comp import map_for_scorer, to_comp_property
    m = map_for_scorer(_airroi_record(239.0))
    assert m["room_revenue"] == pytest.approx(239.0 * 365)
    comp = to_comp_property(m)
    assert comp.room_revenue == pytest.approx(87_235)
    b = revenue_breakdown(comp)
    assert b["mode"] == "split" and b["room"] + b["fees"] == b["total"] == 99_748


@pytest.mark.parametrize("revpar", [None, 0])
def test_no_revpar_means_no_room_revenue_and_the_per_night_card(revpar):
    from adapters.airroi_to_comp import map_for_scorer, to_comp_property
    comp = to_comp_property(map_for_scorer(_airroi_record(revpar)))
    assert comp.room_revenue is None
    assert revenue_breakdown(comp)["mode"] == "per_night"


# ── Rendered cards ──

def _cards(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [c.get_text(" ") for c in soup.select(".comp-card")]


def test_cards_render_both_layouts_and_never_the_adr_field():
    data = _data()
    data.comps[0].room_revenue = data.comps[0].annual_revenue - 10_000
    cards = _cards(render_report(data))
    assert "cleaning & other fees" in cards[0]
    assert "per booked night" in cards[1].lower()
    for text in cards:
        assert "ADR" not in text
        assert "How the revenue adds up" in text


def test_card_figures_match_the_breakdown():
    data = _data()
    data.comps[0].room_revenue = 138_357
    b = revenue_breakdown(data.comps[0])
    card = _cards(render_report(data))[0]
    for figure in (f"{b['room']:,}", f"{b['fees']:,}", f"{b['total']:,}", f"{b['nightly']:,}"):
        assert figure in card, figure
