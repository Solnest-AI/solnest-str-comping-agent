"""
Sanity gate tests — guard the Phase A + Phase B blocking checks.

Phase A (pre-render) catches data issues BEFORE Jinja runs:
  - missing fields on comps, dead Airbnb URLs, untrusted hero source,
    bogus revenue numbers, calculator defaults missing.
Phase B (post-render) parses the rendered HTML and catches presentation issues:
  - <4 or >6 comp cards, dead images in HTML, residual {{ }} or "AirDNA",
    missing required sections.

FIXTURE DISCIPLINE (rewritten 2026-08-29):
`_make_good_comp()` used to hardcode `days_available=365` with `occupancy_pct=65`
— a listing simultaneously empty all year and 65% booked. Everything is now
derived from `booked_nights`, and `test_good_comp_fixture_is_internally_consistent`
enforces that. The revenue gate it exercises used to divide revenue by UNSOLD
nights, which made it an occupancy filter and blocked reports in 5 of 6 markets.

Run: pytest tests/test_sanity.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from validators.sanity import (
    run_phase_a, run_phase_b,
    _check_comp_fields, _check_revenue_sanity,
    _REVENUE_RATIO_MIN, _REVENUE_RATIO_MAX,
    _TRUSTED_HERO_HOSTS,
)
from schema import (
    PropertyBasics, RentalizerData, CompProperty,
    CalculatorDefaults, ReportData,
)


# ── Fixture builders ────────────────────────────────────────────────────

# Corpus fee multiplier: ttm_revenue / (ttm_avg_rate x nights_booked).
# Median 1.192 over 200 live records. Used to keep the fixture on the same
# fee-inclusive basis AirROI actually reports.
_FEE_FACTOR = 1.19
# Achievable-occupancy ceiling used to derive a plausible revenue potential.
_OCC_CEILING = 0.65


def _make_good_comp(
    name: str = "Solid 3BR Comp",
    airbnb_id: str = "12345",
    *,
    booked_nights: int = 215,
    blocked_nights: int = 15,
    adr: float = 400.0,
) -> CompProperty:
    """A comp that passes all field-completeness checks.

    Everything is DERIVED from `booked_nights` so the comp is a listing that
    could actually exist:
        nights_listed  = 365 - blocked          (open inventory)
        occupancy_pct  = booked / nights_listed (ADJUSTED occupancy)
        annual_revenue = adr x booked x fee multiplier   (fee-INCLUSIVE)
        revpar         = adr x booked / 365
        potential      = adr x listed x ceiling x fee multiplier
    """
    nights_listed = 365 - blocked_nights
    assert 0 < booked_nights <= nights_listed, "fixture must be physically possible"
    occupancy_pct = round(booked_nights / nights_listed * 100, 2)
    room_revenue = adr * booked_nights
    annual_revenue = round(room_revenue * _FEE_FACTOR, 2)
    revenue_potential = round(adr * nights_listed * _OCC_CEILING * _FEE_FACTOR, 2)
    revpar = round(room_revenue / 365, 2)

    return CompProperty(
        name=name,
        image_url=f"https://a0.muscache.com/im/pictures/{airbnb_id}.jpg",
        sleeps=6, bedrooms=3, bathrooms=2.0,
        rating=4.85, review_count=42,
        feature_badges=["Ski-in/Out", "Hot Tub"],
        badge_emojis=["\U0001F3BF", "\U0001F6C1"],
        revenue_potential=revenue_potential,
        annual_revenue=annual_revenue,
        occupancy_pct=occupancy_pct,
        adr=adr,
        nights_booked=booked_nights,
        nights_listed=nights_listed,
        revpar=revpar,
        l90d_nights_booked=58,
        l90d_occupancy_pct=round(58 / 87 * 100, 2),
        airbnb_url=f"https://www.airbnb.com/rooms/{airbnb_id}",
    )


def test_good_comp_fixture_is_internally_consistent():
    """Regression guard on the fixture itself.

    The old builder claimed 365 available nights AND 65% occupancy, which is
    exactly the contradiction the tool shipped to clients.
    """
    c = _make_good_comp()
    assert 0 < c.nights_booked <= c.nights_listed <= 365
    assert c.occupancy_pct == pytest.approx(c.nights_booked / c.nights_listed * 100, abs=0.01)
    assert c.annual_revenue == pytest.approx(c.adr * c.nights_booked * _FEE_FACTOR, rel=1e-6)
    assert c.revenue_potential >= c.annual_revenue, "potential is a ceiling, not a floor"
    assert c.revpar == pytest.approx(c.adr * c.nights_booked / 365, abs=0.01)


def _make_good_report() -> ReportData:
    """A complete report fixture that should pass Phase A entirely."""
    prop = PropertyBasics(
        address="5005 Valley Drive Unit 13, Sun Peaks BC",
        short_address="Test Subject",
        market="Sun Peaks",
        bedrooms=2, bathrooms=2, max_guests=6,
        property_type="Ski Condo",
        hero_image_url="https://cdn.realtor.ca/listings/abc/highres/1/hero.jpg",
    )
    rentalizer = RentalizerData(revenue_potential=80000, adr=350, occupancy_pct=55)
    comps = [_make_good_comp(f"Comp {i+1}", airbnb_id=f"100{i+1}") for i in range(6)]
    calc = CalculatorDefaults(
        adr_default=350, adr_min=200, adr_max=550,
        occ_default=55, occ_min=30, occ_max=80,
        days_default=350,
    )
    return ReportData(
        property=prop, rentalizer=rentalizer, comps=comps, calculator=calc,
        report_date="April 24, 2026",
    )


# ── Per-comp field checks ──────────────────────────────────────────────

def test_check_comp_fields_passes_complete_comp():
    comp = _make_good_comp()
    errors = _check_comp_fields(comp)
    assert errors == [], f"Expected no errors, got: {errors}"


def test_check_comp_fields_catches_missing_image():
    comp = _make_good_comp()
    comp.image_url = ""
    errors = _check_comp_fields(comp)
    assert any("image_url" in e for e in errors)


def test_check_comp_fields_catches_out_of_range_rating():
    """A rating of 0 is out of range for a RATED comp."""
    comp = _make_good_comp()
    comp.rating = 0
    errors = _check_comp_fields(comp)
    assert any("rating" in e for e in errors)


def test_check_comp_fields_allows_unrated_comp():
    """rating=None means 'too few reviews to rate' — a valid AirROI state.

    It is NOT a missing field, and it must not block a report. AirROI signals
    it with rating_overall == 0.0; the adapter maps that sentinel to None.
    """
    comp = _make_good_comp()
    comp.rating = None
    errors = _check_comp_fields(comp)
    assert not any("rating" in e for e in errors), (
        f"Unrated comp should not fail the field check; errors: {errors}"
    )


def test_check_comp_fields_catches_invalid_occupancy():
    comp = _make_good_comp()
    comp.occupancy_pct = 150  # invalid > 100
    errors = _check_comp_fields(comp)
    assert any("occupancy" in e for e in errors)


def test_check_comp_fields_catches_zero_booked_nights():
    """A listing that booked nothing all year is not a comparable operator.

    The old check was `1 <= days_available <= 365`, which fired on 0 of 150
    live comps and 0 of 1 on the constructed case it existed to catch.
    """
    comp = _make_good_comp()
    comp.nights_booked = 0
    errors = _check_comp_fields(comp)
    assert any("booked" in e.lower() for e in errors), errors


def test_check_comp_fields_catches_impossible_night_accounting():
    """More booked nights than exist in a year must be caught."""
    comp = _make_good_comp()
    comp.nights_booked = 500
    errors = _check_comp_fields(comp)
    assert any("nights_booked" in e for e in errors), errors


def test_check_comp_fields_allows_studio():
    """Studios (0 bedrooms) are valid — should NOT be flagged."""
    comp = _make_good_comp()
    comp.bedrooms = 0
    errors = _check_comp_fields(comp)
    assert not any("bedrooms" in e for e in errors), (
        f"0 bedrooms (studio) should be valid; errors: {errors}"
    )


def test_check_comp_fields_catches_negative_bedrooms():
    """Negative is invalid; -1 bedroom isn't real."""
    comp = _make_good_comp()
    comp.bedrooms = -1
    errors = _check_comp_fields(comp)
    assert any("bedrooms" in e for e in errors)


