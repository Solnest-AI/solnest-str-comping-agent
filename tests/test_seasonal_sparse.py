"""Priority 3 must reject a series containing None, or it crashes the run.

HISTORY. This guard was added after a real crash: a partial monthly series on
rentalizer.monthly_occupancy was rejected by the market priority and then
resurrected by Priority 3, where min(None, CAP) raised TypeError and killed the
run AFTER the paid AirROI calls. The provider that produced those partial
series is gone (AirROI has been the single market-data source since
2026-09-20), but nothing in the type signature stops a future caller from
putting a partial series on rentalizer.monthly_occupancy, so the guard stays
and is still tested.
"""
import pytest

from generators.calculator import derive_seasonal_data_with_basis
from schema import RentalizerData


def _rent(monthly=None):
    r = RentalizerData(adr=300, occupancy_pct=50, annual_revenue=50_000,
                       revenue_potential=60_000)
    if monthly is not None:
        r.monthly_occupancy = monthly
    return r


def _partial(covered_months):
    """A 12-slot series with only these calendar months populated."""
    return [55.0 if (m + 1) in covered_months else None for m in range(12)]


def test_partial_subject_series_does_not_crash():
    """The regression. Five covered months used to raise TypeError."""
    series, basis = derive_seasonal_data_with_basis(_rent(_partial({5, 6, 7, 8, 9})))
    assert series == []          # nothing credible -> sanity gate blocks
    assert basis == ""


def test_partial_subject_series_never_fabricates_zero_months():
    series, _ = derive_seasonal_data_with_basis(_rent(_partial({5, 6, 7, 8, 9})))
    assert 0.0 not in series     # a 0% month on a client chart reads as broken


@pytest.mark.parametrize("covered", [0, 1, 4, 8, 11])
def test_any_gap_at_all_rejects_the_subject_series(covered):
    """Priority 3 demands a FULLY populated series. One missing month is enough
    to reject it, because the cap comparison cannot handle a None."""
    months = set(range(1, covered + 1))
    series, basis = derive_seasonal_data_with_basis(_rent(_partial(months)))
    assert series == [] and basis == ""


def test_complete_subject_series_is_accepted_and_labelled():
    series, basis = derive_seasonal_data_with_basis(_rent([40.0] * 12))
    assert basis == "subject"
    assert len(series) == 12
