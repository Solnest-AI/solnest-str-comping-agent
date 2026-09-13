"""Sparse-Airbtics seasonality: the crash, and the cost gate that hid it.

A market Airbtics tracks for fewer than 9 months was rejected by Priority 1 and
then resurrected by Priority 3, because agent.py assigns
rentalizer.monthly_occupancy = airbtics_to_seasonal(metrics) and that helper
leaves uncovered months as None. min(None, CAP) raised TypeError and killed the
run AFTER the AirROI calls were paid for.
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


def _metrics(months, occ=55.0):
    return [{"month": f"2026-{m:02d}", "occupancy": occ} for m in months]


def test_sparse_airbtics_does_not_crash():
    """The regression. Five covered months used to raise TypeError."""
    sparse = _metrics([5, 6, 7, 8, 9])
    r = _rent(airbtics_to_seasonal(sparse))
    series, basis = derive_seasonal_data_with_basis(r, airbtics_metrics=sparse)
    assert series == []          # nothing credible -> sanity gate blocks
    assert basis == ""


def test_sparse_airbtics_never_fabricates_zero_months():
    sparse = _metrics([5, 6, 7, 8, 9])
    r = _rent(airbtics_to_seasonal(sparse))
    series = derive_seasonal_data(r, airbtics_metrics=sparse)
    assert 0.0 not in series     # a 0% month on a client chart reads as broken


def test_full_airbtics_coverage_is_used_and_labelled():
    full = _metrics(range(1, 13))
    series, basis = derive_seasonal_data_with_basis(_rent(), airbtics_metrics=full)
    assert len(series) == 12 and all(v > 0 for v in series)
    assert basis == "airbtics"


def test_subject_own_curve_is_labelled_subject_not_airbtics():
    """The cost gate keys off this. Mislabelling it as Airbtics both misreports
    provenance and skips the per-comp calls that build a real market curve."""
    series, basis = derive_seasonal_data_with_basis(_rent([40.0] * 12))
    assert basis == "subject"
    assert len(series) == 12


def test_backcompat_wrapper_still_returns_a_bare_list():
    assert isinstance(derive_seasonal_data(_rent([40.0] * 12)), list)


@pytest.mark.parametrize("covered", [0, 1, 4, 8])
def test_below_threshold_coverage_never_returns_a_curve(covered):
    m = _metrics(range(1, covered + 1)) if covered else []
    r = _rent(airbtics_to_seasonal(m))
    series, basis = derive_seasonal_data_with_basis(r, airbtics_metrics=m)
    assert series == [] and basis == ""
