"""Narrative copy for the report. Three sources, in priority order.

    1. `--narratives <file>`   copy written by Claude Code (the intended path)
    2. `ANTHROPIC_API_KEY`     the API, for headless/platform runs only
    3. template narratives     data-driven copy derived from the comp set

This tool needs NO Anthropic API key. Users run it inside Claude Code, where
they already have Claude, so the copy is written there and handed back in with
`--narratives`. The API path stays for headless platforms that have a key and
nobody at the keyboard.

On a keyless run the agent writes `<slug>.narrative-brief.json` beside the
report and prints the loop, so the handoff is discoverable instead of being
something you have to already know about. See `generators.narrative_brief`.

Market-neutral by construction. Nothing in this module may hardcode a season,
a climate, a region, or an occupancy range. The season comes from
`generators.calculator.derive_season_labels()`, which reads AirROI's real
`monthly_revenue_distributions`; the occupancy numbers come from the comp set
that was actually selected. A Destin beach report used to ship
"Peak Season (Dec-Mar) ... Ski-in proximity" next to a chart peaking in July --
Destin Dec-Mar is 22.5% of annual revenue, and no Destin comp reached the
"75-85%" that the old fallback asserted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional, Union

# Optional: the published tool runs keyless, so a missing `anthropic` package
# must not stop it importing. Only the API path needs it, and that path checks.
try:
    import anthropic
except ImportError:  # pragma: no cover - exercised by keyless installs
    anthropic = None

import config
from generators.calculator import derive_season_labels
from generators.narrative_brief import (
    FIELD_GUIDE,
    NARRATIVE_FIELDS,
    NARRATIVE_INPUT_SCHEMA,
    NARRATIVE_LIST_FIELDS,
    NARRATIVE_RULES,
    _own_performance,
    adr_stats,
    build_narrative_brief,
    comp_occupancy_line,
    handoff_message,
    months_from_label,
    narrative_brief_path,
    narratives_output_path,
    report_data_path,
    occupancy_stats,
    share_from_label,
    write_narrative_brief,
    rerun_command,
)
from schema import (
    CalculatorDefaults,
    CompProperty,
    Narratives,
    PositioningCard,
    PropertyBasics,
    RentalizerData,
)

PathLike = Union[str, Path]

# Neutral stand-ins used when the market has no usable monthly distribution.
# They must stay vague: naming months we cannot support is the original bug.
_PEAK_FALLBACK_PHRASE = "the market's highest-revenue months"
_SHOULDER_FALLBACK_PHRASE = "the remaining months"


def _season_phrase(label: str, fallback: str) -> str:
    """Human phrase for a season: the real months, or a neutral placeholder."""
    months = months_from_label(label)
    return months if months else fallback


def _resolve_labels(
    peak_season_label: str,
    shoulder_season_label: str,
    monthly_distribution: Optional[list[float]],
    seasonal_data: Optional[list[float]],
) -> tuple[str, str]:
    """Use the caller's labels; derive them if the caller only had raw series."""
    if peak_season_label or shoulder_season_label:
        return peak_season_label or "", shoulder_season_label or ""
    if monthly_distribution or seasonal_data:
        return derive_season_labels(monthly_distribution, seasonal_data)
    return "", ""


# --------------------------------------------------------------------------
# Path 1 — narratives written by Claude Code and handed back in
# --------------------------------------------------------------------------

class NarrativeFileError(ValueError):
    """A `--narratives` file was missing, unreadable, or did not match schema.

    Raised, never swallowed. Falling back to template copy after the user
    explicitly passed a file would hide their typo behind a report that looks
    finished, which is the exact failure this whole handoff exists to avoid.
    """


# The file loader is stricter than the tool call about the two list fields.
# The tool call has a model filling a validated schema plus a three-attempt
# retry loop behind it; a hand-written file has neither, and a missing list
# renders as a silent hole in the report rather than an error.
_FILE_REQUIRED = NARRATIVE_FIELDS + NARRATIVE_LIST_FIELDS

_LIST_ITEM_KEYS = {
    "amenity_badges": ("emoji", "text"),
    "positioning_cards": ("emoji", "title", "text"),
}
_LIST_EXPECTED_LEN = {"amenity_badges": 4, "positioning_cards": 3}

