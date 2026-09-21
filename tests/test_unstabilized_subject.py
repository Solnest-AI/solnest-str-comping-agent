"""A listing that has not been live a full year must not be projected from.

AirROI computes ttm_occupancy across the whole 365-day window whether or not
the listing existed for it, counting the pre-launch period as blocked
inventory. Anchoring the revenue calculator to that number told a healthy
property it was failing.

The real case, "Sleeps 12 Log Cabin | Bike Park" (Sun Peaks, live June 2026):

    AirROI trailing:  20% occupancy of 245 "open" nights, 49 nights booked
    Its 3 real months: Jun 53.3%  Jul 38.7%  Aug 61.3%   (mean 51.1%)
    Market same months: p50 25.3%, p75 43.7%

It ran above the market's upper quartile in every month it existed and the
report opened its calculator at CA$47,775.
"""

from __future__ import annotations

from generators.calculator import (
    _beats_market_median,
    _market_level,
    derive_calculator_defaults,
    market_has_data,
)
from schema import (
    CompProperty,
    PropertyBasics,
    RentalizerData,
    SubjectPerformance,
)

# The cabin, exactly as AirROI reports it.
CABIN = SubjectPerformance(
    annual_revenue=48_183, occupancy_pct=20.0, adr=834.4,
    nights_booked=49, nights_listed=245,
    l90d_occupancy_pct=52.2, l90d_nights_booked=47,
)

# Its own monthly line, Jan..Dec, from /listings/metrics/all.
CABIN_MONTHLY: list[float | None] = [None] * 12
CABIN_MONTHLY[5], CABIN_MONTHLY[6], CABIN_MONTHLY[7] = 53.3, 38.7, 61.3

# Sun Peaks /markets/metrics/occupancy, verbatim. Sep/Oct/Nov 2025 are
# AirROI's no-data sentinel: every percentile identical.
SUNPEAKS = [
    {"date": "2025-09-01", "avg": 0.00, "p25": 0.00, "p50": 0.00, "p75": 0.00, "p90": 0.00},
    {"date": "2025-10-01", "avg": 0.10, "p25": 0.10, "p50": 0.10, "p75": 0.10, "p90": 0.10},
    {"date": "2025-11-01", "avg": 0.00, "p25": 0.00, "p50": 0.00, "p75": 0.00, "p90": 0.00},
    {"date": "2025-12-01", "avg": 0.41, "p25": 0.32, "p50": 0.39, "p75": 0.48, "p90": 0.61},
    {"date": "2026-01-01", "avg": 0.45, "p25": 0.19, "p50": 0.45, "p75": 0.71, "p90": 0.77},
    {"date": "2026-02-01", "avg": 0.57, "p25": 0.32, "p50": 0.61, "p75": 0.75, "p90": 0.89},
    {"date": "2026-03-01", "avg": 0.36, "p25": 0.07, "p50": 0.26, "p75": 0.65, "p90": 0.81},
    {"date": "2026-04-01", "avg": 0.18, "p25": 0.10, "p50": 0.10, "p75": 0.23, "p90": 0.43},
    {"date": "2026-05-01", "avg": 0.23, "p25": 0.07, "p50": 0.16, "p75": 0.29, "p90": 0.55},
    {"date": "2026-06-01", "avg": 0.31, "p25": 0.13, "p50": 0.27, "p75": 0.50, "p90": 0.53},
    {"date": "2026-07-01", "avg": 0.27, "p25": 0.10, "p50": 0.26, "p75": 0.42, "p90": 0.48},
    {"date": "2026-08-01", "avg": 0.29, "p25": 0.23, "p50": 0.23, "p75": 0.39, "p90": 0.52},
]

# Real comp nights_listed from the shipped Sun Peaks report: median 330.
COMP_NIGHTS = [187, 365, 334, 326, 352, 314]


def _comps() -> list[CompProperty]:
    return [
        CompProperty(
            name=f"Comp {i}", sleeps=12, bedrooms=4, bathrooms=2.5,
            rating=4.9, review_count=40, annual_revenue=140_000,
            occupancy_pct=50.0, adr=760, nights_booked=170, nights_listed=n,
        )
        for i, n in enumerate(COMP_NIGHTS)
    ]


def _prop(sp: SubjectPerformance | None) -> PropertyBasics:
    return PropertyBasics(
        address="1 Test", short_address="1 Test", market="Sun Peaks",
        bedrooms=4, bathrooms=2.5, max_guests=12, currency="CA$",
        subject_performance=sp,
    )


