"""Narrative tests — all three paths, on the live fixture corpus.

The published tool needs no Anthropic key: users run it inside Claude Code and
hand the copy back with `--narratives`. So the paths are, in priority order:

  1. `--narratives <file>`   copy written by Claude Code
  2. `ANTHROPIC_API_KEY`     the API, for headless platform runs
  3. template narratives     data-driven copy + a written brief

Every keyless test forces `config.ANTHROPIC_API_KEY = ""` rather than trusting
the ambient environment. A test that silently took the API path because the
developer had a key exported would assert nothing about what users get.

Comps come from `map_batch_for_scorer -> rank_comps -> to_comp_property` on the
150 captured AirROI listings, not from hand-written dicts: the numbers the copy
quotes have to be checkable against numbers a real market produced.

Run: pytest tests/test_narratives.py -v
"""

import asyncio
import json
import re
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from _fixtures import MARKETS, market_listings

import config
from adapters.airroi_to_comp import (
    map_batch_for_scorer,
    subject_for_scorer,
    to_comp_property,
)
from comp_scorer import rank_comps
from generators import narratives as N
from generators.calculator import derive_calculator_defaults, derive_season_labels
from generators.narrative_brief import (
    NARRATIVE_FIELDS,
    NARRATIVE_INPUT_SCHEMA,
    NARRATIVE_LIST_FIELDS,
    build_narrative_brief,
    months_from_label,
    narrative_brief_path,
    occupancy_stats,
)
from schema import CompProperty, Narratives, PropertyBasics, RentalizerData


# ── Building a real subject + comp set out of the fixtures ───────────────

def _median_int(vals):
    return int(round(statistics.median(vals))) if vals else 0


def _market_case(market: str):
    """(prop, rentalizer, comps, calculator) for one captured market."""
    mapped = map_batch_for_scorer(market_listings(market))
    beds = _median_int([m["bedrooms"] for m in mapped if m.get("bedrooms") is not None])
    guests = _median_int([m["sleeps"] for m in mapped if m.get("sleeps") is not None])
    lats = [m["latitude"] for m in mapped if m.get("latitude") is not None]
    lons = [m["longitude"] for m in mapped if m.get("longitude") is not None]
    adr = statistics.median([m["adr"] for m in mapped if m.get("adr")])

    prop = PropertyBasics(
        address=f"1 Test Way, {market.title()}",
        short_address=f"1 Test Way {market.title()}",
        market=market.title(),
        bedrooms=beds,
        bathrooms=2.0,
        max_guests=guests,
        property_type="Vacation Home",
        amenities=["Hot tub", "Wifi"],
        latitude=statistics.median(lats) if lats else None,
        longitude=statistics.median(lons) if lons else None,
    )

    result = rank_comps(subject_for_scorer(prop, {"average_daily_rate": adr}),
                        mapped, top_n=6)
    comps = [to_comp_property(c) for c in result["selected"]]
    assert comps, f"{market}: fixture pool produced no comps"

    occs = [c.occupancy_pct for c in comps]
    rentalizer = RentalizerData(
        revenue_potential=statistics.median([c.annual_revenue for c in comps]),
        adr=adr,
        occupancy_pct=statistics.median(occs),
    )
    calculator = derive_calculator_defaults(comps, rentalizer, prop)
    return prop, rentalizer, comps, calculator


