"""S2-027: co-brand a report with a real estate agent's details.

Asked at the 2026-10-01 summit follow-up: "the PDF you generate via Claude for
real estate agents with ... branding of the agent and yourself". The report
carried one brand (branding.json). A student works with several agents, so
each agent is a saved profile in partners/<agent>.json, picked per report
with --partner, and the student's own brand and colours stay in charge.

Agent profiles hold a real person's phone and email: they live in the
student's own folder only, never in the repo or the distribution zip.
"""

from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from markupsafe import escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import add_partner as ap  # noqa: E402
import agent  # noqa: E402
import partners  # noqa: E402
from report.template_engine import render_report  # noqa: E402
from schema import (  # noqa: E402
    CalculatorDefaults, CompProperty, MethodologyData, Narratives,
    PartnerBrand, PropertyBasics, RentalizerData, ReportData,
)

JANE = {
    "name": "Jane Doe",
    "brokerage": "Summit Realty Group",
    "phone": "+1 (865) 555-0100",
    "email": "jane@summitrealty.example",
    "website_url": "https://summitrealty.example",
    "logo_url": "https://summitrealty.example/logo.png",
    "logo_background": "",
}


def _report(partner: PartnerBrand | None = None) -> ReportData:
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
        comps=comps, calculator=CalculatorDefaults(), narratives=Narratives(),
        methodology=MethodologyData(), partner=partner,
    )


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    d = tmp_path / "partners"
    monkeypatch.setattr(partners, "PARTNERS_DIR", d)
    return d


def _save(pdir: Path, data: dict, slug: str = "jane-doe") -> Path:
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{slug}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── Profiles: load, validate, find ──

def test_a_saved_profile_loads_by_agent_name(pdir):
    _save(pdir, JANE)
    for ref in ("jane-doe", "Jane Doe", "jane doe"):
        assert partners.load_partner(ref).name == "Jane Doe"


def test_a_profile_loads_by_path(pdir, tmp_path):
    path = _save(tmp_path / "elsewhere", JANE, "agent")
    assert partners.load_partner(str(path)).brokerage == "Summit Realty Group"


def test_a_name_alone_is_a_complete_profile(pdir):
    _save(pdir, {"name": "Jane Doe"})
    p = partners.load_partner("jane-doe")
    assert p.name == "Jane Doe" and p.logo_url == "" and p.brokerage == ""


@pytest.mark.parametrize("bad, needle", [
    ({}, "name"),
    ({"name": "  "}, "name"),
    ({"name": "Jane", "logo_url": "javascript:alert(1)"}, "logo_url"),
    ({"name": "Jane", "website_url": "ftp://x"}, "website_url"),
    ({"name": "Jane", "logo_background": "white"}, "logo_background"),
    ({"name": "Jane", "email": "not an email"}, "email"),
    ({"name": "Jane", "phone": "call me <b>"}, "phone"),
    ({"name": "Jane", "brokrage": "typo"}, "brokrage"),
])
def test_a_broken_profile_names_the_field(pdir, bad, needle):
    _save(pdir, bad)
    with pytest.raises(partners.PartnerError) as e:
        partners.load_partner("jane-doe")
    assert needle in str(e.value)


def test_an_unknown_agent_lists_the_saved_ones(pdir):
    _save(pdir, JANE)
    with pytest.raises(partners.PartnerError) as e:
        partners.load_partner("bob")
    assert "jane-doe" in str(e.value)
    assert "add_partner.py" in str(e.value)


def test_profiles_never_ship():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "partners/" in gitignore
    import importlib.util
    spec = importlib.util.spec_from_file_location("package", ROOT / "scripts" / "package.py")
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    assert not package.is_safe(Path("partners/jane-doe.json"))
    assert package.is_safe(Path("partners.py")), "the loader itself must ship"
    assert package.is_safe(Path("scripts/add_partner.py"))


# ── scripts/add_partner.py ──

def test_add_partner_writes_a_profile_from_what_the_student_said(pdir, capsys):
    assert ap.main(["--name", "Jane Doe", "--brokerage", "Summit Realty Group",
                    "--phone", "+1 (865) 555-0100", "--email", "jane@summitrealty.example"]) == 0
    saved = json.loads((pdir / "jane-doe.json").read_text(encoding="utf-8"))
    assert saved["name"] == "Jane Doe" and saved["brokerage"] == "Summit Realty Group"
    out = capsys.readouterr().out
    assert "--partner jane-doe" in out


