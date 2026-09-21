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