@pytest.fixture(scope="module")
def case():
    """One representative market, built once. Destin is the market whose
    report shipped a ski narrative before the season fix."""
    return _market_case("destin")


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    """Keyless by default. Tests that want the API path opt back in."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")


# A well-formed narrative object, used as the `--narratives` payload.
VALID_PAYLOAD = {
    "positioning_summary": "Competes at the top of its bedroom tier locally.",
    "guest_profile": "Multi-couple and extended-family groups.",
    "amenity_upside": "Off-peak conversion turns on amenity depth, not price.",
    "config_description": "Open living and dining flow with guest-ready utility.",
    "guests_description": "Capacity matches local bylaws and the comp set cap.",
    "peak_season_text": "Peak demand concentrates in the market's busiest window.",
    "shoulder_season_text": "Shoulder months are decided by occupancy, not rate.",
    "amenity_badges": [
        {"emoji": "✨", "text": "Amenity Differentiation"},
        {"emoji": "🛏️", "text": "Sleeping Capacity Fit"},
        {"emoji": "📸", "text": "Listing Presentation"},
        {"emoji": "📈", "text": "Off-Season Conversion"},
    ],
    "positioning_cards": [
        {"emoji": "📍", "title": "Location Premium", "text": "Proximity drives it."},
        {"emoji": "⭐", "title": "Statement Quality", "text": "Finish level shows."},
        {"emoji": "💰", "title": "Revenue Potential", "text": "Upper-band pricing."},
    ],
}


def _write(tmp_path: Path, obj, name: str = "n.json") -> Path:
    p = tmp_path / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return p


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
# One schema, three consumers
# ══════════════════════════════════════════════════════════════════════════

def test_tool_call_and_file_loader_share_one_schema():
    """The forced tool call, the brief and the loader must not drift apart."""
    assert N.NARRATIVE_TOOL["input_schema"] is NARRATIVE_INPUT_SCHEMA
    assert NARRATIVE_INPUT_SCHEMA["required"] == NARRATIVE_FIELDS
    # The loader is stricter on purpose: a hand-written file has no retry loop.
    assert set(N._FILE_REQUIRED) == set(NARRATIVE_FIELDS) | set(NARRATIVE_LIST_FIELDS)


def test_brief_embeds_the_schema_the_loader_enforces(case):
    prop, rentalizer, comps, calculator = case
    brief = build_narrative_brief(prop, rentalizer, comps, calculator)
    assert brief["output_schema"] == NARRATIVE_INPUT_SCHEMA
    for field in NARRATIVE_FIELDS + NARRATIVE_LIST_FIELDS:
        assert field in brief["output_example"], field
        assert field in brief["field_guide"], field


# ══════════════════════════════════════════════════════════════════════════
# Path 3 — template narratives (the keyless default)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("market", MARKETS)
def test_template_copy_quotes_only_real_comp_occupancy(market):
    """Every percentage in the copy must exist in the comp set it describes.

    The bug this guards: a fallback that asserted "75-85%" occupancy in a
    market where no comp reached it.
    """
    prop, rentalizer, comps, calculator = _market_case(market)
    stats = occupancy_stats(comps)
    assert stats, f"{market}: comps carry no occupancy"

    n = N.template_narratives(prop, comps)
    blob = " ".join([
        n.positioning_summary, n.guest_profile, n.amenity_upside,
        n.peak_season_text, n.shoulder_season_text,
        n.config_description, n.guests_description,
    ] + [c.text for c in n.positioning_cards])

    # Occupancy percentages are the only "NN%" the copy emits. The spread
    # sentence prints a difference in points, so allow that value too.
    spread = round(stats["high"] - stats["low"])
    allowed = {
        round(stats["low"]), round(stats["high"]), round(stats["median"]), spread,
    }
    for pct in re.findall(r"(\d+)%", blob):
        assert int(pct) in allowed, (
            f"{market}: copy quotes {pct}% which is not a measured comp figure "
            f"(comp range {stats['low']:.0f}-{stats['high']:.0f}%)"
        )


@pytest.mark.parametrize("market", MARKETS)
def test_template_copy_names_no_months_without_a_season_label(market):
    """With no season label resolved, the copy must not name a month.

    This is the Destin-ski failure in its general form: prose that asserts a
    calendar the data never supplied.
    """
    prop, _, comps, _ = _market_case(market)
    n = N.template_narratives(prop, comps, "", "")
    blob = f"{n.peak_season_text} {n.shoulder_season_text} {n.amenity_upside}"
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    assert not [m for m in months if m in blob], (market, blob)


def test_template_copy_uses_the_season_label_when_it_has_one(case):
    prop, _, comps, _ = case
    # A real 12-month distribution peaking in summer.
    dist = [0.03, 0.04, 0.06, 0.08, 0.10, 0.13, 0.15, 0.14, 0.09, 0.07, 0.06, 0.05]
    peak, shoulder = derive_season_labels(dist)
    assert peak, "fixture distribution should resolve a peak"

    months = months_from_label(peak)          # e.g. "May-Aug"
    n = N.template_narratives(prop, comps, peak, shoulder)
    assert months and months in n.peak_season_text
    assert months_from_label(shoulder) in n.shoulder_season_text
    assert n.peak_season_label == peak
    assert n.shoulder_season_label == shoulder


def test_keyless_run_returns_template_copy_and_writes_the_brief(case, tmp_path, capsys):
    prop, rentalizer, comps, calculator = case
    out = _run(N.generate_narratives(
        prop, rentalizer, comps, calculator,
        output_dir=tmp_path,
        input_ref="https://www.airbnb.com/rooms/12345",
    ))
    assert isinstance(out, Narratives)
    assert out.positioning_summary

    brief_path = narrative_brief_path(tmp_path, prop)
    assert brief_path.exists(), f"expected a brief at {brief_path}"
    assert brief_path.name.endswith(".narrative-brief.json")

    printed = capsys.readouterr().out
    assert "NARRATIVE HANDOFF" in printed
    assert "--narratives" in printed
    assert str(brief_path.resolve()) in printed
    # The printed command must be the FREE --render path, not a second full
    # run. It previously echoed the original --input URL, and following that
    # instruction cost another ~$0.40 of AirROI credit just to reword copy.
    assert "--render" in printed
    assert "--input" not in printed
    assert ".report-data.json" in printed


def test_keyless_run_writes_no_brief_without_an_output_dir(case, tmp_path):
    prop, rentalizer, comps, calculator = case
    _run(N.generate_narratives(prop, rentalizer, comps, calculator))
    assert list(tmp_path.iterdir()) == []


def test_brief_carries_the_comp_table_and_calculator(case, tmp_path):
    prop, rentalizer, comps, calculator = case
    _run(N.generate_narratives(
        prop, rentalizer, comps, calculator, output_dir=tmp_path,
    ))
    brief = json.loads(narrative_brief_path(tmp_path, prop).read_text())

    assert brief["comp_set"]["count"] == len(comps)
    rows = brief["comp_set"]["comps"]
    assert len(rows) == len(comps)
    for row, comp in zip(rows, comps):
        assert row["name"] == comp.name
        assert row["bedrooms"] == comp.bedrooms
        assert row["sleeps"] == comp.sleeps
        # The rate actually paid, not ttm_avg_rate (see test_paid_rate_everywhere).
        assert "adr" not in row
        assert row["nightly_rate"] == pytest.approx(comp.nightly_rate, abs=0.51)
        assert row["occupancy_pct"] == pytest.approx(comp.occupancy_pct, abs=0.06)
        assert row["annual_revenue"] == pytest.approx(comp.annual_revenue, abs=0.51)
        # rating None must survive as null: it means "too few reviews", not zero.
        assert (row["rating"] is None) == (comp.rating is None)

    assert brief["subject"]["market"] == prop.market
    assert brief["calculator_defaults"]["occupancy_pct"]["default"] == calculator.occ_default
    assert brief["calculator_defaults"]["adr"]["max"] == calculator.adr_max
    assert brief["rules"], "the brief must ship the discipline rules"
    assert "--narratives" in brief["rerun_command"]


def test_brief_carries_the_derived_season_labels(case, tmp_path):
    prop, rentalizer, comps, calculator = case
    dist = [0.03, 0.04, 0.06, 0.08, 0.10, 0.13, 0.15, 0.14, 0.09, 0.07, 0.06, 0.05]
    peak, shoulder = derive_season_labels(dist)

    _run(N.generate_narratives(
        prop, rentalizer, comps, calculator,
        peak_season_label=peak, shoulder_season_label=shoulder,
        monthly_distribution=dist, output_dir=tmp_path,
    ))
    season = json.loads(narrative_brief_path(tmp_path, prop).read_text())["market_seasonality"]
    assert season["peak_season_label"] == peak
    assert season["shoulder_season_label"] == shoulder
    assert season["peak_months"]
    assert season["peak_share_of_annual_revenue_pct"] is not None
    assert season["monthly_revenue_distribution"] == dist


def test_brief_is_valid_json_for_every_market(tmp_path):
    """A brief that cannot be re-read is a handoff that does not work."""
    for market in MARKETS:
        prop, rentalizer, comps, calculator = _market_case(market)
        d = tmp_path / market
        _run(N.generate_narratives(
            prop, rentalizer, comps, calculator, output_dir=d,
        ))
        loaded = json.loads(narrative_brief_path(d, prop).read_text(encoding="utf-8"))
        assert loaded["schema_version"] == 1
        assert loaded["comp_set"]["comps"], market


def test_unwritable_brief_does_not_lose_the_report(case, tmp_path, monkeypatch, capsys):
    """Blast radius: a failed brief write costs a convenience, not the run."""
    prop, rentalizer, comps, calculator = case

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(N, "emit_narrative_brief", boom)
    out = _run(N.generate_narratives(
        prop, rentalizer, comps, calculator, output_dir=tmp_path,
    ))
    assert out.positioning_summary
    assert "Could not write the narrative brief" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════════
# Path 1 — narratives handed back from Claude Code
# ══════════════════════════════════════════════════════════════════════════

def test_valid_file_is_loaded_verbatim(tmp_path):
    n = N.load_narratives_from_file(_write(tmp_path, VALID_PAYLOAD))
    assert n.positioning_summary == VALID_PAYLOAD["positioning_summary"]
    assert n.peak_season_text == VALID_PAYLOAD["peak_season_text"]
    assert len(n.amenity_badges) == 4
    assert [c.title for c in n.positioning_cards] == [
        "Location Premium", "Statement Quality", "Revenue Potential",
    ]


def test_loaded_file_is_stamped_with_the_callers_season_labels(tmp_path):
    n = N.load_narratives_from_file(
        _write(tmp_path, VALID_PAYLOAD), "Peak Season (Jul-Oct)", "Shoulder (Nov-Jun)"
    )
    assert n.peak_season_label == "Peak Season (Jul-Oct)"
    assert n.shoulder_season_label == "Shoulder (Nov-Jun)"


def test_file_path_beats_the_api_key(case, tmp_path, monkeypatch):
    """A named file wins even when a key is configured."""
    prop, rentalizer, comps, calculator = case
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-should-not-be-used")

    def explode(*_a, **_k):
        raise AssertionError("the API must not be called when a file is given")

    monkeypatch.setattr(N, "_generate_via_api", explode)
    out = _run(N.generate_narratives(
        prop, rentalizer, comps, calculator,
        narratives_file=_write(tmp_path, VALID_PAYLOAD),
    ))
    assert out.positioning_summary == VALID_PAYLOAD["positioning_summary"]


def test_wrapped_object_is_unwrapped(tmp_path):
    """{"narratives": {...}} is the shape a model writes when asked for
    "the narratives". Tolerate it rather than failing on it."""
    n = N.load_narratives_from_file(_write(tmp_path, {"narratives": VALID_PAYLOAD}))
    assert n.guest_profile == VALID_PAYLOAD["guest_profile"]


def test_unknown_keys_warn_but_do_not_block(tmp_path, capsys):
    payload = dict(VALID_PAYLOAD, positioning_card="typo'd key")
    n = N.load_narratives_from_file(_write(tmp_path, payload))
    assert n.positioning_summary
    assert "positioning_card" in capsys.readouterr().out


def test_short_list_warns(tmp_path, capsys):
    payload = dict(VALID_PAYLOAD, positioning_cards=VALID_PAYLOAD["positioning_cards"][:2])
    N.load_narratives_from_file(_write(tmp_path, payload))
    assert "expects 3" in capsys.readouterr().out


# ── failure modes: loud, never a silent fallback ─────────────────────────

def test_missing_file_raises_and_says_how_to_get_a_brief(tmp_path):
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(tmp_path / "nope.json")
    msg = str(e.value)
    assert "not found" in msg
    assert "narrative-brief" in msg


def test_malformed_json_names_the_line(tmp_path):
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, '{"positioning_summary": '))
    assert "not valid JSON" in str(e.value)
    assert "line" in str(e.value)


def test_markdown_fenced_file_is_rejected_with_advice(tmp_path):
    fenced = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```\n"
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, fenced))
    assert "code fences" in str(e.value)


def test_top_level_list_is_rejected(tmp_path):
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, [VALID_PAYLOAD]))
    assert "JSON object" in str(e.value)


@pytest.mark.parametrize("field", NARRATIVE_FIELDS)
def test_every_missing_prose_field_is_reported(tmp_path, field):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != field}
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, payload))
    assert field in str(e.value)


@pytest.mark.parametrize("field", NARRATIVE_LIST_FIELDS)
def test_every_missing_list_field_is_reported(tmp_path, field):
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != field}
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, payload))
    assert field in str(e.value)


def test_empty_string_is_not_accepted_as_copy(tmp_path):
    payload = dict(VALID_PAYLOAD, guest_profile="   ")
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, payload))
    assert "guest_profile" in str(e.value)


def test_wrong_type_is_reported_with_the_type_it_got(tmp_path):
    payload = dict(VALID_PAYLOAD, amenity_upside=42)
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, payload))
    assert "must be a string" in str(e.value) and "int" in str(e.value)


def test_malformed_card_item_is_reported_by_index(tmp_path):
    cards = [dict(c) for c in VALID_PAYLOAD["positioning_cards"]]
    cards[1].pop("title")
    payload = dict(VALID_PAYLOAD, positioning_cards=cards)
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, payload))
    assert "positioning_cards[1].title" in str(e.value)


def test_all_problems_are_reported_in_one_pass(tmp_path):
    """Four broken fields should take one re-run to fix, not four."""
    payload = {k: v for k, v in VALID_PAYLOAD.items()
               if k not in ("guest_profile", "amenity_upside", "peak_season_text")}
    payload["config_description"] = ""
    with pytest.raises(N.NarrativeFileError) as e:
        N.load_narratives_from_file(_write(tmp_path, payload))
    msg = str(e.value)
    for field in ("guest_profile", "amenity_upside", "peak_season_text",
                  "config_description"):
        assert field in msg


def test_generate_narratives_does_not_swallow_a_bad_file(case, tmp_path):
    """The whole point of the handoff: a typo must not ship as a finished
    report full of boilerplate."""
    prop, rentalizer, comps, calculator = case
    bad = _write(tmp_path, {k: v for k, v in VALID_PAYLOAD.items()
                            if k != "positioning_summary"})
    with pytest.raises(N.NarrativeFileError):
        _run(N.generate_narratives(
            prop, rentalizer, comps, calculator,
            narratives_file=bad, output_dir=tmp_path,
        ))


# ══════════════════════════════════════════════════════════════════════════
# Path 2 — the Anthropic API (optional, headless platform runs)
# ══════════════════════════════════════════════════════════════════════════

class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeAPIError(Exception):
    pass


def _fake_anthropic(content_blocks, calls=None, raises=None):
    """A stand-in for the `anthropic` module with a scripted response."""
    class _Messages:
        async def create(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            if raises is not None:
                raise raises
            return SimpleNamespace(content=content_blocks)

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    return SimpleNamespace(AsyncAnthropic=_Client, APIError=_FakeAPIError)


def test_api_path_survives_a_leading_thinking_block(case, monkeypatch):
    """Newer models emit a ThinkingBlock first; indexing content[0].text on
    that raises AttributeError and burned the whole retry loop."""
    prop, rentalizer, comps, calculator = case
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    blocks = [
        _Block("thinking", thinking="considering the comp set"),
        _Block("tool_use", input=dict(VALID_PAYLOAD)),
    ]
    calls = []
    monkeypatch.setattr(N, "anthropic", _fake_anthropic(blocks, calls))

    out = _run(N.generate_narratives(prop, rentalizer, comps, calculator))
    assert out.positioning_summary == VALID_PAYLOAD["positioning_summary"]
    assert len(calls) == 1
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "emit_narratives"}
    assert calls[0]["tools"] == [N.NARRATIVE_TOOL]


def test_api_path_parses_a_fenced_text_block(case, monkeypatch):
    prop, rentalizer, comps, calculator = case
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    fenced = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
    monkeypatch.setattr(N, "anthropic", _fake_anthropic([_Block("text", text=fenced)]))
    out = _run(N.generate_narratives(prop, rentalizer, comps, calculator))
    assert out.guest_profile == VALID_PAYLOAD["guest_profile"]


def test_api_failure_falls_back_to_template_copy(case, monkeypatch, tmp_path):
    prop, rentalizer, comps, calculator = case
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        N, "anthropic", _fake_anthropic([], raises=_FakeAPIError("503")),
    )
    # N.asyncio IS the asyncio module, so bind the real sleep before patching
    # or the replacement calls itself.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(N.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))

    out = _run(N.generate_narratives(
        prop, rentalizer, comps, calculator, output_dir=tmp_path,
    ))
    assert out.positioning_summary == N.template_narratives(prop, comps).positioning_summary
    # A key was configured, so this is not the keyless handoff path: no brief.
    assert not narrative_brief_path(tmp_path, prop).exists()


def test_api_path_degrades_when_the_sdk_is_not_installed(case, monkeypatch, capsys):
    """A keyless install has no `anthropic` package. A stale key in the
    environment must not crash the import or the run."""
    prop, rentalizer, comps, calculator = case
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(N, "anthropic", None)

    out = _run(N.generate_narratives(prop, rentalizer, comps, calculator))
    assert out.positioning_summary
    assert "not installed" in capsys.readouterr().out


def test_api_prompt_carries_the_real_season_and_comp_numbers(case, monkeypatch):
    """The prompt must not hand the model a season the market did not have."""
    prop, rentalizer, comps, calculator = case
    dist = [0.03, 0.04, 0.06, 0.08, 0.10, 0.13, 0.15, 0.14, 0.09, 0.07, 0.06, 0.05]
    peak, shoulder = derive_season_labels(dist)
    prompt = N._build_prompt(prop, rentalizer, comps, calculator, peak, shoulder)

    assert peak in prompt
    stats = occupancy_stats(comps)
    assert f"{stats['median']:.0f}%" in prompt
    assert "Never invent a number" in prompt
    for c in comps:
        assert c.name in prompt


def test_api_prompt_says_unknown_rather_than_guessing_a_season(case):
    prop, rentalizer, comps, calculator = case
    prompt = N._build_prompt(prop, rentalizer, comps, calculator, "", "")
    assert "Peak season: UNKNOWN" in prompt
    assert "Do not name specific months" in prompt


# ══════════════════════════════════════════════════════════════════════════
# Regression guards on the corrected data semantics
# ══════════════════════════════════════════════════════════════════════════

def test_brief_never_uses_the_days_available_name():
    """`ttm_available_days` is UNSOLD nights. The name is gone on purpose."""
    src = (_ROOT / "generators" / "narrative_brief.py").read_text(encoding="utf-8")
    assert "days_available" not in src


def test_brief_labels_the_fee_basis(case):
    """adr excludes fees, annual_revenue includes them. Copy written from the
    brief must be told, or it will divide one by the other."""
    prop, rentalizer, comps, calculator = case
    brief = build_narrative_brief(prop, rentalizer, comps, calculator)
    rules = " ".join(brief["rules"])
    assert "fee-INCLUSIVE" in rules and "fee-EXCLUSIVE" in rules
    assert "adjusted" in brief["comp_set"]["_note"].lower()


def test_unrated_comp_is_never_presented_as_a_zero(case, tmp_path):
    prop, rentalizer, comps, calculator = case
    comps = list(comps)
    comps[0] = CompProperty(**{**comps[0].model_dump(), "rating": None})

    brief = build_narrative_brief(prop, rentalizer, comps, calculator)
    assert brief["comp_set"]["comps"][0]["rating"] is None
    assert "too few reviews" in " ".join(brief["rules"])


def test_api_prompt_says_a_young_listing_is_not_a_full_year(case):
    """Same facts as the brief, so the two narrative paths cannot drift. The
    brief was fixed to stop ranking an unstabilized subject against the comp
    median; the API prompt reads the same dict and must say the same thing."""
    from schema import SubjectPerformance
    prop, rentalizer, comps, calculator = case
    young = prop.model_copy(update={"subject_performance": SubjectPerformance(
        annual_revenue=48_183, occupancy_pct=20.0, adr=834.4,
        nights_booked=49, nights_listed=245, months_with_data=3,
    )})
    prompt = N._build_prompt(young, rentalizer, comps, calculator, "", "")
    assert "NOT a full year" in prompt
    assert "POSITION vs comp median: unknown" in prompt
