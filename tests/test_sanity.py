"""
Sanity gate tests — guard the Phase A + Phase B blocking checks.

Phase A (pre-render) catches data issues BEFORE Jinja runs:
  - missing fields on comps, dead Airbnb URLs, untrusted hero source,
    bogus revenue numbers, calculator defaults missing.
Phase B (post-render) parses the rendered HTML and catches presentation issues:
  - <4 or >6 comp cards, dead images in HTML, residual {{ }} or "AirDNA",
    missing required sections.

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
    _TRUSTED_HERO_HOSTS,
)
from schema import (
    PropertyBasics, RevenueEstimate, CompProperty,
    CalculatorDefaults, ReportData,
)


# ── Fixture builders ────────────────────────────────────────────────────

def _make_good_comp(name: str = "Solid 3BR Comp", airbnb_id: str = "12345") -> CompProperty:
    """A comp that passes all field-completeness checks."""
    return CompProperty(
        name=name,
        image_url=f"https://a0.muscache.com/im/pictures/{airbnb_id}.jpg",
        sleeps=6, bedrooms=3, bathrooms=2.0,
        rating=4.85, review_count=42,
        feature_badges=["Ski-in/Out", "Hot Tub"],
        badge_emojis=["🎿", "🛁"],
        revenue_potential=100000, annual_revenue=85000,
        occupancy_pct=65, adr=400, days_available=365,
        airbnb_url=f"https://www.airbnb.com/rooms/{airbnb_id}",
    )


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
    revenue_estimate = RevenueEstimate(revenue_potential=80000, adr=350, occupancy_pct=55)
    comps = [_make_good_comp(f"Comp {i+1}", airbnb_id=f"100{i+1}") for i in range(6)]
    calc = CalculatorDefaults(
        adr_default=350, adr_min=200, adr_max=550,
        occ_default=55, occ_min=30, occ_max=80,
        days_default=365,
    )
    return ReportData(
        property=prop, revenue_estimate=revenue_estimate, comps=comps, calculator=calc,
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


def test_check_comp_fields_warns_on_zero_rating():
    """Zero rating (no reviews yet) is a warning, not a blocking error.
    In thin markets, new listings may be the only comps available."""
    comp = _make_good_comp()
    comp.rating = 0
    errors = _check_comp_fields(comp)
    # Should NOT be in errors (it's a warning printed to stderr)
    assert not any("rating" in e for e in errors), (
        f"Zero rating should be a warning, not a block: {errors}"
    )


def test_check_comp_fields_catches_invalid_occupancy():
    comp = _make_good_comp()
    comp.occupancy_pct = 150  # invalid > 100
    errors = _check_comp_fields(comp)
    assert any("occupancy" in e for e in errors)


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
    """A comp with reasonable revenue ratios should pass."""
    comp = _make_good_comp()
    # ADR 400 × 365 = 146000; revenue 85000 / 146000 = 0.58 → within [0.15, 1.6]
    msg = _check_revenue_sanity(comp)
    assert msg is None, f"Expected pass, got: {msg}"


def test_revenue_sanity_catches_unreasonably_low_revenue():
    """Revenue < 15% of (ADR × days) indicates inactive listing."""
    comp = _make_good_comp()
    comp.annual_revenue = 5000   # 5000 / 146000 = 3.4% → too low
    msg = _check_revenue_sanity(comp)
    assert msg is not None and "<15%" in msg


def test_revenue_sanity_catches_unreasonably_high_revenue():
    """Revenue > 300% of (ADR × days) indicates a data error."""
    comp = _make_good_comp()
    comp.annual_revenue = 500000   # 500000 / 146000 = 342% → too high
    msg = _check_revenue_sanity(comp)
    assert msg is not None and "exceeds 300%" in msg


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
    """Subject hero from a random domain is now a warning, not a block.
    The report should still generate — users can provide their own photo."""
    report = _make_good_report()
    report.property.hero_image_url = "https://random-stock-photos.example.com/xyz.jpg"
    failures = asyncio.run(run_phase_a(report))
    # Hero source is no longer a blocking failure — just a stderr warning
    hero_failures = [f for f in failures if "untrusted source" in f]
    assert len(hero_failures) == 0, (
        f"Untrusted hero should be a warning, not a block: {failures}"
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
