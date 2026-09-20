"""The market seasonality curve: one $0.10 call in place of six.

Six /listings/metrics/all calls ($0.60) existed only to average a monthly
occupancy curve out of the SELECTED comps. Those six are chosen for quality and
sit above the market, which is exactly the bias the occupancy anchor removes
from the headline. /markets/metrics/occupancy returns the whole-market curve
with p25-p90 for $0.10. Verified live on Gatlinburg and Sun Peaks.
"""
import pytest

from generators.calculator import (
    derive_seasonal_data_with_basis,
    market_occupancy_to_seasonal,
)
from schema import RentalizerData


def _rent():
    return RentalizerData(adr=300, occupancy_pct=50, annual_revenue=50_000,
                          revenue_potential=60_000)


def _rows(months, occ=0.5):
    """AirROI returns a TRAILING window, so months may start mid-year."""
    return [{"date": f"2026-{m:02d}-01", "avg": occ, "p25": occ - .1, "p50": occ,
             "p75": occ + .1, "p90": occ + .2} for m in months]


def test_rows_map_by_calendar_month_not_by_position():
    """The trailing window starts in September. A positional mapping would put
    September's value in January."""
    rows = [{"date": "2025-09-01", "avg": 0.44}, {"date": "2025-10-01", "avg": 0.61},
            {"date": "2026-01-01", "avg": 0.30}]
    out = market_occupancy_to_seasonal(rows)
    assert out[8] == 44.0     # Sep
    assert out[9] == 61.0     # Oct
    assert out[0] == 30.0     # Jan
    assert out[1] is None     # Feb uncovered, stays None


def test_fractions_become_percentages():
    assert market_occupancy_to_seasonal([{"date": "2026-03-01", "avg": 0.615}])[2] == 61.5


def test_already_percent_values_are_left_alone():
    assert market_occupancy_to_seasonal([{"date": "2026-03-01", "avg": 61.5}])[2] == 61.5


def test_full_coverage_is_used_and_labelled_market():
    series, basis = derive_seasonal_data_with_basis(
        _rent(), market_occupancy=_rows(range(1, 13), 0.5))
    assert basis == "market"
    assert len(series) == 12 and all(v > 0 for v in series)


def test_market_beats_the_per_comp_average():
    """Priority order: a market curve must win over comp monthly data, so the
    six paid calls are never bought when the $0.10 call already answered."""
    comps = [[70.0] * 12 for _ in range(6)]
    _, basis = derive_seasonal_data_with_basis(
        _rent(), comp_monthly_data=comps, market_occupancy=_rows(range(1, 13), 0.4))
    assert basis == "market"


def test_airbtics_still_outranks_market():
    full_airbtics = [{"month": f"2026-{m:02d}", "occupancy": 55.0} for m in range(1, 13)]
    _, basis = derive_seasonal_data_with_basis(
        _rent(), airbtics_metrics=full_airbtics, market_occupancy=_rows(range(1, 13), 0.4))
    assert basis == "airbtics"


@pytest.mark.parametrize("covered", [0, 3, 8])
def test_thin_market_falls_through_rather_than_guessing(covered):
    """Same >=9 bar as Airbtics. Below it the caller must pay for the comps."""
    comps = [[70.0] * 12 for _ in range(6)]
    _, basis = derive_seasonal_data_with_basis(
        _rent(), comp_monthly_data=comps,
        market_occupancy=_rows(range(1, covered + 1), 0.4) if covered else [])
    assert basis == "comps"


def test_malformed_rows_are_skipped_not_fatal():
    rows = [{"date": "garbage", "avg": 0.5}, {"date": "2026-04-01", "avg": None},
            "not a dict", {"date": "2026-05-01", "avg": 0.6}]
    out = market_occupancy_to_seasonal(rows)
    assert out[4] == 60.0
    assert out[3] is None