def _rent() -> RentalizerData:
    return RentalizerData(revenue_potential=150_000, adr=860, occupancy_pct=43)


def _defaults(sp, monthly=None, market=None):
    return derive_calculator_defaults(
        _comps(), _rent(), _prop(sp),
        pool_occupancies=None,
        market_occupancy=market if market is not None else SUNPEAKS,
        subject_monthly=monthly,
    )


# ── is_stabilized ──

def test_three_months_of_history_is_not_a_year():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    assert sp.has_history, "it has real bookings and must still be displayed"
    assert not sp.is_stabilized, "but 3 of 12 months cannot anchor a projection"


def test_twelve_months_of_history_is_a_year():
    sp = CABIN.model_copy(update={"months_with_data": 12})
    assert sp.is_stabilized


def test_l90d_share_catches_a_young_listing_with_no_month_count():
    """96% of the year's bookings in one quarter is a launch, not a season."""
    assert CABIN.months_with_data is None
    assert not CABIN.is_stabilized


def test_l90d_share_does_not_flag_a_mature_seasonal_listing():
    mature = SubjectPerformance(
        annual_revenue=200_000, occupancy_pct=62, adr=700,
        nights_booked=210, nights_listed=340,
        l90d_nights_booked=74,          # 35% of the year in one quarter
    )
    assert mature.is_stabilized


def test_no_signal_at_all_stays_stabilized():
    """Old behaviour when AirROI gives us nothing to judge with."""
    bare = SubjectPerformance(
        annual_revenue=100_000, occupancy_pct=55, adr=500,
        nights_booked=180, nights_listed=330,
    )
    assert bare.is_stabilized


# ── AirROI's no-data sentinel ──

def test_identical_percentiles_means_no_data():
    assert not market_has_data(SUNPEAKS[0])           # all zeros
    assert not market_has_data(SUNPEAKS[1])           # all 0.10
    assert market_has_data(SUNPEAKS[3])               # a real month


def test_market_level_excludes_the_dead_months():
    """Averaging the sentinel months in drags p50 down by a third."""
    p50 = _market_level(SUNPEAKS, "p50")
    assert p50 is not None
    assert 29.0 < p50 < 32.0, p50                     # 30.3% over 9 real months
    naive = sum(r["p50"] for r in SUNPEAKS) / len(SUNPEAKS) * 100
    assert naive < 25.0, "the bug this filter prevents"


def test_market_level_returns_none_when_the_curve_is_too_thin():
    assert _market_level(SUNPEAKS[:3], "p50") is None
    assert _market_level([], "p50") is None


# ── the anchor ──

def test_unstabilized_subject_does_not_anchor_the_scenario():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert calc.occ_basis != "subject"
    assert calc.occ_default > 20, (
        f"the whole bug: AirROI's fake 20% became the default ({calc.occ_default}%)"
    )


def test_outperformer_gets_the_market_upper_quartile():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert calc.occ_basis == "market_strong"
    assert 47 <= calc.occ_default <= 51, calc.occ_default      # market p75 ~49.1%


def test_outperformer_is_never_given_its_own_raw_pace():
    """51.1% across three summer months is not 51.1% across a ski year."""
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert calc.occ_default < 51.1


def test_underperformer_gets_the_market_median_not_the_quartile():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    weak = [None] * 12
    weak[5], weak[6], weak[7] = 8.0, 9.0, 7.0     # well under the market p50
    calc = _defaults(sp, weak)
    assert calc.occ_basis == "market_typical"
    assert 29 <= calc.occ_default <= 32, calc.occ_default