# ── Revenue sanity ───────────────────────────────────────────────────────

def test_revenue_sanity_passes_normal_comp():
    """revenue / (ADR x booked nights) is a FEE MULTIPLIER, ~1.19 on live data."""
    comp = _make_good_comp()
    ratio = comp.annual_revenue / (comp.adr * comp.nights_booked)
    assert _REVENUE_RATIO_MIN < ratio < _REVENUE_RATIO_MAX
    assert _check_revenue_sanity(comp) is None


def test_revenue_sanity_passes_high_occupancy_comp():
    """The gate must NOT reject a well-booked listing.

    Regression guard: the old gate divided revenue by UNSOLD nights, so the
    better a comp performed the more likely it was rejected. It blocked report
    generation entirely in 5 of 6 real markets.
    """
    comp = _make_good_comp(booked_nights=320, blocked_nights=5)
    assert comp.occupancy_pct > 88
    assert _check_revenue_sanity(comp) is None, (
        "a 320-night comp is the best kind of comp, not a data error"
    )


def test_revenue_sanity_catches_revenue_below_room_revenue():
    """Revenue far below ADR x booked nights means the mapping is broken."""
    comp = _make_good_comp()
    comp.annual_revenue = 5000   # 5000 / 86000 = 5.8% of room revenue
    msg = _check_revenue_sanity(comp)
    assert msg is not None
    assert "below" in msg and f"{_REVENUE_RATIO_MIN:.0%}" in msg


