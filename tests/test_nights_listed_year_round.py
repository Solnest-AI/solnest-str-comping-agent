"""S2-009: Nights Listed started at 200 for a listing its host books all year.

Reported 2026-10-07 by a summit student who ran the agent on his own listing:
"bookable 365 days a year but only had 200 in the report". The slider starts
at the subject's own open nights from AirROI, which is 365 minus the nights
BLOCKED on its Airbnb calendar over the last 12 months. AirROI only sees
Airbnb, so VRBO and direct bookings that sync over as blocks, and owner holds,
all count as "not listed". The report did not say where 200 came from, and
there was no way to start at 365.

The fix keeps the measured default (a host who really blocks half the year
must not be shown a 365-night projection), says where it came from, and adds
--year-round so a host who lists all year starts at 365 with Airbnb's own
figure still shown beside it.

Why this student's listing showed exactly 200 is NOT verified: he sent no
link. The test reproduces the shape of his report, not his listing.
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from markupsafe import escape

import agent
from generators.calculator import apply_year_round, derive_calculator_defaults
from generators.narrative_brief import build_narrative_brief
from report.template_engine import nights_basis_text, render_report
from schema import (
    CalculatorDefaults,
    CompProperty,
    MethodologyData,
    Narratives,
    PropertyBasics,
    RentalizerData,
    ReportData,
    SubjectPerformance,
)
from validators.sanity import validate_calculator_defaults

ROOT = Path(__file__).resolve().parent.parent
GENERIC_NOTE = "Account for maintenance, cleaning, and personal use"


def _comps(nights_listed: int = 340) -> list[CompProperty]:
    return [
        CompProperty(
            name=f"Comp {i}", image_url="https://example.com/i.jpg", sleeps=8,
            bedrooms=3, bathrooms=2.0, rating=4.9, review_count=40,
            annual_revenue=90_000, occupancy_pct=60, adr=350,
            nights_booked=200, nights_listed=nights_listed,
            airbnb_url=f"https://www.airbnb.com/rooms/{i}",
        )
        for i in range(6)
    ]


def _prop(sp: SubjectPerformance | None) -> PropertyBasics:
    return PropertyBasics(
        address="1 Test St", short_address="1 Test St", market="Testville",
        bedrooms=3, bathrooms=2.0, max_guests=8, subject_performance=sp,
    )


def _rent() -> RentalizerData:
    return RentalizerData(revenue_potential=100_000, adr=350, occupancy_pct=60)


# The student's report: his own listing, a full year of history, 200 open
# nights on the Airbnb calendar.
OWN_LISTING = SubjectPerformance(
    annual_revenue=60_000, occupancy_pct=70.0, adr=400,
    nights_booked=140, nights_listed=200, months_with_data=12,
)


def _calc(sp: SubjectPerformance | None = OWN_LISTING) -> CalculatorDefaults:
    return derive_calculator_defaults(_comps(), _rent(), _prop(sp))


def _report(calc: CalculatorDefaults, sp: SubjectPerformance | None = OWN_LISTING) -> ReportData:
    return ReportData(
        property=_prop(sp), rentalizer=_rent(), comps=_comps(),
        calculator=calc, narratives=Narratives(), methodology=MethodologyData(),
    )


# ── Where the default came from is recorded ──

def test_own_listing_starts_at_its_airbnb_open_nights_and_records_that():
    calc = _calc()
    assert calc.days_default == 200, "a host who blocks nights still starts at their own number"
    assert calc.days_basis == "subject"


def test_prospective_property_starts_at_the_comp_median():
    calc = _calc(sp=None)
    assert calc.days_default == 340
    assert calc.days_basis == "comp_set"


def test_young_listing_below_the_comp_median_uses_the_comps():
    young = OWN_LISTING.model_copy(update={"months_with_data": 4, "nights_listed": 245})
    calc = _calc(young)
    assert calc.days_default == 340
    assert calc.days_basis == "comp_set"


def test_young_listing_above_the_comp_median_keeps_its_own_number():
    young = OWN_LISTING.model_copy(update={"months_with_data": 4, "nights_listed": 352})
    calc = _calc(young)
    assert calc.days_default == 352
    assert calc.days_basis == "subject"


# ── --year-round ──

def test_year_round_starts_at_365_and_changes_nothing_else():
    calc = _calc()
    yr = apply_year_round(calc)
    assert yr.days_default == 365
    assert yr.days_basis == "year_round"
    assert yr.days_min == calc.days_min and yr.days_max == 365
    for field in ("occ_default", "occ_basis", "adr_default", "adr_basis"):
        assert getattr(yr, field) == getattr(calc, field), field
    assert calc.days_default == 200, "apply_year_round must not mutate its input"
    assert validate_calculator_defaults(yr) == []


def test_cli_accepts_year_round():
    parser = agent._build_parser()
    assert parser.parse_args(["--input", "x", "--year-round"]).year_round is True
    assert parser.parse_args(["--input", "x"]).year_round is False


def test_host_overrides_apply_only_when_asked():
    calc = _calc()
    assert agent._apply_host_overrides(Namespace(year_round=False), calc) is calc
    assert agent._apply_host_overrides(Namespace(year_round=True), calc).days_default == 365


# ── The report says where the number came from ──

def test_report_labels_the_airbnb_figure_and_how_to_reach_365():
    calc = _calc()
    text = nights_basis_text(calc, OWN_LISTING)
    assert "200" in text and "Airbnb" in text and "AirROI" in text
    assert "VRBO" in text and "365" in text
    html = render_report(_report(calc))
    assert str(escape(text)) in html
    assert GENERIC_NOTE not in html


def test_year_round_report_shows_both_numbers():
    calc = apply_year_round(_calc())
    text = nights_basis_text(calc, OWN_LISTING)
    assert "365" in text and "per the host" in text
    assert "200" in text, "the AirROI figure stays visible, labelled"
    html = render_report(_report(calc))
    assert str(escape(text)) in html
    assert 'value="365"' in html


def test_year_round_without_history_does_not_invent_an_airbnb_figure():
    calc = apply_year_round(_calc(sp=None))
    text = nights_basis_text(calc, None)
    assert "per the host" in text and "AirROI" not in text


def test_comp_median_default_is_labelled():
    calc = _calc(sp=None)
    assert "six comparables" in nights_basis_text(calc, None)


def test_old_report_data_without_a_basis_keeps_the_old_note():
    """A report-data.json written before this fix has no days_basis.
    --render must still work on it and must not claim a basis it never had."""
    raw = json.loads(_report(_calc()).model_dump_json())
    del raw["calculator"]["days_basis"]
    old = ReportData.model_validate(raw)
    assert old.calculator.days_basis == ""
    assert GENERIC_NOTE in render_report(old)


def test_new_report_copy_has_no_em_dash():
    calcs = [_calc(), _calc(sp=None), apply_year_round(_calc()), CalculatorDefaults()]
    for calc in calcs:
        for sp in (OWN_LISTING, None):
            assert "—" not in nights_basis_text(calc, sp)


# ── Pass 2 (--render) applies it for free and keeps it ──

def _render_args(path: Path, year_round: bool) -> Namespace:
    return Namespace(render=str(path), narratives=None, email=None, year_round=year_round)


def _fake_gate(tmp_path: Path, seen: list):
    async def gate(report_data, slug):
        html = render_report(report_data)
        seen.append(html)
        out = tmp_path / "report.html"
        out.write_text(html, encoding="utf-8")
        return out
    return gate


def test_render_with_year_round_applies_and_persists(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(agent, "_render_and_gate", _fake_gate(tmp_path, seen))
    data_path = tmp_path / "x.report-data.json"
    data_path.write_text(_report(_calc()).model_dump_json(), encoding="utf-8")

    asyncio.run(agent._render_only(_render_args(data_path, year_round=True)))

    assert 'value="365"' in seen[0]
    saved = ReportData.model_validate_json(data_path.read_text(encoding="utf-8"))
    assert saved.calculator.days_default == 365
    assert saved.calculator.days_basis == "year_round"

    # A later copy-only re-render must not fall back to 200.
    asyncio.run(agent._render_only(_render_args(data_path, year_round=False)))
    assert 'value="365"' in seen[1]


def test_render_without_year_round_leaves_the_data_file_alone(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(agent, "_render_and_gate", _fake_gate(tmp_path, seen))
    data_path = tmp_path / "x.report-data.json"
    data_path.write_text(_report(_calc()).model_dump_json(), encoding="utf-8")
    before = data_path.read_bytes()

    asyncio.run(agent._render_only(_render_args(data_path, year_round=False)))

    assert data_path.read_bytes() == before
    assert 'value="200"' in seen[0]


# ── Claude is told to ask, only when it matters ──

def _season():
    return SimpleNamespace(basis="market", data=[50.0] * 12)


def test_run_log_asks_about_year_round_for_an_own_listing_below_365(capsys):
    agent._print_calculator(_prop(OWN_LISTING), _calc(), 25, _season())
    out = capsys.readouterr().out
    assert "--year-round" in out
    assert "200" in out


def test_run_log_does_not_ask_when_it_would_not_change_anything(capsys):
    agent._print_calculator(_prop(None), _calc(sp=None), 25, _season())
    agent._print_calculator(_prop(OWN_LISTING), apply_year_round(_calc()), 25, _season())
    assert "--year-round" not in capsys.readouterr().out


def test_brief_carries_the_basis_and_keeps_the_number_out_of_the_copy():
    calc = _calc()
    brief = build_narrative_brief(_prop(OWN_LISTING), _rent(), _comps(), calc)
    days = brief["calculator_defaults"]["days"]
    assert days["basis"] == "subject"
    assert "--year-round" in brief["calculator_defaults"]["_note"]


def test_skill_tells_claude_to_ask_and_how_to_apply_it():
    skill = (ROOT / ".claude/skills/str-comping-agent/SKILL.md").read_text(encoding="utf-8")
    assert "--year-round" in skill
    assert "year-round" in skill.lower()
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "--year-round" in claude_md
