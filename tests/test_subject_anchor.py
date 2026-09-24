"""Guards for the occupancy/rate anchoring fixes (Fix A + Fix B).

Backtested leave-one-out over the 125 unique live listings in
tests/fixtures/ (comps_coords.json excluded — it is comps_gatlinburg
re-queried by coordinates and double-counts 21 listings in the single
most-biased market):

    anchor                     median err   median bias   within +/-30%
    comp-set median (before)        72.5%        +72.5%           26%
    market-pool median (Fix B)      39.1%         +7.0%           40%
    subject's own history (Fix A)    1.7%         -0.0%          100%

Fix A's 1.7% is a plumbing check, not forecast skill: for an already-listed
property the trailing revenue IS the answer, so the report is reporting
rather than predicting. Fix B is the number that matters for a subject with
no history, which is the pre-purchase case the tool is usually run for.
"""

from __future__ import annotations

import pytest

from _fixtures import market_listings
from adapters.airroi_to_comp import (
    map_batch_for_scorer,
    subject_performance_from_listing,
    to_comp_property,
)
from generators.calculator import MIN_POOL_FOR_ANCHOR, derive_calculator_defaults
from report.template_engine import render_report
from schema import (
    CalculatorDefaults,
    CompProperty,
    MethodologyData,
    Narratives,
    PropertyBasics,
    ReportData,
    RentalizerData,
    SubjectPerformance,
)

REAL_MARKETS = ["destin", "gatlinburg", "nashville", "scottsdale", "sunpeaks"]


def _comps(occ: float, n: int = 6) -> list[CompProperty]:
    return [
        CompProperty(
            name=f"Comp {i}", sleeps=8, bedrooms=3, bathrooms=2.0,
            rating=4.9, review_count=40, annual_revenue=90_000,
            occupancy_pct=occ, adr=350, nights_booked=200, nights_listed=340,
        )
        for i in range(n)
    ]


def _prop(sp: SubjectPerformance | None = None) -> PropertyBasics:
    return PropertyBasics(
        address="1 Test St", short_address="1 Test St", market="Testville",
        bedrooms=3, bathrooms=2.0, max_guests=8, subject_performance=sp,
    )


def _rent() -> RentalizerData:
    return RentalizerData(revenue_potential=100_000, adr=350, occupancy_pct=60)


SUBJECT = SubjectPerformance(
    annual_revenue=129_134, occupancy_pct=84.3, adr=410,
    nights_booked=290, nights_listed=344,
)


# ── Fix A: the subject's own history is extracted and used ──

def test_subject_performance_extracted_from_every_live_listing():
    """125/125 live listings carry the block that used to be discarded."""
    total = found = 0
    for market in REAL_MARKETS:
        for raw in market_listings(market):
            total += 1
            if subject_performance_from_listing(raw) is not None:
                found += 1
    assert total == 125
    assert found == 125, f"only {found}/{total} subjects yielded performance data"


def test_subject_performance_uses_open_inventory_not_unsold_nights():
    """nights_listed must be total-blocked, never ttm_available_days.

    ttm_available_days is UNSOLD nights. Reading it here inverts the number
    and every downstream occupancy claim with it.
    """
    for market in REAL_MARKETS:
        for raw in market_listings(market):
            sp = subject_performance_from_listing(raw)
            if sp is None:
                continue
            pm = raw["performance_metrics"]
            assert sp.nights_listed == pm["ttm_total_days"] - pm["ttm_blocked_days"]
            assert sp.nights_booked <= sp.nights_listed


def test_no_history_returns_none_not_a_zeroed_model():
    """A pre-purchase subject must read as 'unknown', never as 0% occupancy."""
    assert subject_performance_from_listing(None) is None
    assert subject_performance_from_listing({}) is None
    assert subject_performance_from_listing(
        {"performance_metrics": {"ttm_revenue": 0, "ttm_days_reserved": 0}}
    ) is None


def test_subject_history_beats_comp_median():
    calc = derive_calculator_defaults(_comps(55), _rent(), _prop(SUBJECT))
    assert calc.occ_basis == "subject"
    assert calc.occ_default == 84, "headline ignored the subject's own occupancy"


def test_strong_subject_is_not_dragged_down_to_the_comp_median():
    """The Gatlinburg regression: a property at 84.3% was reported at 50%."""
    calc = derive_calculator_defaults(_comps(50), _rent(), _prop(SUBJECT))
    assert calc.occ_default > 50


# ── Fix B: market pool, not the six selected comps ──

def test_pool_median_used_when_subject_has_no_history():
    pool = [30.0] * 12
    calc = derive_calculator_defaults(_comps(70), _rent(), _prop(), pool_occupancies=pool)
    assert calc.occ_basis == "market_pool"
    assert calc.occ_default == 30