def test_revenue_sanity_catches_impossible_fee_multiplier():
    """Revenue far above ADR x booked nights is not a fee multiplier."""
    comp = _make_good_comp()
    comp.annual_revenue = 400000   # 400000 / 86000 = 465% of room revenue
    msg = _check_revenue_sanity(comp)
    assert msg is not None
    assert "exceeds" in msg and f"{_REVENUE_RATIO_MAX:.0%}" in msg


# ── Phase A end-to-end ───────────────────────────────────────────────────

def test_phase_a_passes_complete_report():
    """A fully-populated report fixture should sail through Phase A.
    NOTE: this test makes real HTTP HEAD requests to muscache + realtor.ca.
    The fixture URLs are placeholder so they'll likely 404 — accept that;
    we're validating the FIELD-level checks here, not URL liveness.
    """
    report = _make_good_report()
    failures = asyncio.run(run_phase_a(report))
    # Strip out the URL-liveness failures since fixture URLs are fake
    field_failures = [f for f in failures if "HTTP" not in f and "raised" not in f]
    assert field_failures == [], (
        f"Expected only URL-related failures (fixture URLs are placeholder); "
        f"got field failures: {field_failures}"
    )


def test_phase_a_blocks_on_missing_comps():
    report = _make_good_report()
    report.comps = report.comps[:3]  # only 3 — below the 4-comp floor
    failures = asyncio.run(run_phase_a(report))
    assert any("Only 3 comps" in f for f in failures), (
        f"Expected 'Only 3 comps' block, got: {failures}"
    )


def test_phase_a_blocks_on_5_comps():
    """5 comps should block — we always require exactly 6."""
    report = _make_good_report()
    report.comps = report.comps[:5]
    failures = asyncio.run(run_phase_a(report))
    block_failures = [f for f in failures if "Only" in f and "comps" in f]
    assert len(block_failures) > 0, f"5 comps should block; got: {failures}"


def test_phase_a_blocks_on_untrusted_hero():
    """Subject hero from a random domain (not realtor.ca / muscache.com) → block."""
    report = _make_good_report()
    report.property.hero_image_url = "https://random-stock-photos.example.com/xyz.jpg"
    failures = asyncio.run(run_phase_a(report))
    assert any("untrusted source" in f for f in failures), (
        f"Expected untrusted-source failure, got: {failures}"
    )