def test_stabilized_subject_still_wins():
    """Fix A must survive: a real year of history outranks the market."""
    sp = CABIN.model_copy(update={"months_with_data": 12, "occupancy_pct": 84.3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert calc.occ_basis == "subject"
    assert calc.occ_default == 84


def test_no_market_curve_falls_through_to_the_old_ladder():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = derive_calculator_defaults(
        _comps(), _rent(), _prop(sp),
        pool_occupancies=[30.0] * 25,
        market_occupancy=None, subject_monthly=CABIN_MONTHLY,
    )
    assert calc.occ_basis == "market_pool"


# ── nights listed ──

def test_prelaunch_blackout_does_not_shrink_the_inventory():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert calc.days_default == 330, (
        f"245 'open' nights is 365 minus the 120 days before launch; "
        f"got {calc.days_default}"
    )


def test_a_host_who_really_blocks_half_the_year_keeps_their_number():
    sp = SubjectPerformance(
        annual_revenue=90_000, occupancy_pct=55, adr=600,
        nights_booked=100, nights_listed=180, months_with_data=12,
    )
    calc = _defaults(sp, None)
    assert calc.days_default == 180


# ── the end-to-end number ──

def test_the_scenario_no_longer_opens_at_the_pre_launch_figure():
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    scenario = round(calc.days_default * calc.occ_default / 100) * calc.adr_default
    assert scenario > 100_000, f"shipped CA$47,775; now CA${scenario:,}"


def test_beats_market_median_needs_real_overlap():
    assert not _beats_market_median(None, SUNPEAKS)
    assert not _beats_market_median(CABIN_MONTHLY, None)
    one = [None] * 12
    one[6] = 90.0
    assert not _beats_market_median(one, SUNPEAKS), "one month is not evidence"


# ── the methodology section must not contradict the report above it ──

def test_methodology_never_claims_airbtics():
    """Airbtics was removed from the pipeline. The report kept citing it as a
    live data source, which is a false statement to a client."""
    from generators.methodology import build_methodology
    sp = CABIN.model_copy(update={"months_with_data": 3})
    m = build_methodology(_prop(sp), _comps(), calculator=_defaults(sp, CABIN_MONTHLY))
    blob = " ".join(m.data_sources).lower()
    assert "airbtics" not in blob
    assert "tourism trends" not in blob, "names research nobody performs"


def test_methodology_occupancy_assumption_matches_the_actual_basis():
    """It used to fall through to 'median of the six comparables shown' for any
    basis it did not know, contradicting the disclosure three sections above."""
    from generators.methodology import build_methodology
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert calc.occ_basis == "market_strong"
    m = build_methodology(_prop(sp), _comps(), calculator=calc)
    assumption = m.assumptions[0].lower()
    assert "upper-quartile" in assumption
    assert "six comparables shown" not in assumption


def test_methodology_market_typical_basis_is_described():
    from generators.methodology import build_methodology
    sp = CABIN.model_copy(update={"months_with_data": 3})
    weak = [None] * 12
    weak[5], weak[6], weak[7] = 8.0, 9.0, 7.0
    calc = _defaults(sp, weak)
    assert calc.occ_basis == "market_typical"
    m = build_methodology(_prop(sp), _comps(), calculator=calc)
    assumption = m.assumptions[0].lower()
    assert "median occupancy" in assumption
    assert "six comparables shown" not in assumption


# ── disclosure fixes taken from reading Codex's hand-written report ──

def test_comp_funnel_line_reports_the_whole_funnel():
    from generators.methodology import _comp_funnel_line
    line = _comp_funnel_line({
        "candidates": 25, "subject_removed": 1, "filtered_out": 1,
        "hard_fails": 4, "selected": 6,
    })
    assert "25 candidate listings considered, 6 selected" in line
    assert "the subject itself removed" in line
    assert "1 dropped" in line
    assert "4 failed the activity and history gates" in line


def test_comp_funnel_line_says_when_the_radius_was_widened():
    from generators.methodology import _comp_funnel_line
    line = _comp_funnel_line({
        "candidates": 12, "subject_removed": 1, "filtered_out": 0,
        "hard_fails": 3, "selected": 6, "widened": True,
    })
    assert "wider second search" in line, (
        "a widened set no longer matches the radius the criteria claim"
    )


def test_comp_funnel_line_is_empty_when_there_is_nothing_to_say():
    from generators.methodology import _comp_funnel_line
    assert _comp_funnel_line(None) == ""
    assert _comp_funnel_line({}) == ""
    assert _comp_funnel_line({"candidates": 0, "selected": 6}) == ""


def test_slider_text_separates_our_bounds_from_observed_results():
    """'Industry range: 10-90% for premium properties' claimed industry data
    for a number that was the anchor padded out. No source was consulted."""
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert "industry" not in calc.occ_range_text.lower()
    assert "adjustable" in calc.occ_range_text
    assert "comps observed" in calc.occ_range_text


def test_rate_slider_discloses_that_card_adr_is_a_different_basis():
    """The slider is revenue per booked night (fees IN); the card ADR excludes
    fees. Showing both without saying so invites the reader to compare them."""
    sp = CABIN.model_copy(update={"months_with_data": 3})
    calc = _defaults(sp, CABIN_MONTHLY)
    assert "fees included" in calc.adr_range_text
    assert "excludes fees" in calc.adr_range_text


# ── the headline potential: p75, not p90 ──

def test_headline_potential_uses_p75_not_p90():
    """AirROI's percentiles are the spread of its MODEL'S PREDICTIONS, not of
    observed results. Measured against the 25 comps in the same response:
    p25/p50/p75 land within 5% of what the pool actually did; p90 was 28% above
    what the single best of 25 listings earned. p75 is the last percentile
    still anchored to observed performance."""
    import agent
    est = {
        "revenue": 123_852.41, "average_daily_rate": 860.53, "occupancy": 0.4254,
        "percentiles": {"revenue": {"avg": 123_852.41, "p25": 72_446.88,
                                    "p50": 108_061.87, "p75": 150_067.86,
                                    "p90": 196_645.17}},
    }
    r = agent.build_rentalizer(est) if hasattr(agent, "build_rentalizer") else None
    if r is None:
        import re
        src = open("agent.py").read()
        assert 'rev_pct.get("p75")' in src
        assert 'rev_pct.get("p90")' not in src, "p90 must not be the headline"
        return
    assert abs(r.revenue_potential - 150_067.86) < 1


def test_brief_tells_the_writer_what_the_potential_actually_is():
    """The narrative rules say every figure must come from the brief, so the
    brief licenses whatever it carries. It must not hand over a ceiling
    unlabelled."""
    src = open("generators/narrative_brief.py").read()
    assert "75th percentile" in src
    assert "not a forecast" in src


# ── the seasonality chart's fabricated months ──

def test_sentinel_months_are_not_plotted_as_real_occupancy():
    from generators.calculator import market_occupancy_to_seasonal
    series = market_occupancy_to_seasonal(SUNPEAKS)
    assert series[8] is None, "Sep 2025 reported no data and was drawn as 0%"
    assert series[10] is None, "Nov 2025 reported no data and was drawn as 0%"
    assert series[9] is None, "Oct 2025 reported no data and was drawn as 10%"
    assert series[11] == 39.0, "Dec is real and must survive"


def test_the_band_is_interpolated_over_the_gap_not_collapsed():
    from generators.calculator import market_occupancy_band
    band = market_occupancy_band(SUNPEAKS)
    for key in ("p25", "p50", "p75"):
        assert all(v is not None for v in band[key]), key
        assert band[key][8] > 0, f"{key} Sep collapsed to the axis"
    # ordering must survive independent interpolation
    for i in range(12):
        assert band["p25"][i] <= band["p50"][i] <= band["p75"][i], i


def test_partial_rows_are_not_mistaken_for_no_data():
    """The sentinel is IDENTICAL percentiles, not MISSING ones. Requiring all
    five keys blanked every legitimately sparse market; caught by
    tests/test_market_seasonality.py."""
    assert market_has_data({"date": "2026-01-01", "p50": 0.44})
    assert market_has_data({"date": "2026-01-01", "avg": 0.71})
    assert not market_has_data(
        {"date": "2025-09-01", "avg": 0.0, "p25": 0.0, "p50": 0.0,
         "p75": 0.0, "p90": 0.0})


def test_market_months_missing_counts_the_sentinel_rows():
    from generators.calculator import market_months_missing
    assert market_months_missing(SUNPEAKS) == 3
    assert market_months_missing([]) == 0


def test_interpolated_months_are_disclosed():
    from generators.methodology import build_methodology
    sp = CABIN.model_copy(update={"months_with_data": 3})
    m = build_methodology(_prop(sp), _comps(),
                          calculator=_defaults(sp, CABIN_MONTHLY),
                          market_months_missing=3)
    blob = " ".join(m.data_sources)
    assert "3 of 12" in blob and "not measured" in blob


def test_no_interpolation_note_when_nothing_was_interpolated():
    from generators.methodology import build_methodology
    sp = CABIN.model_copy(update={"months_with_data": 12})
    m = build_methodology(_prop(sp), _comps(),
                          calculator=_defaults(sp, None),
                          market_months_missing=0)
    assert not any("Interpolated" in d for d in m.data_sources)
