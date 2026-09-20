"""Priority 3 must reject a series containing None, or it crashes the run.

HISTORY. This guard was added after a real crash. agent.py used to assign
rentalizer.monthly_occupancy = airbtics_to_seasonal(metrics) when the subject
had no monthly data of its own, and that helper leaves uncovered months as None
by design. A market tracked for fewer than 9 months was rejected by the Airbtics
priority and then resurrected by Priority 3, where min(None, CAP) raised
TypeError and killed the run AFTER the paid AirROI calls.

Airbtics was removed from the pipeline entirely on 2026-09-20 (AirROI is now the
single market-data source), so that specific route is gone. The guard stays and
is still tested: any future caller that puts a partial series on
rentalizer.monthly_occupancy would hit the same crash, and nothing in the type
signature stops them.
"""
import pytest

from generators.calculator import (
    airbtics_to_seasonal,
    derive_seasonal_data,
    derive_seasonal_data_with_basis,
)
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
    series = derive_seasonal_data(_rent(_partial({5, 6, 7, 8, 9})))
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


def test_backcompat_wrapper_still_returns_a_bare_list():
    assert isinstance(derive_seasonal_data(_rent([40.0] * 12)), list)


def test_airbtics_helper_still_leaves_gaps_as_none():
    """airbtics_to_seasonal is no longer wired into the pipeline, but it is kept
    and tested: its None-for-missing-month contract is what the guard above
    exists to survive, and re-introducing a provider must not quietly change it."""
    sparse = [{"month": f"2026-{m:02d}", "occupancy": 55.0} for m in (5, 6, 7)]
    out = airbtics_to_seasonal(sparse)
    assert out.count(None) == 9
    assert 0.0 not in out