_KNOWN_KEYS = set(_FILE_REQUIRED) | {
    "peak_season_label",
    "shoulder_season_label",
}


def _validate_narrative_data(data: dict, source: str) -> list[str]:
    """Return every problem with `data`, not just the first one.

    One round trip should tell the writer everything that is wrong. Reporting
    errors one at a time turns a malformed file into four failed re-runs.
    """
    problems: list[str] = []

    for field in NARRATIVE_FIELDS:
        value = data.get(field)
        if value is None:
            problems.append(f"missing required field {field!r} ({FIELD_GUIDE[field]})")
        elif not isinstance(value, str):
            problems.append(
                f"{field!r} must be a string, got {type(value).__name__}"
            )
        elif not value.strip():
            problems.append(f"{field!r} is empty ({FIELD_GUIDE[field]})")

    for field in NARRATIVE_LIST_FIELDS:
        value = data.get(field)
        keys = _LIST_ITEM_KEYS[field]
        if value is None:
            problems.append(f"missing required field {field!r} ({FIELD_GUIDE[field]})")
            continue
        if not isinstance(value, list):
            problems.append(f"{field!r} must be a list, got {type(value).__name__}")
            continue
        if not value:
            problems.append(f"{field!r} is empty; {FIELD_GUIDE[field]}")
            continue
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                problems.append(
                    f"{field}[{i}] must be an object with keys {list(keys)}, "
                    f"got {type(item).__name__}"
                )
                continue
            for key in keys:
                text = item.get(key)
                if not isinstance(text, str) or not text.strip():
                    problems.append(
                        f"{field}[{i}].{key} must be a non-empty string"
                    )

    if problems:
        problems.append(f"(source: {source})")
    return problems


def _warn_about_shape(data: dict, source: str) -> None:
    """Non-fatal notes: a typo'd key and a short list are both worth saying out
    loud, because both render as a quietly incomplete report."""
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        print(
            f"[Narratives] {source}: ignoring unrecognised key(s) "
            f"{', '.join(repr(k) for k in unknown)} — check for a typo against "
            f"the brief's output_schema."
        )
    for field, expected in _LIST_EXPECTED_LEN.items():
        value = data.get(field)
        if isinstance(value, list) and value and len(value) != expected:
            print(
                f"[Narratives] {source}: {field} has {len(value)} item(s); the "
                f"report layout expects {expected}."
            )


def load_narratives_from_file(
    path: PathLike,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
) -> Narratives:
    """Load narrative copy from a JSON file written by Claude Code.

    Validates against the same field spec the forced tool call uses
    (`generators.narrative_brief.NARRATIVE_INPUT_SCHEMA`). Raises
    `NarrativeFileError` with every problem listed on any failure — it never
    falls back to template copy, because the caller asked for this file
    specifically.
    """
    p = Path(path)
    source = str(p)

    if not p.exists():
        raise NarrativeFileError(
            f"--narratives file not found: {p.resolve()}\n"
            f"Generate the brief first by running without --narratives; it "
            f"writes a <slug>.narrative-brief.json next to the report telling "
            f"Claude Code exactly what to write here."
        )

    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise NarrativeFileError(f"could not read {p.resolve()}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise NarrativeFileError(
            f"{p.resolve()} is not valid JSON: {e.msg} at line {e.lineno}, "
            f"column {e.colno}.\n"
            f"Write the object on its own, with no markdown code fences and no "
            f"commentary around it."
        ) from e

    # Tolerate one common wrapper: a model asked for "the narratives" sometimes
    # writes {"narratives": {...}}. Unwrap it rather than failing on it.
    if (
        isinstance(data, dict)
        and set(data) == {"narratives"}
        and isinstance(data["narratives"], dict)
    ):
        data = data["narratives"]

    if not isinstance(data, dict):
        raise NarrativeFileError(
            f"{p.resolve()} must contain a JSON object with the narrative "
            f"fields at the top level, got {type(data).__name__}."
        )

    problems = _validate_narrative_data(data, source)
    if problems:
        bullet = "\n  - ".join(problems)
        raise NarrativeFileError(
            f"{p.resolve()} does not match the narrative schema:\n  - {bullet}\n"
            f"Required fields: {', '.join(_FILE_REQUIRED)}."
        )

    _warn_about_shape(data, source)

    print(f"[Narratives] Loaded copy from {p.resolve()}")
    return _parse_narratives(data, peak_season_label, shoulder_season_label)


