"""Fixes Codex made BY HAND to the delivered Sunburst report, ported to source.

Codex corrected nine things directly in one generated HTML file on 2026-09-20.
None of them reached the template or the generators, so every later report
shipped without them. Each test below pins one of those corrections at the
source, so a re-render can never quietly undo it again.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from generators.calculator import derive_season_labels
from generators.methodology import build_methodology
from generators.narratives import template_narratives
from report.template_engine import render_report
from schema import (
    CalculatorDefaults, CompProperty, Narratives, PropertyBasics, RentalizerData,
    ReportData, SubjectPerformance,
)


def _comps() -> list[CompProperty]:
    revs = [148_357, 168_684, 156_944, 134_254, 101_539, 84_937]
    occs = [78.6, 56.4, 55.7, 45.7, 38.6, 24.5]
    return [
        CompProperty(
            name=f"Comp {i}", image_url="https://example.com/i.jpg", sleeps=10,
            bedrooms=4, bathrooms=2.5, rating=4.9, review_count=50,
            annual_revenue=r, revenue_potential=r * 1.1, occupancy_pct=o,
            adr=800, nights_booked=150, nights_listed=330,
            airbnb_url="https://www.airbnb.com/rooms/1",
        )
        for i, (r, o) in enumerate(zip(revs, occs))
    ]


def _data(*, market_band: bool = True) -> ReportData:
    prop = PropertyBasics(
        address="Sun Peaks, BC", short_address="Cabin", market="Sun Peaks",
        bedrooms=4, bathrooms=2.5, max_guests=12, currency="CA$",
        subject_performance=SubjectPerformance(
            annual_revenue=48_183, occupancy_pct=20.0, adr=834.4,
            nights_booked=49, nights_listed=245, months_with_data=3,
        ),
    )
    comps = _comps()
    peak, shoulder = derive_season_labels(
        [0.15, 0.17, 0.15, 0.04, 0.04, 0.05, 0.08, 0.07, 0.05, 0.04, 0.04, 0.12])
    monthly = [None] * 12
    monthly[5], monthly[6], monthly[7] = 53.3, 38.7, 61.3
    return ReportData(
        property=prop,
        rentalizer=RentalizerData(revenue_potential=150_068, adr=860, occupancy_pct=43),
        comps=comps,
        calculator=CalculatorDefaults(occ_basis="market_strong", adr_basis="subject"),
        narratives=Narratives(peak_season_label=peak, shoulder_season_label=shoulder),
        methodology=build_methodology(prop, comps, peak, shoulder),
        seasonal_data=[45.0] * 12,
        seasonal_p25=[20.0] * 12 if market_band else [],
        seasonal_p75=[60.0] * 12 if market_band else [],
        subject_monthly=monthly,
        report_date="September 24, 2026",
    )


def _text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ")


def test_comp_section_no_longer_asserts_the_subject_outranks_the_market():
    """Printed on every report, including subjects below the comp median."""
    assert "caliber is above typical inventory" not in _text(render_report(_data()))


def test_comp_section_states_the_real_comp_revenue_median_and_range():
    text = _text(render_report(_data()))
    assert "CA$141,306" in text           # median of the six
    assert "CA$84,937" in text and "CA$168,684" in text


def test_trailing_block_is_labelled_as_airroi_reported_not_actual():
    text = _text(render_report(_data()))
    assert "(Actual)" not in text
    assert "Revenue Earned" not in text
    assert "AirROI Reported Trailing Performance" in text
    assert "Reported Gross Revenue" in text


def test_comp_card_occupancy_keeps_one_decimal():
    text = _text(render_report(_data()))
    assert "78.6%" in text and "24.5%" in text


def test_occupancy_slider_says_what_the_percentage_is_of():
    text = _text(render_report(_data()))
    assert "Annual Occupancy Rate" not in text
    assert "Occupancy of Listed Nights" in text


def test_revenue_headline_has_a_mobile_size_rule():
    """Codex measured the headline overflowing a 390px viewport."""
    html = render_report(_data())
    assert "@media (max-width: 640px)" in html
    assert ".stat-number.text-6xl" in html


def test_chart_says_it_is_the_whole_market_not_the_six_comps():
    text = _text(render_report(_data()))
    assert "shading shows the 25th-75th percentiles" in text.lower()
    assert "not only the six" in text.lower()
    assert "left blank" in text.lower()


def test_chart_note_does_not_claim_a_market_band_it_does_not_have():
    text = _text(render_report(_data(market_band=False))).lower()
    assert "shading shows the 25th-75th percentiles" not in text


def test_no_em_dash_in_visible_report_text():
    """Client deliverable under Solnest branding: the brand rule is zero."""
    data = _data()
    data.narratives = template_narratives(
        data.property, data.comps,
        peak_season_label=data.narratives.peak_season_label,
        shoulder_season_label=data.narratives.shoulder_season_label,
    )
    text = _text(render_report(data))
    assert "—" not in text, [ln for ln in text.splitlines() if "—" in ln]
