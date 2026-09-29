"""Regression tests from the 2026-09-28 codebase review: generators, validators,
template engine, scripts and CI. Hermetic: no network, no keys.

Each test names the failure it guards, so a future edit that reintroduces the
bug fails with the reason attached.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brand_from_website as bw  # noqa: E402
import check_setup as cs  # noqa: E402
import config  # noqa: E402
import install_launcher as il  # noqa: E402
import setup as setup_script  # noqa: E402
from generators import narratives as N  # noqa: E402
from generators.calculator import (  # noqa: E402
    _interpolate_gaps,
    derive_calculator_defaults,
    derive_season_labels,
    market_months_missing,
    occupancy_band_text,
)
from generators.methodology import build_methodology  # noqa: E402
from generators.narrative_brief import (  # noqa: E402
    NARRATIVE_INPUT_SCHEMA,
    comp_occupancy_line,
    months_from_label,
    share_from_label,
)
from report import template_engine as te  # noqa: E402
from schema import (  # noqa: E402
    CalculatorDefaults,
    CompProperty,
    Narratives,
    PropertyBasics,
    RentalizerData,
    ReportData,
    SubjectPerformance,
)
from validators import sanity  # noqa: E402


# ── builders ────────────────────────────────────────────────────────────

def _comp(i: int = 0, occ: float = 60.0, **kw) -> CompProperty:
    base = dict(
        name=f"Comp {i}", image_url="https://a0.muscache.com/im/pictures/x.jpg",
        sleeps=6, bedrooms=3, bathrooms=2.0, rating=4.8, review_count=40,
        annual_revenue=90_000, revenue_potential=120_000, occupancy_pct=occ,
        adr=300, nights_booked=200, nights_listed=330,
        airbnb_url="https://www.airbnb.com/rooms/1",
    )
    base.update(kw)
    return CompProperty(**base)


def _prop(**kw) -> PropertyBasics:
    base = dict(address="1 Main St, Testville", short_address="Test House",
                market="Testville", bedrooms=3, bathrooms=2.0, max_guests=6)
    base.update(kw)
    return PropertyBasics(**base)


def _rent() -> RentalizerData:
    return RentalizerData(revenue_potential=120_000, adr=300, occupancy_pct=60)


def _report(**calc) -> ReportData:
    return ReportData(
        property=_prop(hero_image_url="https://a0.muscache.com/im/pictures/h.jpg",
                       airbnb_url="https://www.airbnb.com/rooms/9"),
        rentalizer=_rent(),
        comps=[_comp(i) for i in range(6)],
        calculator=CalculatorDefaults(**calc),
        narratives=Narratives(positioning_summary="copy"),
        seasonal_data=[50.0] * 12,
        report_date="September 28, 2026",
    )


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
# 1. Season labels never invent a revenue share
# ══════════════════════════════════════════════════════════════════════════

OCC_SERIES = [30, 30, 35, 40, 55, 70, 85, 80, 60, 45, 35, 30]
DIST = [0.03, 0.04, 0.06, 0.08, 0.10, 0.13, 0.15, 0.14, 0.09, 0.07, 0.06, 0.05]


def test_occupancy_fallback_names_months_but_not_a_revenue_share():
    peak, shoulder = derive_season_labels(None, OCC_SERIES)
    assert months_from_label(peak) and shoulder
    assert "revenue" not in peak and "%" not in peak
    assert share_from_label(peak) is None


def test_revenue_distribution_still_carries_its_share():
    peak, _ = derive_season_labels(DIST, OCC_SERIES)
    assert share_from_label(peak) is not None and "of annual revenue" in peak


@pytest.mark.parametrize("dead", [[0.0] * 12, [None] * 12])
def test_an_empty_distribution_falls_back_to_the_seasonal_series(dead):
    peak, shoulder = derive_season_labels(dead, OCC_SERIES)
    assert months_from_label(peak) == "Jun-Sep" and shoulder
    assert share_from_label(peak) is None


def test_nothing_usable_gives_no_labels():
    assert derive_season_labels([0.0] * 12, [0.0] * 12) == ("", "")
    assert derive_season_labels(None, None) == ("", "")


# ══════════════════════════════════════════════════════════════════════════
# 2. Missing months count absent rows as well as zero rows
# ══════════════════════════════════════════════════════════════════════════

def _row(month: int, p50=0.4, dead=False) -> dict:
    v = 0.0 if dead else p50
    return {"date": f"2026-{month:02d}-01", "avg": v, "p25": v if dead else 0.3,
            "p50": v, "p75": v if dead else 0.5, "p90": v if dead else 0.6}


def test_absent_months_are_counted_as_missing():
    rows = [_row(m) for m in range(1, 10)]           # only nine rows came back
    assert market_months_missing(rows) == 3


def test_absent_and_zero_rows_add_up_and_duplicates_count_once():
    rows = [_row(m) for m in range(1, 9)] + [_row(9, dead=True), _row(1)]
    assert market_months_missing(rows) == 4          # Sep dead; Oct-Dec absent
    assert market_months_missing([_row(m) for m in range(1, 13)]) == 0


def test_no_market_response_is_not_twelve_missing_months():
    assert market_months_missing([]) == 0
    assert market_months_missing(None) == 0


# ══════════════════════════════════════════════════════════════════════════
# 3. Consecutive gaps interpolate linearly between real months
# ══════════════════════════════════════════════════════════════════════════

def test_a_run_of_gaps_fills_as_a_straight_line():
    series = [10.0, None, None, None, 50.0] + [50.0] * 7
    filled = _interpolate_gaps(series)
    assert filled[1:4] == pytest.approx([20.0, 30.0, 40.0])


def test_a_single_gap_is_still_the_neighbour_average():
    series = [10.0, None, 30.0] + [30.0] * 9
    assert _interpolate_gaps(series)[1] == pytest.approx(20.0)


def test_gaps_wrap_across_year_end_and_too_many_gaps_give_up():
    series = [None, 40.0] + [40.0] * 9 + [20.0]
    assert _interpolate_gaps(series)[0] == pytest.approx(30.0)
    assert _interpolate_gaps([None] * 4 + [40.0] * 8) is None


# ══════════════════════════════════════════════════════════════════════════
# 4. Occupancy bands round inward
# ══════════════════════════════════════════════════════════════════════════

def test_band_bounds_round_inward():
    assert occupancy_band_text(61.4, 88.6) == "62-88%"
    assert occupancy_band_text(61.0, 89.0) == "61-89%"


def test_band_with_no_whole_number_inside_stays_inside_the_data():
    assert occupancy_band_text(61.4, 61.6) == "61.4-61.6%"
    assert occupancy_band_text(61.44, 61.44) == "61.4%"
    assert occupancy_band_text(62.0, 62.0) == "62%"


def test_calculator_range_text_never_exceeds_the_comps():
    comps = [_comp(0, occ=61.4), _comp(1, occ=75.0), _comp(2, occ=88.6)]
    calc = derive_calculator_defaults(comps, _rent(), _prop())
    assert "observed 62-88%" in calc.occ_range_text


def test_comp_occupancy_line_rounds_the_range_inward():
    line = comp_occupancy_line({"n": 6, "median": 75.0, "low": 61.4, "high": 88.6})
    assert "observed range 62-88%" in line


# ══════════════════════════════════════════════════════════════════════════
# 5. --narratives file: BOM and encoding
# ══════════════════════════════════════════════════════════════════════════

_GOOD = {
    **{f: "text" for f in ("positioning_summary", "guest_profile", "amenity_upside",
                           "config_description", "guests_description",
                           "peak_season_text", "shoulder_season_text")},
    "amenity_badges": [{"emoji": "x", "text": "t"}] * 4,
    "positioning_cards": [{"emoji": "x", "title": "t", "text": "t"}] * 3,
}


def test_a_utf8_bom_file_loads(tmp_path):
    p = tmp_path / "n.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(_GOOD).encode("utf-8"))
    assert N.load_narratives_from_file(p).positioning_summary == "text"


def test_a_non_utf8_file_raises_narrative_file_error_naming_the_encoding(tmp_path):
    p = tmp_path / "n.json"
    p.write_bytes(json.dumps({**_GOOD, "guest_profile": "café"}, ensure_ascii=False).encode("cp1252"))
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(p)
    assert "UTF-8" in str(e.value)


def test_a_json_written_by_powershell_utf16_is_reported_not_crashed(tmp_path):
    p = tmp_path / "n.json"
    p.write_text(json.dumps(_GOOD), encoding="utf-16")
    with pytest.raises(N.NarrativeFileError):
        N.load_narratives_from_file(p)


# ══════════════════════════════════════════════════════════════════════════
# 6. API path validates, retries, and withholds a young listing's occupancy
# ══════════════════════════════════════════════════════════════════════════

class _Block:
    def __init__(self, payload):
        self.type = "tool_use"
        self.input = payload


def _fake_anthropic(payloads, calls):
    queue = list(payloads)

    class _Messages:
        async def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=[_Block(queue.pop(0) if len(queue) > 1 else queue[0])])

    class _Client:
        def __init__(self, **kw):
            self.messages = _Messages()

    return SimpleNamespace(AsyncAnthropic=_Client, APIError=RuntimeError)


def test_schema_requires_both_lists():
    assert {"amenity_badges", "positioning_cards"} <= set(NARRATIVE_INPUT_SCHEMA["required"])


def test_api_response_missing_the_lists_is_retried_then_accepted(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    bad = {k: v for k, v in _GOOD.items() if k != "positioning_cards"}
    calls: list = []
    monkeypatch.setattr(N, "anthropic", _fake_anthropic([bad, _GOOD], calls))
    out = _run(N.generate_narratives(_prop(), _rent(), [_comp()], CalculatorDefaults()))
    assert len(calls) == 2 and len(out.positioning_cards) == 3


def test_api_response_that_never_validates_falls_back_to_template_copy(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    calls: list = []
    monkeypatch.setattr(N, "anthropic", _fake_anthropic([{**_GOOD, "guest_profile": " "}], calls))
    comps = [_comp()]
    out = _run(N.generate_narratives(_prop(), _rent(), comps, CalculatorDefaults()))
    assert len(calls) == 3
    assert out.guest_profile == N.template_narratives(_prop(), comps).guest_profile


def test_api_prompt_withholds_a_young_listings_occupancy_and_open_nights():
    young = _prop(subject_performance=SubjectPerformance(
        annual_revenue=48_183, occupancy_pct=20.0, adr=834.4,
        nights_booked=49, nights_listed=245, months_with_data=3))
    prompt = N._build_prompt(young, _rent(), [_comp()], CalculatorDefaults(), "", "")
    assert "Trailing 12 months adjusted occupancy" not in prompt
    assert "open nights" not in prompt and "Nights booked: 49" in prompt
    assert "$48,183" in prompt          # real bookings stay


def test_api_prompt_keeps_a_stabilized_listings_occupancy():
    steady = _prop(subject_performance=SubjectPerformance(
        annual_revenue=90_000, occupancy_pct=71.0, adr=300, nights_booked=240,
        nights_listed=340, months_with_data=12))
    prompt = N._build_prompt(steady, _rent(), [_comp()], CalculatorDefaults(), "", "")
    assert "adjusted occupancy: 71%" in prompt and "240 of 340 open nights" in prompt


# ══════════════════════════════════════════════════════════════════════════
# 7. Template copy states only what the data supports
# ══════════════════════════════════════════════════════════════════════════

def _copy(prop, comps, peak="", shoulder="") -> str:
    n = N.template_narratives(prop, comps, peak, shoulder)
    return " ".join([n.guest_profile, n.amenity_upside, n.peak_season_text,
                     n.shoulder_season_text]).lower()


@pytest.mark.parametrize("guests", [1, 2])
def test_a_sleeps_two_property_is_not_sold_to_families(guests):
    text = N.template_narratives(_prop(max_guests=guests), [_comp()]).guest_profile.lower()
    assert "famil" not in text and "multi-couple" not in text and "couples" in text


def test_a_big_house_is_described_by_its_capacity():
    text = N.template_narratives(_prop(max_guests=12), [_comp()]).guest_profile.lower()
    assert "12-guest" in text and "larger groups" in text


def test_copy_makes_none_of_the_removed_claims():
    comps = [_comp(0, occ=45), _comp(1, occ=80)]
    peak, shoulder = derive_season_labels(DIST)
    text = _copy(_prop(), comps, peak, shoulder)
    for claim in ("top of that range", "addressable", "top-quartile",
                  "holiday and event demand", "decides the year"):
        assert claim not in text, claim


# ══════════════════════════════════════════════════════════════════════════
# 8-11. Sanity gate
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("url", [
    "https://evilmuscache.com/x.jpg",
    "https://muscache.com.evil.com/x.jpg",
    "https://muscache.com@evil.com/x.jpg",
    "https://a0.muscache.com:pw@evil.com/x.jpg",
    "javascript://a0.muscache.com/%0aalert(1)",
    "//a0.muscache.com/x.jpg",
])
def test_hero_host_lookalikes_are_refused(url):
    assert sanity.check_subject_hero(url), url


@pytest.mark.parametrize("url", [
    "https://a0.muscache.com/im/pictures/x.jpg",
    "https://muscache.com/x.jpg",
    "https://ap.rdcpix.com/abc.jpg",
    "http://photos.zillowstatic.com/fp/x.jpg",
])
def test_real_trusted_hosts_and_their_subdomains_pass(url):
    assert sanity.check_subject_hero(url) == []


def _no_http(monkeypatch):
    async def ok(client, url):
        return url, True, 200
    monkeypatch.setattr(sanity, "_head_ok", ok)


@pytest.mark.parametrize("n", [5, 7])
def test_phase_a_needs_exactly_six_comps(n, monkeypatch):
    _no_http(monkeypatch)
    data = _report()
    data.comps = [_comp(i, name=f"Comp {i}") for i in range(n)]
    failures = _run(sanity.run_phase_a(data))
    assert any(f"{n} comps" in f and "exactly 6" in f for f in failures), failures


def test_phase_a_passes_six_comps(monkeypatch):
    _no_http(monkeypatch)
    assert [f for f in _run(sanity.run_phase_a(_report())) if "comps" in f] == []


def test_revenue_gate_does_not_multiply_adr_by_nights():
    """No measured room revenue means no check: adr x nights is the noise
    (-13.8% to +18.9%) this gate must not judge."""
    wild = _comp(annual_revenue=1_000_000, adr=100, nights_booked=100, room_revenue=None)
    assert sanity._check_revenue_sanity(wild) is None
    measured = _comp(annual_revenue=1_000_000, room_revenue=90_000)
    assert "room revenue" in sanity._check_revenue_sanity(measured)


def test_dead_code_is_gone():
    assert not hasattr(sanity, "run_sanity_checks")
    assert not hasattr(N, "_fallback_narratives")
    import generators.calculator as calc
    assert not hasattr(calc, "airbtics_to_seasonal")
    assert not hasattr(calc, "derive_seasonal_data")
    assert not hasattr(te, "_slugify")


def test_calculator_validator_checks_days_and_occupancy_ceiling():
    good = CalculatorDefaults()
    assert sanity.validate_calculator_defaults(good) == []
    assert sanity.validate_calculator_defaults(good.model_copy(update={"days_default": 90}))
    assert sanity.validate_calculator_defaults(good.model_copy(update={"days_default": 400}))
    over = sanity.validate_calculator_defaults(
        good.model_copy(update={"occ_max": 105, "occ_default": 101}))
    assert any("over 100%" in f for f in over)


def test_calculator_adr_minimum_never_hits_zero():
    cheap = [_comp(i, annual_revenue=2_000, nights_booked=200, adr=8) for i in range(6)]
    calc = derive_calculator_defaults(cheap, RentalizerData(revenue_potential=1, adr=8, occupancy_pct=50),
                                      _prop())
    assert calc.adr_min >= calc.adr_step and calc.adr_min <= calc.adr_default <= calc.adr_max
    assert sanity.validate_calculator_defaults(calc) == []


# ══════════════════════════════════════════════════════════════════════════
# 12. Methodology
# ══════════════════════════════════════════════════════════════════════════

def _sources(basis, missing=3) -> str:
    m = build_methodology(_prop(), [_comp()], market_months_missing=missing, seasonal_basis=basis)
    return " ".join(m.data_sources)


def test_interpolated_months_note_only_when_the_chart_is_the_market_curve():
    assert "Interpolated months" in _sources("market")
    for basis in ("comps", "subject", ""):
        assert "Interpolated months" not in _sources(basis), basis


def test_methodology_uses_one_distance_unit():
    m = build_methodology(
        _prop(), [_comp(distance_km=3.2)],
        comp_funnel={"candidates": 25, "selected": 6, "targeted": {"radius_miles": 10, "added": 4}})
    blob = " ".join(m.comp_criteria)
    assert "16 km" in blob and "miles" not in blob


# ══════════════════════════════════════════════════════════════════════════
# 14-16. Calculator rounding parity with the page's JavaScript
# ══════════════════════════════════════════════════════════════════════════

def test_half_nights_round_up_like_math_round():
    assert te.js_round(52.5) == 53 and te.js_round(0.5) == 1 and te.js_round(2.4999) == 2
    assert round(52.5) == 52          # the bug this replaces


def test_initial_values_use_half_up_rounding():
    """250 nights at 21% is 52.5 booked nights: Python's round() said 52, the
    slider script said 53, so the pre-JS HTML was a night short."""
    html = te.render_report(_report(days_min=100, days_default=250, days_max=365,
                                    occ_min=10, occ_default=21, occ_max=90,
                                    adr_min=100, adr_default=200, adr_max=400))
    assert 'id="occNights">53<' in html.replace(" ", "").replace("\n", "") or ">53<" in html
    assert f"{53 * 200:,}" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_round_matches_node_math_round_on_the_whole_slider_grid():
    grid = [(d, o) for d in range(100, 366, 5) for o in range(10, 96)]
    script = ("const g = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "console.log(JSON.stringify(g.map(([d,o]) => Math.round(d * (o / 100)))));")
    out = subprocess.run(["node", "-e", script], input=json.dumps(grid),
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [te.js_round(d * (o / 100)) for d, o in grid]


def test_days_default_is_the_real_open_nights_and_on_the_slider():
    """A browser snaps an off-grid value to min + k*step. With a 5-night step
    243 open nights became 245 in the calculator while the prose said 243, so
    the slider moves in single nights and the real figure is kept."""
    sp = SubjectPerformance(annual_revenue=80_000, occupancy_pct=70.0, adr=300,
                            nights_booked=170, nights_listed=243, months_with_data=12)
    calc = derive_calculator_defaults([_comp(i) for i in range(6)], _rent(), _prop(subject_performance=sp))
    assert calc.days_default == 243
    assert (calc.days_default - calc.days_min) % calc.days_step == 0
    assert calc.days_min <= calc.days_default <= calc.days_max


def test_currency_rounds_half_up_the_same_way_the_breakdown_does():
    assert te.format_currency(1234.5, "$") == "$1,235"      # int() said 1,234
    assert te.format_currency(1234.4, "$") == "$1,234"
    comp = _comp(annual_revenue=100_000.5, nights_booked=200)
    assert te.format_currency(comp.annual_revenue, "$") == f"${te.revenue_breakdown(comp)['total']:,}"


# ══════════════════════════════════════════════════════════════════════════
# 17. Template hardening
# ══════════════════════════════════════════════════════════════════════════

def test_chart_js_is_pinned_to_a_major_version():
    tpl = (ROOT / "templates" / "report.html.j2").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net/npm/chart.js@4" in tpl
    assert 'npm/chart.js"' not in tpl


@pytest.mark.parametrize("value,expected", [
    ("https://www.airbnb.com/rooms/1", "https://www.airbnb.com/rooms/1"),
    ("http://example.com/a?b=1", "http://example.com/a?b=1"),
    ("javascript:alert(1)", ""), ("JaVaScRiPt:alert(1)", ""),
    ("data:text/html;base64,AAA", ""), ("//evil.com/x", ""),
    ("https://", ""), ("", ""), (None, ""),
])
def test_safe_url_only_passes_http_urls(value, expected):
    assert te.safe_url(value) == expected


def test_a_javascript_listing_url_never_reaches_an_href():
    data = _report()
    data.property.listing_url = "javascript:alert(1)"
    data.property.airbnb_url = "javascript:alert(2)"
    data.comps[0].airbnb_url = "javascript:alert(3)"
    html = te.render_report(data)
    assert "javascript:" not in html.lower()


def test_unused_border_glow_css_is_gone():
    tpl = (ROOT / "templates" / "report.html.j2").read_text(encoding="utf-8")
    assert "borderGlow" not in tpl and "border-glow" not in tpl


# ══════════════════════════════════════════════════════════════════════════
# 18. check_setup: only a blank or rejected key opens the editor
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def world(tmp_path, monkeypatch):
    kit_dir = tmp_path / "str-secrets-connections"
    kit_dir.mkdir()
    (kit_dir / "CONNECTIONS.md").write_text("", encoding="utf-8")
    (kit_dir / "fan-out-env.sh").write_text("", encoding="utf-8")
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    results = {"AIRROI_API_KEY": "ok", "FIRECRAWL_API_KEY": "ok"}
    opened: list = []
    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs, "PROBES", {n: (lambda key, n=n: results[n]) for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    monkeypatch.setattr(cs, "other_copy", lambda name: ("", ""))
    monkeypatch.setattr(cs, "open_for_paste", lambda path: opened.append(path))
    branding = tmp_path / "branding.json"
    branding.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs, "BRANDING", branding)
    return kit_dir, results, opened


@pytest.mark.parametrize("status", ["no credit", "rate limited", "error 503", "unreachable"])
def test_a_working_key_is_never_sent_to_notepad(world, capsys, status):
    kit_dir, results, opened = world
    results["AIRROI_API_KEY"] = status
    before = (kit_dir / ".env").read_text(encoding="utf-8")
    assert cs.main([]) == 2
    assert opened == [], f"{status} says nothing against the key"
    assert (kit_dir / ".env").read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "paste the key" not in out and "Opening" not in out
    if status == "no credit":
        assert "Top up" in out and "no new key needed" in out
    else:
        assert "not a key problem" in out


@pytest.mark.parametrize("status", ["rejected", "blank"])
def test_a_rejected_or_blank_key_still_opens_the_editor(world, capsys, status):
    kit_dir, results, opened = world
    if status == "blank":
        (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=\n", encoding="utf-8")
    else:
        results["FIRECRAWL_API_KEY"] = "rejected"
    assert cs.main([]) == 2
    assert opened == [kit_dir / ".env"]
    assert "paste the key straight after the = sign" in capsys.readouterr().out


def test_mixed_failures_open_the_editor_only_for_the_key_that_needs_it(world, capsys):
    kit_dir, results, opened = world
    results["AIRROI_API_KEY"] = "no credit"
    results["FIRECRAWL_API_KEY"] = "rejected"
    assert cs.main([]) == 2
    out = capsys.readouterr().out
    assert opened == [kit_dir / ".env"]
    assert "FIRECRAWL_API_KEY: paste the key" in out and "AIRROI_API_KEY: the key is fine" in out


@pytest.mark.parametrize("code,expected", [
    (200, "ok"), (401, "rejected"), (403, "rejected"), (402, "no credit"),
    (429, "rate limited"), (503, "error 503"),
])
def test_probe_maps_every_status(monkeypatch, code, expected):
    monkeypatch.setattr(cs.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=code))
    assert cs._probe("https://x.example", {}) == expected


# ══════════════════════════════════════════════════════════════════════════
# 19. brand_from_website: the link is the site, not the page it was read from
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("site,origin", [
    ("https://example.com/about/team?utm=1#x", "https://example.com"),
    ("https://user:pw@example.com:8443/a", "https://example.com:8443"),
    ("http://www.example.com", "http://www.example.com"),
    ("https:///nohost", ""), ("https://", ""), ("ftp://example.com", ""),
    ("https://example.com:notaport/", ""),
])
def test_site_origin(site, origin):
    assert bw.site_origin(site) == origin


def test_main_writes_the_origin_not_the_page_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw, "CACHE", tmp_path / ".cache")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(bw, "read_brand", lambda site: {"brandName": ""})
    monkeypatch.setattr(bw, "fetch_logo", lambda url: None)
    assert bw.main(["https://www.acme-stays.com/about?ref=ad#team"]) == 0
    out = json.loads((tmp_path / "branding.json").read_text(encoding="utf-8"))
    assert out["website_url"] == "https://www.acme-stays.com"
    assert out["company_name"] == "acme-stays.com"


def test_an_address_with_no_host_exits_3_without_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    assert bw.main(["https:///"]) == 3
    assert "not a website address" in capsys.readouterr().out
    assert not (tmp_path / "branding.json").exists()


# ══════════════════════════════════════════════════════════════════════════
# 20. package.py: UTF-8 paths, and a missing tracked file is loud
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def package():
    import importlib.util
    spec = importlib.util.spec_from_file_location("package_under_test", ROOT / "scripts" / "package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_git_manifest_is_decoded_as_utf8_not_the_locale(package, monkeypatch):
    raw = "café/menu.md\0docs/\U0001F3E0 notes.md\0agent.py\0".encode("utf-8")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return SimpleNamespace(stdout=raw)

    monkeypatch.setattr(package.subprocess, "run", fake_run)
    rels = [p.relative_to(package.PROJECT_ROOT).as_posix() for p in package.tracked_files()]
    assert rels == ["café/menu.md", "docs/\U0001F3E0 notes.md", "agent.py"]
    assert not seen.get("text"), "text=True decodes with cp1252 on Windows"


def test_a_tracked_file_missing_on_disk_fails_loudly_and_leaves_no_zip(package, monkeypatch, tmp_path):
    monkeypatch.setattr(package, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(package, "tracked_files",
                        lambda: [package.PROJECT_ROOT / "agent.py", package.PROJECT_ROOT / "gone-file.py"])
    with pytest.raises(SystemExit) as e:
        package.build_zip()
    assert "gone-file.py" in str(e.value)
    assert not (tmp_path / "dist" / package.ZIP_NAME).exists()


# ══════════════════════════════════════════════════════════════════════════
# 21-22. setup.py never reads a key back; the launcher quotes its YAML
# ══════════════════════════════════════════════════════════════════════════

def test_setup_prompt_shows_set_not_a_key_prefix(monkeypatch, capsys):
    key = "ar_live_ABCDEFGHIJKLMNOP1234"
    monkeypatch.setattr("builtins.input", lambda _p="": "")
    assert setup_script.prompt("AirROI", True, "somewhere", "hint", key) == key
    out = capsys.readouterr().out
    assert "Current: set" in out
    for piece in (key[:6], key[-4:], "ABCDEF"):
        assert piece not in out


def test_launcher_description_survives_quotes_and_colons(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    tricky = 'Run comps: say "run comps" or \'comp this\' # not a comment'
    skill.write_text(f"---\nname: x\ndescription: {tricky}\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(il, "PROJECT_SKILL", skill)
    line = next(l for l in il.launcher_text().splitlines() if l.startswith("description:"))
    assert json.loads(line.split(":", 1)[1]) == tricky


def test_an_already_quoted_description_is_not_quoted_twice(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text('---\nname: x\ndescription: "Say \\"go\\": now"\n---\n', encoding="utf-8")
    monkeypatch.setattr(il, "PROJECT_SKILL", skill)
    line = next(l for l in il.launcher_text().splitlines() if l.startswith("description:"))
    assert json.loads(line.split(":", 1)[1]) == 'Say "go": now'


# ══════════════════════════════════════════════════════════════════════════
# 23. CI
# ══════════════════════════════════════════════════════════════════════════

def test_ci_hides_python_if_any_pattern_matches_and_does_not_pipe_into_head():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "compgen -G" in ci
    assert 'ls "$p"/python*' not in ci, "ls fails unless EVERY pattern matches"
    assert "--help | head" not in ci, "SIGPIPE fails the step under pipefail"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_ci_hide_loop_hides_a_folder_holding_only_one_of_the_patterns(tmp_path):
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    block = re.search(r"(          keep=\"\"\n.*?echo \"CLEAN_PATH=.*?\n)", ci, re.S).group(1)
    script = "\n".join(line[10:] for line in block.splitlines())
    only_py, only_uv, neither = (tmp_path / n for n in ("py", "uv", "none"))
    for d in (only_py, only_uv, neither):
        d.mkdir()
    (only_py / "python3.exe").write_text("", encoding="utf-8")
    (only_uv / "uv").write_text("", encoding="utf-8")
    (neither / "tool").write_text("", encoding="utf-8")
    env_file = tmp_path / "env"

    def to_posix(p: Path) -> str:
        # C:/x -> /c/x: a drive colon would split the colon-separated PATH.
        return re.sub(r"^([A-Za-z]):", lambda m: "/" + m.group(1).lower(), p.as_posix())

    run = subprocess.run(
        [shutil.which("bash"), "-c", script],
        env={**os.environ, "PATH": ":".join(map(to_posix, (only_py, only_uv, neither))),
             "GITHUB_ENV": to_posix(env_file)},
        capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    clean = env_file.read_text(encoding="utf-8").split("=", 1)[1].strip().split(":")
    # Git Bash on a Windows runner prepends its own dirs (/mingw64/bin,
    # /usr/bin, ~/bin) to PATH, so judge only the three dirs made here.
    ours = {to_posix(d) for d in (only_py, only_uv, neither)}
    assert [p for p in clean if p in ours] == [to_posix(neither)], clean



def test_a_non_utf8_kit_env_is_reported_not_a_traceback(world, monkeypatch, capsys):
    kit_dir, results, opened = world
    results["FIRECRAWL_API_KEY"] = "rejected"

    def boom(env, names):
        raise ValueError(f"{env} is not UTF-8 text; re-save it as UTF-8 and try again")

    monkeypatch.setattr(cs.kit, "add_blank_lines", boom)
    assert cs.main([]) == 2
    assert "not UTF-8 text" in capsys.readouterr().out
    assert opened == [kit_dir / ".env"]