# --------------------------------------------------------------------------
# Path 2 — the Anthropic API (optional, for headless platform runs)
# --------------------------------------------------------------------------

def _build_prompt(
    prop: PropertyBasics,
    rentalizer: RentalizerData,
    comps: list[CompProperty],
    calculator: CalculatorDefaults,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
) -> str:
    """Build the user prompt with all property and comp data."""
    comp_summary = ""
    for i, c in enumerate(comps, 1):
        rating = f"{c.rating:.2f}" if c.rating else "unrated (too few reviews)"
        comp_summary += (
            f"  Comp {i}: {c.name} — {c.bedrooms}BR/{c.bathrooms}BA, "
            f"Sleeps {c.sleeps}, ADR=${c.adr:,.0f} (fees excluded), "
            f"AdjOcc={c.occupancy_pct:.0f}%, "
            f"Revenue=${c.annual_revenue:,.0f} (fees included), Rating={rating}\n"
        )

    peak_months = _season_phrase(peak_season_label, _PEAK_FALLBACK_PHRASE)
    shoulder_months = _season_phrase(shoulder_season_label, _SHOULDER_FALLBACK_PHRASE)
    peak_share = share_from_label(peak_season_label)

    occ_stats = occupancy_stats(comps)
    season_facts = []
    if peak_season_label:
        season_facts.append(f"- Peak season (from this market's own monthly revenue distribution): {peak_season_label}")
    else:
        season_facts.append("- Peak season: UNKNOWN for this market. Do not name specific months.")
    if shoulder_season_label:
        season_facts.append(f"- Shoulder season: {shoulder_season_label}")
    if occ_stats:
        season_facts.append(
            f"- Comp-set adjusted occupancy: median {occ_stats['median']:.0f}%, "
            f"range {occ_stats['low']:.0f}-{occ_stats['high']:.0f}% across {occ_stats['n']} properties"
        )
    season_block = "\n".join(season_facts)

    peak_clause = (
        f"the peak months ({peak_months})"
        if months_from_label(peak_season_label)
        else peak_months
    )
    shoulder_clause = (
        f"the shoulder months ({shoulder_months})"
        if months_from_label(shoulder_season_label)
        else shoulder_months
    )
    peak_share_clause = (
        f" Peak carries {peak_share:.0f}% of annual revenue here."
        if peak_share is not None
        else ""
    )

    # The hard constraints are the shared NARRATIVE_RULES, so the API path and
    # the Claude Code path are held to the same standard rather than to two
    # lists that drift apart.
    rules_block = "\n".join(f"- {r}" for r in NARRATIVE_RULES)

    # Same facts the Claude Code brief gets, so the two paths cannot drift.
    _own = _own_performance(prop, occupancy_stats(comps))
    if _own is None:
        subject_facts = (
            "- No trailing performance history (not yet listed, or too new). "
            "Every figure below is a market projection, never this property's "
            "measured result."
        )
    else:
        subject_facts = "\n".join([
            f"- NOTE: {_own['_note']}",
            f"- Trailing 12 months revenue: ${_own['annual_revenue']:,.0f} (fees included)",
            f"- Trailing 12 months adjusted occupancy: {_own['occupancy_pct']:.0f}%",
            f"- Nights booked: {_own['nights_booked']} of {_own['nights_listed']} open nights",
            f"- Average rate: ${_own['adr']:,.0f} (fees excluded)",
            f"- POSITION vs comp median: {_own['position_vs_comp_median'] or 'unknown'}",
        ])

    return f"""Analyze this short-term rental property and generate marketing narratives for the income analysis report.

SUBJECT PROPERTY:
- Address: {prop.address}
- Market: {prop.market}
- Configuration: {prop.bedrooms} bedrooms, {prop.bathrooms} bathrooms, sleeps {prop.max_guests}
- Type: {prop.property_type}
- Amenities: {', '.join(prop.amenities) if prop.amenities else 'Not reported'}
- Description: {prop.description or 'N/A'}

SUBJECT'S OWN MEASURED PERFORMANCE:
{subject_facts}

RENTALIZER ESTIMATES:
- Revenue Potential: ${rentalizer.revenue_potential:,.0f}
- ADR: ${rentalizer.adr:,.0f}
- Occupancy: {rentalizer.occupancy_pct:.0f}%

SEASONALITY AND COMP PERFORMANCE (measured, not assumed):
{season_block}

COMP SET ({len(comps)} properties):
{comp_summary}
CALCULATOR RANGES:
- Occupancy: {calculator.occ_min}-{calculator.occ_max}% (default {calculator.occ_default}%)
- ADR: ${calculator.adr_min:,}-${calculator.adr_max:,} (default ${calculator.adr_default:,})

Return a JSON object with these exact fields:
{{
  "positioning_summary": "2-3 sentences about how this property competes in the {prop.market} market. Be specific about its competitive advantages.",
  "guest_profile": "1 sentence describing the target guest segments, inferred from the bedroom/guest count, the amenities listed above, and what {prop.market} actually draws.",
  "amenity_upside": "A paragraph about how amenity upgrades could improve off-season bookings. Mention specific amenity ideas relevant to {prop.market} and its shoulder months.",
  "amenity_badges": [
    {{"emoji": "✨", "text": "Short amenity opportunity relevant to {prop.market}"}},
    {{"emoji": "🛋️", "text": "Second amenity opportunity"}},
    {{"emoji": "🌡️", "text": "Third amenity opportunity"}},
    {{"emoji": "📈", "text": "Off-Season Conversion"}}
  ],
  "positioning_cards": [
    {{"emoji": "📍", "title": "Location Premium", "text": "2-3 sentences about location advantage"}},
    {{"emoji": "⭐", "title": "Statement Quality", "text": "2-3 sentences about quality positioning"}},
    {{"emoji": "💰", "title": "Revenue Potential", "text": "2-3 sentences about revenue upside"}}
  ],
  "config_description": "1 sentence about the property layout advantage for STR (e.g., flow, presentation, utility).",
  "guests_description": "1 sentence about guest capacity alignment with bylaws and comp set.",
  "peak_season_text": "2 sentences about peak season performance drivers specific to {prop.market}. Peak here means {peak_clause}.{peak_share_clause}",
  "shoulder_season_text": "2 sentences about shoulder season strategy and how amenities can boost off-season occupancy. Shoulder here means {shoulder_clause}."
}}

HARD CONSTRAINTS:
{rules_block}
"""


