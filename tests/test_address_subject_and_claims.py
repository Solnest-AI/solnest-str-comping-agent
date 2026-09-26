"""Four defects found by running two untested subject types live, 2026-09-25.

1. A weak, stabilized listing ("10minDisney | Cozy Mickey themed home",
   Four Corners FL, airbnb 837027843923260762): 23.6% adjusted occupancy over
   348 open nights against a comp median of 59.9%. The template copy called it
   "a premium tier for Four Corners ... competes with the top of the STR
   market" and "a statement-home experience that commands higher nightly
   rates". A student who skips pass two ships that to an owner.

2. The same report's Key Assumptions said the projection assumes professional
   management and premium photography, while the projection was the listing's
   own trailing year, i.e. how it runs today.

3. An address subject (1131 Tanrac Trl, Gatlinburg TN, for sale): the address
   path never sets coordinates, so Step 8 never bought the market curve and the
   scorer gave every comp dist=0. /calculator/estimate had geocoded the address
   in the same run (35.7461037, -83.4811331).

4. That report's Data Sources still printed "AirROI market curve: whole-market
   occupancy for Gatlinburg ... every listing in the market" while the chart
   was the average of the six comps.
"""

from __future__ import annotations

import pytest

from agent import _adopt_estimate_location
from generators.methodology import build_methodology
from generators.narratives import template_narratives
from schema import (
    CalculatorDefaults,
    CompProperty,
    PropertyBasics,
    SubjectPerformance,
)

# The six comps actually selected for the Four Corners run.
FOUR_CORNERS_COMP_OCC = [77.1, 66.0, 59.3, 60.5, 40.8, 30.1]
FOUR_CORNERS_COMP_ADR = [222, 177, 173, 306, 200, 228]

WEAK = SubjectPerformance(
    annual_revenue=20_121, occupancy_pct=23.6, adr=223,
    nights_booked=82, nights_listed=348, months_with_data=12,
)
STRONG = SubjectPerformance(
    annual_revenue=129_134, occupancy_pct=84.3, adr=410,
    nights_booked=290, nights_listed=344, months_with_data=12,
)
YOUNG = SubjectPerformance(
    annual_revenue=40_000, occupancy_pct=20.0, adr=700,
    nights_booked=49, nights_listed=245, months_with_data=3,
)

# Phrases that assert quality or compliance nothing in the pipeline measured.
UNSUPPORTED = (
    "premium tier", "top tier", "top of the", "statement-home",
    "commands higher", "premium finishes", "view-forward",
    "aligned with municipality bylaws",
)


def _comps() -> list[CompProperty]:
    return [
        CompProperty(
            name=f"Comp {i}", sleeps=8, bedrooms=3, bathrooms=2.5,
            rating=4.8, review_count=100, annual_revenue=50_000,
            occupancy_pct=occ, adr=adr, nights_booked=200, nights_listed=350,
        )
        for i, (occ, adr) in enumerate(zip(FOUR_CORNERS_COMP_OCC, FOUR_CORNERS_COMP_ADR))
    ]


def _prop(sp: SubjectPerformance | None = None, **kw) -> PropertyBasics:
    base = dict(
        address="Four Corners, Florida", short_address="Test Townhouse",
        market="Four Corners", bedrooms=3, bathrooms=2.5, max_guests=10,
        subject_performance=sp,
    )
    base.update(kw)
    return PropertyBasics(**base)


def _all_text(n) -> str:
    parts = [
        n.positioning_summary, n.guest_profile, n.amenity_upside,
        n.config_description, n.guests_description,
        n.peak_season_text, n.shoulder_season_text,
    ]
    parts += [b["text"] for b in n.amenity_badges]
    parts += [c.title + " " + c.text for c in n.positioning_cards]
    return " ".join(parts).lower()


# ── 1. Template copy asserts nothing it did not measure ──

@pytest.mark.parametrize("sp", [WEAK, STRONG, YOUNG, None],
                         ids=["weak", "strong", "young", "no_listing"])
def test_template_copy_makes_no_unsupported_quality_claims(sp):
    text = _all_text(template_narratives(_prop(sp), _comps()))
    found = [p for p in UNSUPPORTED if p in text]
    assert not found, f"template copy still asserts {found}"


def test_template_copy_with_no_comps_makes_no_unsupported_claims():
    text = _all_text(template_narratives(_prop(None), []))
    assert not [p for p in UNSUPPORTED if p in text]