def test_trusted_hosts_includes_critical_sources():
    """Anti-regression: muscache (Airbnb) and realtor.ca (MLS) must be trusted."""
    assert any("muscache" in h for h in _TRUSTED_HERO_HOSTS)
    assert any("realtor.ca" in h for h in _TRUSTED_HERO_HOSTS)


def test_phase_a_blocks_on_missing_calculator_defaults():
    report = _make_good_report()
    report.calculator.adr_default = 0    # zero is invalid
    failures = asyncio.run(run_phase_a(report))
    assert any("calculator.adr_default" in f for f in failures)


# ── Phase B (HTML parsing) ──────────────────────────────────────────────

def _write_test_html(tmp_path: Path, body: str) -> Path:
    """Wrap a body fragment in a minimal HTML doc and write to tmp_path."""
    html = f"""<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>Subject Property header</header>
<section>Subject Property Details</section>
<section>Market Positioning</section>
<section>Revenue Projections</section>
<section>Performance Analytics seasonal chart</section>
<section>Market Comparables</section>
<section>Methodology</section>
{body}
</body></html>"""
    p = tmp_path / "test.html"
    p.write_text(html, encoding="utf-8")
    return p


def test_phase_b_passes_clean_html(tmp_path):
    cards = "".join([
        f'<div class="comp-card"><img src="https://a0.muscache.com/x{i}.jpg"></div>'
        for i in range(6)
    ])
    p = _write_test_html(tmp_path, cards)
    failures = asyncio.run(run_phase_b(p))
    # Fixture image URLs are placeholder; ignore HTTP failures
    structural = [f for f in failures if "HTTP" not in f and "raised" not in f]
    assert structural == [], f"Expected structural pass; got: {structural}"


def test_phase_b_blocks_on_residual_airdna(tmp_path):
    cards = "".join([
        '<div class="comp-card"><img src="x.jpg"><span>AirDNA Verified</span></div>'
        for _ in range(6)
    ])
    p = _write_test_html(tmp_path, cards)
    failures = asyncio.run(run_phase_b(p))
    assert any("AirDNA" in f for f in failures)


def test_phase_b_blocks_on_jinja_artifact(tmp_path):
    cards = "".join([
        '<div class="comp-card"><img src="x.jpg">{{ broken_var }}</div>'
        for _ in range(6)
    ])
    p = _write_test_html(tmp_path, cards)
    failures = asyncio.run(run_phase_b(p))
    assert any("Jinja artifacts" in f for f in failures)


def test_phase_b_blocks_on_too_few_cards(tmp_path):
    cards = "".join([
        f'<div class="comp-card"><img src="x{i}.jpg"></div>'
        for i in range(3)   # only 3 cards
    ])
    p = _write_test_html(tmp_path, cards)
    failures = asyncio.run(run_phase_b(p))
    assert any("too few" in f for f in failures)


def test_phase_b_blocks_on_5_cards(tmp_path):
    """5 cards should block — we always require exactly 6."""
    cards = "".join([
        f'<div class="comp-card"><img src="x{i}.jpg"></div>'
        for i in range(5)
    ])
    p = _write_test_html(tmp_path, cards)
    failures = asyncio.run(run_phase_b(p))
    card_failures = [f for f in failures if ".comp-card" in f]
    assert len(card_failures) > 0, f"5 cards should block; got: {card_failures}"


def test_phase_b_blocks_on_missing_required_section(tmp_path):
    """Required sections include the seven keyword headings; missing one fails."""
    p = tmp_path / "missing.html"
    p.write_text(
        "<!DOCTYPE html><html><body>"
        '<div class="comp-card"></div>' * 6
        + "</body></html>",  # no section headings
        encoding="utf-8",
    )
    failures = asyncio.run(run_phase_b(p))
    assert any("Missing required section" in f for f in failures)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