def test_small_pool_falls_back_to_comp_set():
    """A handful of listings is the comp set with extra steps, not a market."""
    pool = [30.0] * (MIN_POOL_FOR_ANCHOR - 1)
    calc = derive_calculator_defaults(_comps(70), _rent(), _prop(), pool_occupancies=pool)
    assert calc.occ_basis == "comp_set"
    assert calc.occ_default == 70


def test_subject_history_outranks_the_pool():
    pool = [30.0] * 20
    calc = derive_calculator_defaults(
        _comps(70), _rent(), _prop(SUBJECT), pool_occupancies=pool,
    )
    assert calc.occ_basis == "subject"


# ── The clamp trap: bounds must contain the anchor ──

@pytest.mark.parametrize("anchor_occ", [12.0, 30.0, 55.0, 88.0])
def test_anchor_is_never_silently_clamped_back_to_the_comp_range(anchor_occ):
    """Deriving bounds from comps alone would drag the anchor back and
    undo the correction without telling anyone."""
    pool = [anchor_occ] * 15
    calc = derive_calculator_defaults(_comps(70), _rent(), _prop(), pool_occupancies=pool)
    assert calc.occ_min <= calc.occ_default <= calc.occ_max
    assert calc.occ_default == round(anchor_occ)


def test_defaults_stay_inside_sliders_across_every_live_market():
    checked = 0
    for market in REAL_MARKETS:
        listings = market_listings(market)
        mapped = map_batch_for_scorer(listings)
        pool = [float(c["occupancy_pct"]) for c in mapped if c.get("occupancy_pct")]
        comps = [
            to_comp_property(c) for c in mapped
            if c.get("annual_revenue") and c.get("nights_booked")
        ][:6]
        if len(comps) < 3:
            continue
        for raw in listings:
            sp = subject_performance_from_listing(raw)
            calc = derive_calculator_defaults(
                comps, _rent(), _prop(sp), pool_occupancies=pool,
            )
            assert calc.occ_min <= calc.occ_default <= calc.occ_max
            assert calc.adr_min <= calc.adr_default <= calc.adr_max
            assert calc.days_min <= calc.days_default <= calc.days_max
            assert calc.occ_default > 0 and calc.adr_default > 0
            checked += 1
    assert checked >= 125


def test_weak_subject_still_sees_upside_on_the_slider():
    """Anchoring honestly must not hide the headroom: a 15% operator gets a
    15% headline, but the slider must still reach the comp range."""
    weak = SubjectPerformance(
        annual_revenue=20_000, occupancy_pct=15.0, adr=300,
        nights_booked=50, nights_listed=340,
    )
    calc = derive_calculator_defaults(_comps(70), _rent(), _prop(weak))
    assert calc.occ_default == 15
    assert calc.occ_max >= 70, "no visible upside for an under-performer"


# ── Rendering: the gap that let an undefined name reach production ──

def _report(sp):
    return ReportData(
        property=_prop(sp), rentalizer=_rent(), comps=_comps(60),
        calculator=CalculatorDefaults(occ_basis="subject" if sp else "market_pool"),
        narratives=Narratives(), methodology=MethodologyData(),
    )


def test_report_renders_with_and_without_subject_history():
    with_sp = render_report(_report(SUBJECT))
    assert "AirROI Reported Trailing Performance" in with_sp
    assert "129,134" in with_sp

    without = render_report(_report(None))
    assert "AirROI Reported Trailing Performance" not in without


def test_report_discloses_which_basis_the_headline_used():
    assert "own measured result" in render_report(_report(SUBJECT))
    assert "median across every comparable" in render_report(_report(None))


def test_report_data_round_trips_subject_performance():
    """The --render pass reads this back off disk; a dropped field there
    would silently revert the headline to the comp median."""
    rd = _report(SUBJECT)
    back = ReportData.model_validate_json(rd.model_dump_json())
    assert back.property.subject_performance.occupancy_pct == 84.3
    assert back.property.subject_performance.has_history
    assert round(back.property.subject_performance.revenue_per_booked_night, 2) == 445.29


# ── Methodology must state the basis it actually used ──

def test_methodology_discloses_the_occupancy_basis():
    from generators.methodology import build_methodology

    with_sp = build_methodology(
        _prop(SUBJECT), [], calculator=CalculatorDefaults(occ_basis="subject"),
    )
    assert "own trailing 12 months" in with_sp.assumptions[0]

    pooled = build_methodology(
        _prop(), [], calculator=CalculatorDefaults(occ_basis="market_pool"),
    )
    assert "every comparable listing in this market" in pooled.assumptions[0]

    fallback = build_methodology(_prop(), [])
    assert "six comparables shown" in fallback.assumptions[0]
