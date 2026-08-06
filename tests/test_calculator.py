"""Calculator defaults + Base Case alignment (2026-06-17 calculator fix)."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from schema import CompProperty, RevenueEstimate, PropertyBasics
from generators.calculator import (
    derive_calculator_defaults,
    derive_revenue_projection,
    align_calculator_to_base_case,
)


def _comp(name, rev, adr, occ):
    return CompProperty(
        name=name, sleeps=6, bedrooms=2, bathrooms=2.0,
        annual_revenue=rev, adr=adr, occupancy_pct=occ,
    )


def _subject():
    return PropertyBasics(
        address="123 Test Rd, Sun Peaks, BC",
        short_address="123 Test Rd, Sun Peaks BC",
        market="Sun Peaks", bedrooms=2, bathrooms=2.0, max_guests=6,
        currency="CA$",
    )


# A comp set where median(revenue) != median(occ) * median(ADR): the top earners
# do it through occupancy/volume, not nightly rate — the exact case that made the
# calculator default disagree with the headline Base Case.
_COMPS = [
    _comp("A", 40000, 200, 50),
    _comp("B", 55000, 240, 55),
    _comp("C", 76606, 300, 43),
    _comp("D", 90000, 520, 60),
    _comp("E", 120000, 600, 62),
]


def test_aligned_default_reproduces_base_case():
    rent = RevenueEstimate(revenue_potential=70000, adr=300, occupancy_pct=50)
    prop = _subject()
    calc = derive_calculator_defaults(_COMPS, rent, prop)
    proj = derive_revenue_projection(_COMPS, rent)

    calc = align_calculator_to_base_case(calc, proj.base_case)

    # The calculator's opening state now reproduces the headline (within rounding).
    default_rev = calc.occ_default / 100 * calc.adr_default * calc.days_default
    assert abs(default_rev - proj.base_case) / proj.base_case < 0.02
    # ADR step tightened to $5 so the aligned default lands on the slider track.
    assert calc.adr_step == 5
    # The slider can always reach its own (repositioned) default.
    assert calc.adr_min <= calc.adr_default <= calc.adr_max


def test_alignment_is_noop_without_base_case():
    prop = _subject()
    rent = RevenueEstimate(revenue_potential=0, adr=0, occupancy_pct=0)
    calc = derive_calculator_defaults([], rent, prop)
    before = calc.adr_default
    calc = align_calculator_to_base_case(calc, 0)
    assert calc.adr_default == before
