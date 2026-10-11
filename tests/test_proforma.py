"""S2-027 (second half): a proforma under the revenue projection.

Asked for at the summit follow-up: "the PDF ... for real estate agents with a
proforma". The report stopped at gross revenue. The tool has no expense,
tax, insurance or HOA data for any property, so every expense and financing
figure is the student's own (Ryan, 2026-10-10: "Student's numbers", financing
optional). Nothing is invented:

- a blank line is never read as zero: NOI waits until every line has a number
  (0 counts) and says which lines are missing,
- each financing result shows only once its own inputs exist,
- the only figure pre-filled from data is the purchase price, from the price on
  the listing page, and the report says so.
"""

from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from markupsafe import escape

import agent
from generators import proforma as pf
from report.template_engine import render_report
from schema import (
    CalculatorDefaults, CompProperty, MethodologyData, Narratives,
    ProformaInputs, PropertyBasics, RentalizerData, ReportData,
)

ROOT = Path(__file__).resolve().parent.parent

FULL = dict(
    management_pct=20, platform_fee_pct=3, cleaning_per_stay=150, avg_stay_nights=4,
    utilities_monthly=400, insurance_annual=2400, property_tax_annual=4800,
    hoa_monthly=0, maintenance_pct=5, supplies_pct=2, other_annual=1200,
    purchase_price=400_000, down_payment_pct=25, interest_rate_pct=7, loan_years=30,
    startup_costs=30_000,
)
GROSS, OCCUPIED = 100_000, 200


# ── The arithmetic ──

def test_every_line_on_a_full_proforma():
    r = pf.compute(ProformaInputs(**FULL), GROSS, OCCUPIED)
    amounts = {line["key"]: line["amount"] for line in r["lines"]}
    assert amounts == {
        "management": 20_000, "platform": 3_000, "cleaning": 7_500,  # 50 stays x 150
        "utilities": 4_800, "insurance": 2_400, "property_tax": 4_800, "hoa": 0,
        "maintenance": 5_000, "supplies": 2_000, "other": 1_200,
    }
    assert r["total_expenses"] == 50_700
    assert r["noi"] == 49_300
    assert r["missing"] == []


def test_financing_on_a_full_proforma():
    r = pf.compute(ProformaInputs(**FULL), GROSS, OCCUPIED)
    # 300,000 at 7% over 30 years: 1,995.91 a month.
    assert r["debt_service"] == pytest.approx(23_950.92, abs=0.05)
    assert r["cash_flow"] == pytest.approx(49_300 - 23_950.92, abs=0.05)
    assert r["cash_invested"] == 130_000                     # 100,000 down + 30,000 startup
    assert r["cash_on_cash_pct"] == pytest.approx((49_300 - 23_950.92) / 130_000 * 100, abs=0.01)
    assert r["cap_rate_pct"] == pytest.approx(12.325, abs=0.001)


def test_a_blank_line_is_never_zero():
    r = pf.compute(ProformaInputs(**{**FULL, "property_tax_annual": None, "hoa_monthly": None}),
                   GROSS, OCCUPIED)
    assert r["total_expenses"] is None and r["noi"] is None
    assert r["missing"] == ["Property tax", "HOA / strata"]
    assert r["cap_rate_pct"] is None and r["cash_flow"] is None


def test_zero_counts_as_filled():
    r = pf.compute(ProformaInputs(**{**FULL, "other_annual": 0}), GROSS, OCCUPIED)
    assert r["noi"] == 50_500


def test_cleaning_needs_both_its_inputs():
    r = pf.compute(ProformaInputs(**{**FULL, "avg_stay_nights": None}), GROSS, OCCUPIED)
    assert {line["key"]: line["amount"] for line in r["lines"]}["cleaning"] is None
    assert "Cleaning" in r["missing"]


def test_each_financing_result_waits_for_its_own_inputs():
    only_price = {k: v for k, v in FULL.items()
                  if k not in ("down_payment_pct", "interest_rate_pct", "loan_years", "startup_costs")}
    r = pf.compute(ProformaInputs(**only_price), GROSS, OCCUPIED)
    assert r["cap_rate_pct"] == pytest.approx(12.325, abs=0.001)
    assert r["debt_service"] is None and r["cash_flow"] is None and r["cash_on_cash_pct"] is None


def test_zero_interest_loan():
    r = pf.compute(ProformaInputs(**{**FULL, "interest_rate_pct": 0}), GROSS, OCCUPIED)
    assert r["debt_service"] == pytest.approx(10_000)        # 300,000 / 30 years


def test_all_cash_purchase_has_no_debt_service():
    r = pf.compute(ProformaInputs(**{**FULL, "down_payment_pct": 100}), GROSS, OCCUPIED)
    assert r["debt_service"] == 0
    assert r["cash_invested"] == 430_000


# ── Loading and validating the student's numbers ──