def test_weak_subject_is_described_as_below_the_comp_median():
    summary = template_narratives(_prop(WEAK), _comps()).positioning_summary
    assert "below" in summary
    assert "24%" in summary


def test_strong_subject_is_described_as_above_the_comp_median():
    summary = template_narratives(_prop(STRONG), _comps()).positioning_summary
    assert "above" in summary


def test_young_subject_is_not_ranked_against_the_comps():
    summary = template_narratives(_prop(YOUNG), _comps()).positioning_summary
    assert "full year" in summary
    assert "below" not in summary and "above" not in summary


def test_unlisted_subject_is_not_given_a_track_record():
    summary = template_narratives(_prop(None), _comps()).positioning_summary
    assert "track record" in summary


# ── 2. Key Assumptions match the basis the projection used ──

def test_subject_anchored_projection_does_not_assume_new_management():
    m = build_methodology(
        _prop(WEAK), _comps(), calculator=CalculatorDefaults(occ_basis="subject"),
    )
    joined = " ".join(m.assumptions).lower()
    assert "professional property management" not in joined
    assert "premium photography" not in joined
    assert "operating as it has" in joined


def test_market_anchored_projection_keeps_the_operator_assumptions():
    m = build_methodology(
        _prop(None), _comps(), calculator=CalculatorDefaults(occ_basis="market_pool"),
    )
    joined = " ".join(m.assumptions).lower()
    assert "professional property management" in joined


def test_unlisted_subject_on_market_median_is_not_called_a_young_listing():
    """Found by the fix below: once an address subject gets coordinates it
    reaches the market-median basis, whose copy was written for a listing
    under a year old. The Gatlinburg re-run printed "has not been listed a
    full year" and "its own measured result is shown below" for a house that
    has never been listed."""
    from report.template_engine import occupancy_basis_text

    calc = CalculatorDefaults(occ_basis="market_typical")
    for text in (
        occupancy_basis_text(calc, None),
        build_methodology(_prop(None), _comps(), calculator=calc).assumptions[0],
    ):
        assert "full year" not in text
        assert "measured result" not in text
        assert "no Airbnb history" in text


def test_young_listing_on_market_median_keeps_its_wording():
    from report.template_engine import occupancy_basis_text

    calc = CalculatorDefaults(occ_basis="market_typical")
    assert "full year" in occupancy_basis_text(calc, YOUNG)
    assert "full year" in build_methodology(
        _prop(YOUNG), _comps(), calculator=calc).assumptions[0]


# ── 3. Address subjects adopt the coordinates AirROI geocoded ──

GATLINBURG_ESTIMATE = {"location": {"latitude": 35.7461037, "longitude": -83.4811331}}


def test_address_subject_adopts_estimate_coordinates():
    prop = _prop(None, market="Gatlinburg")
    assert prop.latitude is None
    assert _adopt_estimate_location(prop, GATLINBURG_ESTIMATE) is True
    assert (prop.latitude, prop.longitude) == (35.7461037, -83.4811331)


def test_listing_coordinates_are_never_overwritten():
    prop = _prop(None, latitude=28.33, longitude=-81.62)
    assert _adopt_estimate_location(prop, GATLINBURG_ESTIMATE) is False
    assert (prop.latitude, prop.longitude) == (28.33, -81.62)


@pytest.mark.parametrize("estimate", [
    {}, {"location": None}, {"location": {"latitude": None, "longitude": -83.4}},
    {"location": {"latitude": "35.7", "longitude": -83.4}}, None,
])
def test_missing_or_malformed_location_changes_nothing(estimate):
    prop = _prop(None)
    assert _adopt_estimate_location(prop, estimate) is False
    assert prop.latitude is None and prop.longitude is None


# ── 4. Data Sources names the seasonal source that was actually used ──

def _sources(basis: str | None) -> str:
    kw = {} if basis is None else {"seasonal_basis": basis}
    return " ".join(build_methodology(_prop(None), _comps(), **kw).data_sources)


def test_market_curve_is_claimed_when_it_was_used():
    assert "AirROI market curve" in _sources("market")


@pytest.mark.parametrize("basis", ["comps", "", None])
def test_market_curve_is_not_claimed_when_it_was_not_used(basis):
    text = _sources(basis)
    assert "market curve" not in text
    assert "whole-market" not in text


def test_comp_average_source_is_named_as_the_six_comps():
    assert "six comparables" in _sources("comps")