# Model is a named constant so it can be swapped without hunting through the
# call site. _extract_tool_input below is deliberately block-type-aware rather
# than indexing content[0], because newer models emit a ThinkingBlock first and
# `message.content[0].text` raises AttributeError on them.
NARRATIVE_MODEL = config.NARRATIVE_MODEL

# Built from the shared spec so the tool call, the file loader and the brief's
# `output_schema` can never disagree about what a narrative object is.
NARRATIVE_TOOL = {
    "name": "emit_narratives",
    "description": "Return the narrative copy blocks for the STR income report.",
    "input_schema": NARRATIVE_INPUT_SCHEMA,
}


def _extract_tool_input(message) -> Optional[dict]:
    """Pull the tool_use payload out of a response, whatever else it contains.

    Never index content[0]: models that return extended thinking put a
    ThinkingBlock there and `.text` raises AttributeError.
    """
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            return dict(getattr(block, "input", {}) or {})
    # Fall back to any text block that parses as JSON.
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            t = (getattr(block, "text", "") or "").strip()
            if t.startswith("```"):
                t = t.split("\n", 1)[1] if "\n" in t else t
            if t.endswith("```"):
                t = t.rsplit("```", 1)[0]
            try:
                return json.loads(t.strip())
            except json.JSONDecodeError:
                continue
    return None


SYSTEM_PROMPT = """You are a short-term rental analyst writing for the operator, a boutique vacation rental management company.

You write about whatever market the data describes: beach, urban, desert, lake, mountain, or rural. You have no home market and no default season. Every seasonal, climatic, or activity claim must come from the data in the prompt.

Writing style:
- Professional, data-informed, and confident but not hyperbolic
- Use specific numbers and comp references when possible
- Focus on competitive positioning and revenue optimization
- Speak to property owners/investors who care about ROI
- Keep language concise and impactful

Never invent a number. If the prompt does not give you a figure, describe the direction without quantifying it.

Return ONLY valid JSON. No markdown code fences, no commentary outside the JSON."""