def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("bad, needle", [
    ({"management_pct": 150}, "management_pct"),
    ({"insurance_annual": -5}, "insurance_annual"),
    ({"management_pct": "20%"}, "management_pct"),
    ({"loan_years": 0}, "loan_years"),
    ({"avg_stay_nights": 0}, "avg_stay_nights"),
    ({"interest_rate_pct": 45}, "interest_rate_pct"),
    ({"managment_pct": 20}, "managment_pct"),
    ({"management_pct": True}, "management_pct"),
])
def test_bad_numbers_name_the_field(tmp_path, bad, needle):
    path = _write(tmp_path / "p.json", bad)
    with pytest.raises(pf.ProformaError) as e:
        pf.load_proforma(defaults_path=tmp_path / "none.json", file_path=path)
    assert needle in str(e.value)


def test_per_property_numbers_override_the_students_defaults(tmp_path):
    defaults = _write(tmp_path / "proforma.json", {"management_pct": 20, "insurance_annual": 2000})
    this = _write(tmp_path / "x.proforma.json", {"insurance_annual": 3100, "hoa_monthly": 250})
    p = pf.load_proforma(defaults_path=defaults, file_path=this)
    assert (p.management_pct, p.insurance_annual, p.hoa_monthly) == (20, 3100, 250)
    assert p.property_tax_annual is None


def test_null_in_a_file_means_blank(tmp_path):
    defaults = _write(tmp_path / "proforma.json", {"management_pct": None})
    assert pf.load_proforma(defaults_path=defaults).management_pct is None


def test_the_listing_price_fills_a_missing_purchase_price_and_says_so(tmp_path):
    p = pf.load_proforma(defaults_path=tmp_path / "none.json", list_price=489_000)
    assert p.purchase_price == 489_000 and p.price_source == "listing"
    given = _write(tmp_path / "x.json", {"purchase_price": 450_000})
    p = pf.load_proforma(defaults_path=tmp_path / "none.json", file_path=given, list_price=489_000)
    assert p.purchase_price == 450_000 and p.price_source == ""


def test_a_rent_figure_is_not_a_purchase_price(tmp_path):
    p = pf.load_proforma(defaults_path=tmp_path / "none.json", list_price=2_400)
    assert p.purchase_price is None


def test_listing_price_reaches_the_subject():
    assert agent._list_price({"price": 489_000.0}) == 489_000.0
    assert agent._list_price({"price": 2_400.0}) is None
    assert agent._list_price({"price": None}) is None
    assert agent._list_price({}) is None


def test_the_students_defaults_never_ship():
    import importlib.util
    spec = importlib.util.spec_from_file_location("package", ROOT / "scripts" / "package.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    assert not package.is_safe(Path("proforma.json"))
    assert package.is_safe(Path("proforma.example.json"))
    assert "proforma.json" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    example = json.loads((ROOT / "proforma.example.json").read_text(encoding="utf-8"))
    assert set(example) == set(pf.FIELDS)
    assert all(v is None for v in example.values()), "the example must not suggest numbers"


# ── The report ──

def _report(proforma: ProformaInputs | None) -> ReportData:
    comps = [
        CompProperty(
            name=f"Comp {i}", image_url="https://example.com/i.jpg", sleeps=8,
            bedrooms=3, bathrooms=2.0, rating=4.9, review_count=40,
            annual_revenue=90_000, occupancy_pct=60, adr=350,
            nights_booked=200, nights_listed=340,
            airbnb_url=f"https://www.airbnb.com/rooms/{i}",
        )
        for i in range(6)
    ]
    return ReportData(
        property=PropertyBasics(
            address="1 Test St", short_address="1 Test St", market="Testville",
            bedrooms=3, bathrooms=2.0, max_guests=8,
        ),
        rentalizer=RentalizerData(revenue_potential=100_000, adr=350, occupancy_pct=60),
        comps=comps,
        calculator=CalculatorDefaults(occ_default=55, adr_default=500, days_default=364),
        narratives=Narratives(), methodology=MethodologyData(), proforma=proforma,
    )


def test_no_proforma_unless_asked_for():
    assert "Pro Forma" not in render_report(_report(None))


def test_full_proforma_renders_its_noi_before_any_javascript():
    html = render_report(_report(ProformaInputs(**FULL)))
    assert "Pro Forma" in html
    # 364 x 55% = 200 nights at 500 = 100,000 gross: the full proforma above.
    assert 'id="pfNoi">$49,300<' in html
    assert 'data-key="management_pct"' in html and 'value="20"' in html
    assert 'id="pfCapRate">12.3%<' in html


def test_incomplete_proforma_says_what_to_fill_in():
    html = render_report(_report(ProformaInputs(management_pct=20)))
    assert "Fill in the blank lines" in html
    assert 'id="pfNoi">' in html and 'id="pfNoi">$49,300<' not in html


def test_report_says_whose_numbers_these_are():
    html = render_report(_report(ProformaInputs(**FULL)))
    assert "not market data" in html


def test_listing_price_is_labelled():
    html = render_report(_report(ProformaInputs(purchase_price=489_000, price_source="listing")))
    assert "price shown on the listing page" in html


def test_proforma_copy_has_no_em_dash():
    html = render_report(_report(ProformaInputs(**FULL)))
    start = html.index("Pro Forma")
    end = html.index("<!-- /Pro Forma -->")
    assert "—" not in html[start:end]


def test_proforma_survives_the_report_data_round_trip():
    back = ReportData.model_validate_json(_report(ProformaInputs(**FULL)).model_dump_json())
    assert back.proforma.management_pct == 20


def test_old_report_data_without_a_proforma_still_loads():
    raw = json.loads(_report(None).model_dump_json())
    raw.pop("proforma")
    assert ReportData.model_validate(raw).proforma is None


# ── The CLI ──

def test_cli_flags():
    parser = agent._build_parser()
    assert parser.parse_args(["--input", "x", "--proforma"]).proforma == ""
    assert parser.parse_args(["--input", "x", "--proforma", "p.json"]).proforma == "p.json"
    assert parser.parse_args(["--input", "x"]).proforma is None
    assert parser.parse_args(["--render", "x", "--no-proforma"]).no_proforma is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--input", "x", "--proforma", "--no-proforma"])


def test_a_bad_proforma_stops_pass_one_before_anything_is_spent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf, "DEFAULTS_PATH", tmp_path / "none.json")
    bad = _write(tmp_path / "x.json", {"management_pct": 150})
    with pytest.raises(SystemExit) as e:
        agent._load_proforma_arg(Namespace(proforma=str(bad), no_proforma=False), list_price=None)
    assert e.value.code == 2
    assert "Nothing has been spent" in capsys.readouterr().out