def test_add_partner_finds_the_logo_on_the_agents_site(pdir, tmp_path, monkeypatch):
    monkeypatch.setattr(ap.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(ap.bw, "read_brand",
                        lambda site: {"logo": "/logo.png", "colorScheme": "light"})
    fetched = []
    monkeypatch.setattr(ap.bw, "fetch_logo",
                        lambda url, stem="brand_logo": fetched.append((url, stem)) or tmp_path / "l.png")
    monkeypatch.setattr(ap.bw, "logo_is_light", lambda path: False)
    assert ap.main(["--name", "Jane Doe", "--website", "summitrealty.example/team/jane"]) == 0
    saved = json.loads((pdir / "jane-doe.json").read_text(encoding="utf-8"))
    assert saved["website_url"] == "https://summitrealty.example"
    assert saved["logo_url"] == "https://summitrealty.example/logo.png"
    # Never overwrite the student's own saved logo (.cache/brand_logo.*).
    assert fetched == [("https://summitrealty.example/logo.png", "partner_logo")]


def test_add_partner_drops_a_logo_that_does_not_load(pdir, monkeypatch, capsys):
    monkeypatch.setattr(ap.bw, "fetch_logo", lambda url, stem="brand_logo": None)
    assert ap.main(["--name", "Jane Doe", "--logo", "https://x.example/missing.png"]) == 0
    saved = json.loads((pdir / "jane-doe.json").read_text(encoding="utf-8"))
    assert saved["logo_url"] == ""
    assert "did not load" in capsys.readouterr().out


def test_add_partner_updates_without_losing_earlier_answers(pdir):
    _save(pdir, JANE)
    assert ap.main(["--name", "Jane Doe", "--phone", "+1 865 555 0199"]) == 0
    saved = json.loads((pdir / "jane-doe.json").read_text(encoding="utf-8"))
    assert saved["phone"] == "+1 865 555 0199"
    assert saved["brokerage"] == "Summit Realty Group"


def test_add_partner_refuses_a_bad_email_and_writes_nothing(pdir, capsys):
    assert ap.main(["--name", "Jane Doe", "--email", "jane at realty"]) == 3
    assert not (pdir / "jane-doe.json").exists()
    assert "email" in capsys.readouterr().out


def test_add_partner_lists_saved_agents(pdir, capsys):
    _save(pdir, JANE)
    assert ap.main(["--list"]) == 0
    assert "jane-doe" in capsys.readouterr().out


def test_fetch_logo_keeps_the_students_own_logo(tmp_path, monkeypatch):
    import brand_from_website as bw
    import httpx

    class R:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = b"png"

    monkeypatch.setattr(bw, "CACHE", tmp_path)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    (tmp_path / "brand_logo.png").write_bytes(b"student")
    path = bw.fetch_logo("https://x.example/l.png", stem="partner_logo")
    assert path.name == "partner_logo.png"
    assert (tmp_path / "brand_logo.png").read_bytes() == b"student"


# ── The report ──

def test_report_names_the_agent_in_header_and_footer():
    html = render_report(_report(PartnerBrand(**JANE)))
    assert "Prepared in partnership with Jane Doe, Summit Realty Group" in html
    assert html.count("Jane Doe") >= 2, "header and footer"
    assert "+1 (865) 555-0100" in html and "jane@summitrealty.example" in html
    assert 'src="https://summitrealty.example/logo.png"' in html
    assert "summitrealty.example" in html


def test_report_without_a_partner_is_unchanged():
    html = render_report(_report(None))
    assert "in partnership with" not in html.lower()


def test_partner_text_is_escaped_and_bad_links_never_render():
    p = PartnerBrand.model_construct(
        name="<script>x</script>", brokerage="", phone="", email="",
        website_url="javascript:alert(1)", logo_url="javascript:alert(2)",
        logo_background="",
    )
    html = render_report(_report(p))
    assert "<script>x</script>" not in html
    assert str(escape("<script>x</script>")) in html
    assert "javascript:alert" not in html


def test_partner_survives_the_report_data_round_trip():
    back = ReportData.model_validate_json(_report(PartnerBrand(**JANE)).model_dump_json())
    assert back.partner.name == "Jane Doe"


def test_old_report_data_without_a_partner_still_loads():
    raw = json.loads(_report(None).model_dump_json())
    raw.pop("partner")
    assert ReportData.model_validate(raw).partner is None


def test_new_report_copy_has_no_em_dash():
    html = render_report(_report(PartnerBrand(**JANE)))
    start = html.index("Prepared in partnership")
    assert "—" not in html[start:start + 400]


# ── The CLI, both passes ──

def test_cli_flags():
    parser = agent._build_parser()
    assert parser.parse_args(["--input", "x", "--partner", "jane-doe"]).partner == "jane-doe"
    assert parser.parse_args(["--render", "x", "--no-partner"]).no_partner is True
    with pytest.raises(SystemExit):
        parser.parse_args(["--input", "x", "--partner", "a", "--no-partner"])


def test_an_unknown_partner_stops_pass_one_before_anything_is_spent(pdir, capsys):
    with pytest.raises(SystemExit) as e:
        agent._resolve_partner(Namespace(partner="bob"))
    assert e.value.code == 2
    assert "Nothing has been spent" in capsys.readouterr().out


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
                partner=None, no_partner=False)
    base.update(kw)
    return Namespace(**base)


def test_render_adds_the_partner_for_free_and_keeps_it(pdir, monkeypatch, tmp_path):
    _save(pdir, JANE)
    seen: list = []
    monkeypatch.setattr(agent, "_render_and_gate", _fake_gate(tmp_path, seen))
    data_path = tmp_path / "x.report-data.json"
    data_path.write_text(_report(None).model_dump_json(), encoding="utf-8")

    asyncio.run(agent._render_only(_args(data_path, partner="jane-doe")))
    assert "Prepared in partnership with Jane Doe" in seen[0]
    saved = ReportData.model_validate_json(data_path.read_text(encoding="utf-8"))
    assert saved.partner.name == "Jane Doe"

    asyncio.run(agent._render_only(_args(data_path)))          # copy-only re-render
    assert "Prepared in partnership with Jane Doe" in seen[1]

    asyncio.run(agent._render_only(_args(data_path, no_partner=True)))
    assert "in partnership with" not in seen[2].lower()
    assert ReportData.model_validate_json(data_path.read_text(encoding="utf-8")).partner is None


def test_skill_and_manual_document_the_flow():
    skill = (ROOT / ".claude/skills/str-comping-agent/SKILL.md").read_text(encoding="utf-8")
    manual = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for text in (skill, manual):
        assert "--partner" in text and "add_partner.py" in text