async def _generate_via_api(
    prop: PropertyBasics,
    rentalizer: RentalizerData,
    comps: list[CompProperty],
    calculator: CalculatorDefaults,
    peak_label: str,
    shoulder_label: str,
) -> Optional[Narratives]:
    """Three attempts against the API. None when every one of them failed."""
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = _build_prompt(
        prop, rentalizer, comps, calculator, peak_label, shoulder_label
    )

    for attempt in range(3):
        try:
            message = await client.messages.create(
                model=NARRATIVE_MODEL,
                max_tokens=2000,
                # No cache_control: the system prompt is ~191 tokens and
                # Anthropic's minimum cacheable prefix is 1024, so it never
                # created a cache entry (measured: cache_creation_input = 0).
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                # Forcing a tool call makes the model emit a validated object
                # instead of prose-wrapped JSON. Free-text JSON failed to parse
                # intermittently, and each failure burned the whole retry loop;
                # three in a row silently shipped the generic fallback copy.
                tools=[NARRATIVE_TOOL],
                tool_choice={"type": "tool", "name": NARRATIVE_TOOL["name"]},
            )

            data = _extract_tool_input(message)
            if data is None:
                raise ValueError("no tool_use block in response")
            return _parse_narratives(data, peak_label, shoulder_label)

        except (json.JSONDecodeError, ValueError) as e:
            print(f"[Narratives] Response parse error (attempt {attempt + 1}/3): {e}")
        except anthropic.APIError as e:
            print(f"[Narratives] API error (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            print(f"[Narratives] Unexpected error (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    return None


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def generate_narratives(
    prop: PropertyBasics,
    rentalizer: RentalizerData,
    comps: list[CompProperty],
    calculator: CalculatorDefaults,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
    monthly_distribution: Optional[list[float]] = None,
    seasonal_data: Optional[list[float]] = None,
    narratives_file: Optional[PathLike] = None,
    output_dir: Optional[PathLike] = None,
    input_ref: str = "",
    write_brief: bool = True,
) -> Narratives:
    """Produce the report's narrative copy.

    Priority: `narratives_file` > `ANTHROPIC_API_KEY` > template narratives.

    On the keyless path this also writes `<slug>.narrative-brief.json` into
    `output_dir` and prints the copy-pasteable loop for regenerating the copy
    in Claude Code. Pass `write_brief=False` (or omit `output_dir`) to skip
    that, e.g. in tests.

    The season arguments are all optional. Pass the labels straight from
    `derive_season_labels()`, or pass the raw series and let this derive them.
    With none of them supplied the output stays market-neutral: it describes
    peak/shoulder without naming months it cannot support.

    Raises `NarrativeFileError` if `narratives_file` is given and unusable.
    Never falls back in that case — the caller named a file on purpose.
    """
    peak_label, shoulder_label = _resolve_labels(
        peak_season_label, shoulder_season_label, monthly_distribution, seasonal_data
    )

    # ---- 1. copy handed back from Claude Code ---------------------------
    if narratives_file:
        return load_narratives_from_file(narratives_file, peak_label, shoulder_label)

    # ---- 2. the API, for headless platform runs -------------------------
    if config.ANTHROPIC_API_KEY:
        if anthropic is None:
            print(
                "[Narratives] ANTHROPIC_API_KEY is set but the `anthropic` "
                "package is not installed (pip install anthropic). "
                "Using template narratives."
            )
        else:
            result = await _generate_via_api(
                prop, rentalizer, comps, calculator, peak_label, shoulder_label
            )
            if result is not None:
                return result
            print("[Narratives] All API attempts failed. Using template narratives.")
            return template_narratives(prop, comps, peak_label, shoulder_label)

    # ---- 3. template copy, plus the handoff -----------------------------
    print("[Narratives] No ANTHROPIC_API_KEY set — using template narratives.")
    if write_brief and output_dir is not None:
        try:
            emit_narrative_brief(
                prop, rentalizer, comps, calculator,
                output_dir=output_dir,
                peak_season_label=peak_label,
                shoulder_season_label=shoulder_label,
                monthly_distribution=monthly_distribution,
                seasonal_data=seasonal_data,
                input_ref=input_ref,
            )
        except OSError as e:
            # A brief that cannot be written is a lost convenience, not a lost
            # report. Say so and carry on rendering.
            print(f"[Narratives] Could not write the narrative brief: {e}")

    return template_narratives(prop, comps, peak_label, shoulder_label)


def emit_narrative_brief(
    prop: PropertyBasics,
    rentalizer: RentalizerData,
    comps: list[CompProperty],
    calculator: CalculatorDefaults,
    output_dir: PathLike,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
    monthly_distribution: Optional[list[float]] = None,
    seasonal_data: Optional[list[float]] = None,
    input_ref: str = "",
    announce: bool = True,
) -> Path:
    """Write `<slug>.narrative-brief.json` into `output_dir` and print the loop.

    Returns the path written. Split out from `generate_narratives()` so it can
    be called on its own and tested without going near the pipeline.
    """
    out = Path(output_dir)
    brief_path = narrative_brief_path(out, prop)
    narr_path = narratives_output_path(out, prop)

    brief = build_narrative_brief(
        prop, rentalizer, comps, calculator,
        peak_season_label=peak_season_label,
        shoulder_season_label=shoulder_season_label,
        monthly_distribution=monthly_distribution,
        seasonal_data=seasonal_data,
        input_ref=input_ref,
        narratives_path=str(narr_path.resolve()),
        data_path=str(report_data_path(out, prop)),
    )
    write_narrative_brief(brief, brief_path)

    if announce:
        print(handoff_message(brief_path, narr_path, rerun_command(
            str(report_data_path(out, prop)), str(narr_path))))
    return brief_path


def _parse_narratives(
    data: dict,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
) -> Narratives:
    """Parse a validated narrative object into a Narratives model."""
    positioning_cards = []
    for card_data in data.get("positioning_cards", []):
        positioning_cards.append(PositioningCard(
            emoji=card_data.get("emoji", "✨"),
            title=card_data.get("title", ""),
            text=card_data.get("text", ""),
        ))

    return Narratives(
        positioning_summary=data.get("positioning_summary", ""),
        guest_profile=data.get("guest_profile", ""),
        amenity_upside=data.get("amenity_upside", ""),
        amenity_badges=data.get("amenity_badges", []),
        positioning_cards=positioning_cards,
        config_description=data.get("config_description", ""),
        guests_description=data.get("guests_description", ""),
        peak_season_text=data.get("peak_season_text", ""),
        shoulder_season_text=data.get("shoulder_season_text", ""),
        peak_season_label=peak_season_label or "",
        shoulder_season_label=shoulder_season_label or "",
    )


# --------------------------------------------------------------------------
# Path 3 — template narratives
# --------------------------------------------------------------------------

def template_narratives(
    prop: PropertyBasics,
    comps: Optional[list[CompProperty]] = None,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
) -> Narratives:
    """Data-driven copy, used when no file and no API key are available.

    Market-neutral: no season, activity, climate or occupancy number appears
    here unless it came from `peak_season_label` (AirROI's real monthly revenue
    distribution) or from the comp set that was actually selected. This is the
    default path for the published tool, not an error path, so it has to be
    shippable prose on its own.
    """
    occ = occupancy_stats(comps)
    adr = adr_stats(comps)
    peak_months = months_from_label(peak_season_label)
    shoulder_months = months_from_label(shoulder_season_label)
    peak_share = share_from_label(peak_season_label)
    occ_line = comp_occupancy_line(occ)

    # ---- peak season -----------------------------------------------------
    if peak_months:
        peak_sentence = f"Demand in {prop.market} concentrates in {peak_months}"
        if peak_share is not None:
            peak_sentence += f", which carries {peak_share:.0f}% of annual revenue"
        peak_sentence += "."
    else:
        # No resolved months. Say nothing about WHEN peak falls -- this text
        # renders directly beneath `peak_season_label` in the report, so it
        # must not contradict a label the caller may set separately.
        peak_sentence = (
            f"Peak pricing in {prop.market} should follow the market's own "
            f"booking curve rather than a fixed calendar."
        )
    if occ_line:
        peak_sentence += (
            f" Across the full year {occ_line}, and the peak window is where "
            f"the top of that range is set."
        )
    else:
        peak_sentence += (
            " Rate strategy should follow the market's own booking curve rather "
            "than a flat annual average."
        )

    # ---- shoulder season -------------------------------------------------
    if shoulder_months:
        shoulder_sentence = (
            f"The shoulder window ({shoulder_months}) is where occupancy, not "
            f"rate, decides the year."
        )
    else:
        shoulder_sentence = (
            "Outside the peak window, occupancy rather than rate decides the year."
        )
    if occ and occ["high"] > occ["low"]:
        shoulder_sentence += (
            f" The spread between the strongest and weakest comp is "
            f"{occ['high'] - occ['low']:.0f} points of occupancy, which is the "
            f"size of the addressable gap."
        )
    else:
        shoulder_sentence += (
            " Amenity and listing-quality work is the lever that closes the gap "
            "to the stronger operators in the set."
        )

    # ---- amenity upside --------------------------------------------------
    subject_amenities = ", ".join(prop.amenities[:6]) if prop.amenities else ""
    amenity_upside = (
        f"Amenity investment pays in the off-peak window, where guests choose "
        f"on features rather than on dates. In {prop.market}, the highest-return "
        f"upgrades are the ones the local off-season traveller actually books "
        f"for, and the comp set is the guide to which those are."
    )
    if subject_amenities:
        amenity_upside += (
            f" The subject already lists {subject_amenities}, so the question is "
            f"which gaps against the comp set are worth closing."
        )
    if occ:
        amenity_upside += (
            f" With comp occupancy spanning {occ['low']:.0f}-{occ['high']:.0f}%, "
            f"the difference between an average and a top-quartile listing here "
            f"is measurable, not theoretical."
        )

    # ---- positioning -----------------------------------------------------
    revenue_card_text = (
        f"Positioning at the top of the {prop.market} comp set supports a "
        f"stronger ADR and a longer booked season, with upside from closing "
        f"amenity gaps against the leaders."
    )
    if adr:
        revenue_card_text = (
            f"The {prop.market} comp set prices between ${adr['low']:,.0f} and "
            f"${adr['high']:,.0f} per night (median ${adr['median']:,.0f}, fees "
            f"excluded). Positioning toward the upper half of that band is the "
            f"revenue lever, and amenity parity with the leaders is what "
            f"supports it."
        )

    return Narratives(
        positioning_summary=(
            f"This property sits in a premium tier for {prop.market}: "
            f"a {prop.bedrooms}-bedroom, {prop.max_guests}-guest capacity home "
            f"that competes with the top of the STR market rather than "
            f"typical like-for-like inventory."
        ),
        guest_profile=(
            f"Target guest profile: groups sized to the {prop.max_guests}-guest "
            f"capacity: family and multi-couple travel, plus the holiday and "
            f"event demand that {prop.market} draws."
        ),
        amenity_upside=amenity_upside,
        amenity_badges=[
            {"emoji": "✨", "text": "Amenity Differentiation"},
            {"emoji": "🛏️", "text": "Sleeping Capacity Fit"},
            {"emoji": "📸", "text": "Listing Presentation"},
            {"emoji": "📈", "text": "Off-Season Conversion"},
        ],
        positioning_cards=[
            PositioningCard(
                emoji="📍",
                title="Location Premium",
                text=(
                    f"Location places this property in the top tier of "
                    f"{prop.market} rentals, competing on proximity to whatever "
                    f"drives demand in this market rather than on price alone."
                ),
            ),
            PositioningCard(
                emoji="⭐",
                title="Statement Quality",
                text=(
                    "Premium finishes and architectural quality create a "
                    "statement-home experience that commands higher nightly rates."
                ),
            ),
            PositioningCard(
                emoji="💰",
                title="Revenue Potential",
                text=revenue_card_text,
            ),
        ],
        config_description=(
            "Underwrite assumes premium living/dining flow, view-forward "
            "presentation, and guest-ready utility."
        ),
        guests_description=(
            "Max guests aligned with municipality bylaws; comp set is "
            "capped accordingly to keep pricing and demand signals comparable."
        ),
        peak_season_text=peak_sentence,
        shoulder_season_text=shoulder_sentence,
        peak_season_label=peak_season_label or "",
        shoulder_season_label=shoulder_season_label or "",
    )


# Kept so anything still importing the old private name keeps working. The copy
# is the default path now, not a fallback, hence the rename.
_fallback_narratives = template_narratives