def test_saved_defaults_turn_the_proforma_on_by_themselves(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "DEFAULTS_PATH", _write(tmp_path / "proforma.json", {"management_pct": 20}))
    p = agent._load_proforma_arg(Namespace(proforma=None, no_proforma=False), list_price=None)
    assert p.management_pct == 20
    assert agent._load_proforma_arg(Namespace(proforma=None, no_proforma=True), list_price=None) is None


def test_no_defaults_and_no_flag_means_no_proforma(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "DEFAULTS_PATH", tmp_path / "none.json")
    assert agent._load_proforma_arg(Namespace(proforma=None, no_proforma=False), list_price=None) is None


def _fake_gate(tmp_path: Path, seen: list):
    async def gate(report_data, slug):
        html = render_report(report_data)
        seen.append(html)
        out = tmp_path / "report.html"
        out.write_text(html, encoding="utf-8")
        return out
    return gate


def _args(path: Path, **kw) -> Namespace:
    base = dict(render=str(path), narratives=None, email=None, year_round=False,
                partner=None, no_partner=False, proforma=None, no_proforma=False)
    base.update(kw)
    return Namespace(**base)


def test_render_adds_the_proforma_for_free_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "DEFAULTS_PATH", tmp_path / "none.json")
    seen: list = []
    monkeypatch.setattr(agent, "_render_and_gate", _fake_gate(tmp_path, seen))
    data_path = tmp_path / "x.report-data.json"
    data_path.write_text(_report(None).model_dump_json(), encoding="utf-8")
    numbers = _write(tmp_path / "x.proforma.json", FULL)

    asyncio.run(agent._render_only(_args(data_path, proforma=str(numbers))))
    assert 'id="pfNoi">$49,300<' in seen[0]
    saved = ReportData.model_validate_json(data_path.read_text(encoding="utf-8"))
    assert saved.proforma.insurance_annual == 2400

    asyncio.run(agent._render_only(_args(data_path)))       # copy-only re-render keeps it
    assert 'id="pfNoi">$49,300<' in seen[1]

    asyncio.run(agent._render_only(_args(data_path, no_proforma=True)))
    assert "Pro Forma" not in seen[2]


def test_pass_one_tells_claude_which_numbers_to_ask_for(capsys):
    agent._print_proforma(_report(ProformaInputs(management_pct=20, purchase_price=489_000,
                                                 price_source="listing")))
    out = capsys.readouterr().out
    assert "ASK THE USER" in out and "Property tax" in out and "down_payment_pct" in out
    assert "pre-filled from the listing page: $489,000" in out


def test_pass_one_says_when_the_proforma_is_complete(capsys):
    agent._print_proforma(_report(ProformaInputs(**FULL)))
    out = capsys.readouterr().out
    assert "Complete: NOI $49,300" in out and "ASK THE USER" not in out


def test_pass_one_is_silent_without_a_proforma(capsys):
    agent._print_proforma(_report(None))
    assert capsys.readouterr().out == ""


def test_skill_and_manual_document_the_flow():
    skill = (ROOT / ".claude/skills/str-comping-agent/SKILL.md").read_text(encoding="utf-8")
    manual = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for text in (skill, manual):
        assert "--proforma" in text and "proforma.json" in text


def test_escaped_company_name_in_the_note(monkeypatch):
    import config
    monkeypatch.setitem(config.BRANDING, "company_name", "<b>Acme</b>")
    html = render_report(_report(ProformaInputs(**FULL)))
    assert "<b>Acme</b>" not in html and str(escape("<b>Acme</b>")) in html


def test_print_keeps_every_animated_section_visible():
    text = (ROOT / "templates" / "report.html.j2").read_text(encoding="utf-8")
    start = text.index("@media print")
    assert ".delay-4" in text[start:start + 800]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
