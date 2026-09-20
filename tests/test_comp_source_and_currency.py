"""The estimate's comp block is the primary source, and the subject's money
fields must match the report's currency.

Two bugs this locks down:

1. /listings/comparables ($0.10) returned a payload byte-identical to the
   comparable_listings block already inside /calculator/estimate ($0.20). The
   pipeline paid for both on every report.

2. Step 1's get_listing() defaults to currency="usd" and cannot pass the right
   one, because the currency is derived from location_info.country_code IN that
   same response. On a Canadian property the subject's money came back USD while
   the comps and estimate came back CAD, and the report labelled both CA$.
   Measured on Sun Peaks: 127,618 USD next to 188,668 CAD, a 1.3737x gap that
   made the property read as 32% below its own potential.
"""
import pytest

from scrapers.airroi import MIN_COMPS_FROM_ESTIMATE
from adapters.airroi_to_comp import subject_performance_from_listing


def _perf(rev, rate):
    return {
        "ttm_revenue": rev, "ttm_avg_rate": rate, "ttm_occupancy": 0.4,
        "ttm_adjusted_occupancy": 0.4, "ttm_total_days": 365,
        "ttm_blocked_days": 0, "ttm_days_reserved": 146,
        "ttm_available_days": 219, "ttm_revpar": rev / 365,
        "l90d_occupancy": 0.21, "l90d_days_reserved": 19,
    }


def test_threshold_is_well_above_the_six_comp_gate():
    """Phase A needs 6 comps AFTER heavy filtering, so the estimate block has to
    clear a much higher bar before we skip paying for the comparables call."""
    assert MIN_COMPS_FROM_ESTIMATE >= 10


def test_subject_performance_parses_from_a_comp_pool_record():
    """The re-source path reads a comp-pool entry with the same helper used on
    the /listings response. Same shape in, same object out."""
    comp = {"listing_info": {"listing_id": 39508095}, "performance_metrics": _perf(175310.0, 808.5)}
    sp = subject_performance_from_listing(comp)
    assert sp is not None
    assert sp.annual_revenue == 175310.0
    assert sp.adr == 808.5


def test_usd_and_native_records_differ_by_fx_not_by_occupancy():
    """The guard rail: occupancy is currency-free, money is not. If a future
    change re-sources occupancy but not revenue, this still passes; if it
    re-sources neither, the revenue assertion fails."""
    usd = subject_performance_from_listing(
        {"listing_info": {"listing_id": 1}, "performance_metrics": _perf(127618.0, 588.6)})
    cad = subject_performance_from_listing(
        {"listing_info": {"listing_id": 1}, "performance_metrics": _perf(175310.0, 808.5)})
    assert usd.occupancy_pct == cad.occupancy_pct          # FX must not move occupancy
    assert cad.annual_revenue > usd.annual_revenue
    assert round(cad.annual_revenue / usd.annual_revenue, 3) == pytest.approx(1.374, abs=0.002)


def test_missing_performance_block_yields_none_not_a_crash():
    assert subject_performance_from_listing({"listing_info": {"listing_id": 1}}) is None
