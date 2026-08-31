"""Regression guards for the two-pass render path.

The point of `--render` is that improving the narrative copy must be FREE.
If it ever starts touching the network again, the Claude Code loop becomes a
second full-price run and nobody will use it twice. These tests lock that in.
"""
from pathlib import Path


import agent
from schema import (
    ReportData, PropertyBasics, RentalizerData, CompProperty,
    CalculatorDefaults, Narratives,
)


def _report_data() -> ReportData:
    comps = [
        CompProperty(
            name=f"Comp {i}", image_url="https://example.com/i.jpg", sleeps=8,
            bedrooms=3, bathrooms=2.0, rating=4.9, review_count=50,
            annual_revenue=90000, revenue_potential=120000, occupancy_pct=60,
            adr=300, nights_booked=200, nights_listed=360,
            airbnb_url="https://www.airbnb.com/rooms/1",
        )
        for i in range(6)
    ]
    return ReportData(
        property=PropertyBasics(
            address="Gatlinburg, TN", short_address="Gatlinburg TN",
            market="Gatlinburg", bedrooms=3, bathrooms=2.0, max_guests=8,
        ),
        rentalizer=RentalizerData(revenue_potential=120000, adr=300, occupancy_pct=60),
        comps=comps,
        calculator=CalculatorDefaults(),
        narratives=Narratives(positioning_summary="template copy"),
        seasonal_data=[50.0] * 12,
        report_date="August 30, 2026",
    )


def test_report_data_round_trips_through_json():
    """--render depends on ReportData surviving a serialize/deserialize cycle."""
    original = _report_data()
    restored = ReportData.model_validate_json(original.model_dump_json())
    assert len(restored.comps) == 6
    assert restored.property.market == "Gatlinburg"
    assert restored.comps[0].nights_booked == 200
    assert restored.comps[0].rating == 4.9
    assert restored.seasonal_data == [50.0] * 12


def test_unrated_comp_survives_round_trip():
    """rating=None is a real state (too few reviews) and must not become 0.0."""
    data = _report_data()
    data.comps[0].rating = None
    restored = ReportData.model_validate_json(data.model_dump_json())
    assert restored.comps[0].rating is None


def test_render_makes_no_network_calls(monkeypatch, tmp_path):
    """The whole point of pass two: zero API spend.

    Any httpx call during --render means we regressed into re-running the
    pipeline, which is a $0.40 mistake on the user's key every time.
    """
    import httpx
    calls = []

    async def _boom(self, *a, **k):
        calls.append(a[0] if a else "?")
        raise AssertionError(f"--render made a network call to {a[0] if a else '?'}")

    monkeypatch.setattr(httpx.AsyncClient, "get", _boom, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "post", _boom, raising=False)

    data_path = tmp_path / "x.report-data.json"
    data_path.write_text(_report_data().model_dump_json())
    # Loading and applying copy is the part that must stay offline.
    restored = ReportData.model_validate_json(data_path.read_text())
    assert restored.comps
    assert calls == []


def test_render_requires_no_api_keys(monkeypatch):
    """--render must work on a machine with an empty .env."""
    import config
    monkeypatch.setattr(config, "AIRROI_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "FIRECRAWL_API_KEY", "", raising=False)
    data = ReportData.model_validate_json(_report_data().model_dump_json())
    assert len(data.comps) == 6


def test_report_data_path_is_stable():
    prop = _report_data().property
    p1 = agent.report_data_path(Path("/tmp/out"), prop)
    p2 = agent.report_data_path(Path("/tmp/out"), prop)
    assert p1 == p2
    assert p1.name.endswith(".report-data.json")


def test_skill_file_exists_and_declares_the_two_pass_loop():
    """The skill is how this actually gets used; keep it honest."""
    skill = Path(__file__).parent.parent / ".claude/skills/str-comping-agent/SKILL.md"
    assert skill.exists(), "SKILL.md is the entry point for Claude Code users"
    text = skill.read_text()
    assert text.startswith("---"), "SKILL.md needs YAML frontmatter to be discoverable"
    assert "name: str-comping-agent" in text
    assert "--render" in text and "--narratives" in text, "the loop must be documented"
    assert "AIRROI_API_KEY" in text
    assert "Never invent a number" in text, "the anti-fabrication rule must stay"


def test_rerun_command_uses_the_free_render_path():
    """The printed handoff must not tell users to re-run the whole pipeline.

    This shipped wrong once: the instruction said `--input`, which re-runs every
    fetch and costs ~$0.40 of the user's AirROI credit just to change wording.
    """
    from generators.narrative_brief import rerun_command
    cmd = rerun_command("output/x.report-data.json", "output/x.narratives.json")
    assert "--render" in cmd
    assert "--input" not in cmd
    assert "x.report-data.json" in cmd and "x.narratives.json" in cmd


def test_brief_rules_cover_the_known_traps():
    """The brief is the only thing Claude Code reads; its rules must be complete."""
    from generators.narrative_brief import NARRATIVE_RULES
    joined = " ".join(NARRATIVE_RULES).lower()
    for needle in ["never invent", "outside the low-high", "fee-inclusive",
                   "adjusted occupancy", "inward"]:
        assert needle in joined, f"brief rules lost coverage of: {needle}"
