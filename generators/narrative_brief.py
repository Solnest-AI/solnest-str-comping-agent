"""The narrative contract, and the brief that hands it to Claude Code.

This tool ships WITHOUT an Anthropic API key. Users run it inside Claude Code,
where they already have Claude, so the narrative copy is written there and fed
back in with `--narratives <file>` instead of being bought from the API.

That makes the loop:

    1. `python agent.py --input ...`   -> report with template copy, plus
                                          `<slug>.narrative-brief.json`
    2. Claude Code reads the brief, writes `<slug>.narratives.json`
    3. `python agent.py --input ... --narratives <slug>.narratives.json`

Everything the three narrative paths share lives here so they cannot drift:

  * `NARRATIVE_FIELDS` / `NARRATIVE_INPUT_SCHEMA` — the one field spec, used by
    the forced tool call (`generators.narratives.NARRATIVE_TOOL`), by
    `load_narratives_from_file()`, and by the `output_schema` block of the brief.
  * the comp-set statistics helpers — the only place a number in the narrative
    copy is allowed to come from.

Nothing here may hardcode a season, a climate, a region, or an occupancy range.
Seasons come from `generators.calculator.derive_season_labels()` reading
AirROI's real `monthly_revenue_distributions`; occupancy comes from the comp
set that was actually selected.
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schema import (
    CalculatorDefaults,
    CompProperty,
    PropertyBasics,
    RentalizerData,
)

# --------------------------------------------------------------------------
# The narrative contract — one definition, three consumers
# --------------------------------------------------------------------------

# The seven prose blocks. Every path must produce all of them; the report
# template renders each one directly.
NARRATIVE_FIELDS: list[str] = [
    "positioning_summary",
    "guest_profile",
    "amenity_upside",
    "config_description",
    "guests_description",
    "peak_season_text",
    "shoulder_season_text",
]

# The two structured blocks. The template iterates both, so an absent or
# malformed list renders as a hole in the report rather than an error.
NARRATIVE_LIST_FIELDS: list[str] = ["amenity_badges", "positioning_cards"]

# JSON-Schema for the whole object. Handed verbatim to the Anthropic tool call
# and embedded verbatim in the brief, so the API path and the Claude Code path
# are answering the same question.
NARRATIVE_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        **{f: {"type": "string"} for f in NARRATIVE_FIELDS},
        "amenity_badges": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "emoji": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["emoji", "text"],
            },
        },
        "positioning_cards": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "emoji": {"type": "string"},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["emoji", "title", "text"],
            },
        },
    },
    "required": NARRATIVE_FIELDS,
}

# What each field is for, in one line. The API path gets this as prose inside
# the prompt; the file path gets it as JSON in the brief. Same words either way.
FIELD_GUIDE: dict[str, str] = {
    "positioning_summary": (
        "2-3 sentences on how this property competes in this market. "
        "Specific about its competitive advantages."
    ),
    "guest_profile": (
        "1 sentence naming the target guest segments, inferred from the "
        "bedroom/guest count, the listed amenities, and what this market draws."
    ),
    "amenity_upside": (
        "A paragraph on how amenity upgrades could improve off-season "
        "bookings. Name amenity ideas that fit this market's shoulder months."
    ),
    "config_description": (
        "1 sentence on the layout advantage for STR (flow, presentation, utility)."
    ),
    "guests_description": (
        "1 sentence on guest-capacity alignment with local bylaws and the comp set."
    ),
    "peak_season_text": (
        "2 sentences on peak-season performance drivers in this market. "
        "Use only the peak months given in market_seasonality."
    ),
    "shoulder_season_text": (
        "2 sentences on shoulder-season strategy and how amenities lift "
        "off-season occupancy. Use only the shoulder months given."
    ),
    "amenity_badges": (
        "Exactly 4 objects {emoji, text}. Short amenity-opportunity labels, "
        "a few words each. The last one is conventionally off-season conversion."
    ),
    "positioning_cards": (
        "Exactly 3 objects {emoji, title, text}, in this order: "
        "Location Premium, Statement Quality, Revenue Potential. "
        "2-3 sentences of text each."
    ),
}

# The rules the copy has to obey. These are the ones that were actually broken
# in production, not a generic style guide: a Destin beach report once shipped
# "Peak Season (Dec-Mar) ... Ski-in proximity" beside a chart peaking in July.
NARRATIVE_RULES: list[str] = [
    "Never invent a number. If a figure is not in this brief, describe the "
    "direction without quantifying it.",
    "Use ONLY the months named in market_seasonality. Never substitute another "
    "season, climate, or region.",
    "Never state an occupancy figure or range outside the low-high band in "
    "comp_set.occupancy_pct_summary. Round bounds INWARD (a low of 61.4 becomes "
    "61.5 or 'about 62', never 61) so the range you claim never exceeds the real one.",
    "annual_revenue is fee-INCLUSIVE; nightly_rate is fee-EXCLUSIVE: the rate "
    "actually paid per booked night (room revenue / nights booked). Do not "
    "present them as the same basis.",
    "occupancy_pct is ADJUSTED occupancy (nights booked / nights open), not "
    "booked nights over 365.",
    "A comp with rating: null has too few reviews to be rated. That is not a "
    "rating of zero and must not be described as weak.",
    "Amenity ideas and guest segments must fit this market. Do not import "
    "activities the market does not have.",
    "If `subject.own_performance` is present it is this property's MEASURED "
    "result, not an estimate. Never contradict it and never call the property "
    "under-performing unless own_performance.position says it is below the "
    "comp median.",
    "When own_performance.position is 'above', the comp median is NOT a "
    "target. Do not frame its occupancy as a gap to close. Upside there comes "
    "from rate and shoulder-season demand, not from nights it already fills.",
    "When `subject.own_performance` is null the property has no track record. "
    "Write about market opportunity and never imply it currently earns or "
    "books anything.",
    "When own_performance.is_stabilized is false the listing has not been on "
    "the market a full year: its occupancy_pct and nights_listed cover months "
    "it did not exist and position_vs_comp_median is null on purpose. Never "
    "compare those two figures to the comp set, never call the property "
    "under-performing, and never present them as a year's result. Say the "
    "listing is young and that the projection is anchored to the market "
    "(see calculator_defaults.occupancy_pct.basis).",
    "Write for a property owner weighing ROI: professional, data-informed, "
    "confident, not hyperbolic. No markdown, no headings, plain sentences.",
]


# --------------------------------------------------------------------------
# Season-label parsing
#
# derive_season_labels() returns strings shaped like:
#   "Peak Season (Jul-Oct) — 45% of annual revenue"
#   "Shoulder Season (Nov-Jun)"
# We reuse those rather than recomputing, so the prompt, the brief, the
# template copy, the methodology and the report all quote one number.
# --------------------------------------------------------------------------

_MONTHS_RE = re.compile(r"\(([^)]+)\)")
_SHARE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s+of annual revenue")


def months_from_label(label: str) -> str:
    """Extract "Jul-Oct" from "Peak Season (Jul-Oct) — 45% of annual revenue"."""
    if not label:
        return ""
    match = _MONTHS_RE.search(label)
    return match.group(1).strip() if match else ""


def share_from_label(label: str) -> Optional[float]:
    """Extract 45.0 from "... — 45% of annual revenue". None when absent."""
    if not label:
        return None
    match = _SHARE_RE.search(label)
    return float(match.group(1)) if match else None


# --------------------------------------------------------------------------
# Comp-set statistics
#
# Every occupancy or ADR number that reaches the reader has to come from here.
# `comps` carries ADJUSTED occupancy (booked / open nights).
# --------------------------------------------------------------------------

def occupancy_stats(comps: Optional[list[CompProperty]]) -> Optional[dict]:
    values = sorted(
        float(c.occupancy_pct)
        for c in (comps or [])
        if c is not None and c.occupancy_pct and c.occupancy_pct > 0
    )
    if not values:
        return None
    return {
        "median": statistics.median(values),
        "low": values[0],
        "high": values[-1],
        "n": len(values),
    }


def nightly_rate_stats(comps: Optional[list[CompProperty]]) -> Optional[dict]:
    """Low / median / high of the rate each comp was actually paid per booked
    night, fees excluded. Built from nightly_rate, never from `adr`
    (ttm_avg_rate), which missed the rate paid by -13.8% to +18.9%."""
    values = sorted(
        float(c.nightly_rate) for c in (comps or [])
        if c is not None and c.nightly_rate and c.nightly_rate > 0
    )
    if not values:
        return None
    return {"median": statistics.median(values), "low": values[0], "high": values[-1]}


def comp_occupancy_line(stats: Optional[dict]) -> str:
    """One clause stating what the comp set actually did. Never invented."""
    if not stats:
        return ""
    return (
        f"the {stats['n']}-property comp set books at a median adjusted "
        f"occupancy of {stats['median']:.0f}% "
        f"(observed range {stats['low']:.0f}-{stats['high']:.0f}%)"
    )


# --------------------------------------------------------------------------
# Brief assembly
# --------------------------------------------------------------------------

def _round(value, digits: int = 1):
    """Round for display without turning None into 0.0."""
    return None if value is None else round(float(value), digits)


def _own_performance(prop, occ_summary: Optional[dict]) -> Optional[dict]:
    """The subject's OWN measured trailing 12 months, or None if unlisted.

    AirROI returns this on get_listing() for any already-listed property. It
    used to be fetched and discarded, so the copy described every subject as
    an under-performer with headroom — including one running 84% adjusted
    occupancy, 14 points ABOVE the comp median it was told to catch up to.

    `position` is precomputed rather than left to the writer: comparing two
    numbers is exactly the step a language model gets wrong under a prompt
    that rewards optimism.
    """
    sp = getattr(prop, "subject_performance", None)
    if sp is None or not sp.has_history:
        return None

    # A listing live for three months reports a trailing-12 occupancy that is
    # arithmetic over nine months it did not exist. The calculator already
    # refuses to anchor to it (see SubjectPerformance.is_stabilized); the
    # brief must refuse to rank it, or the writer is handed "20%, below the
    # comp median, MEASURED" and licensed to call a top-quartile property an
    # under-performer. That is the sentence the calculator fix took out of
    # the headline.
    stabilized = sp.is_stabilized
    position = None
    if stabilized and occ_summary and occ_summary.get("median") is not None:
        gap = sp.occupancy_pct - float(occ_summary["median"])
        position = "above" if gap >= 5 else ("below" if gap <= -5 else "in_line")

    if stabilized:
        note = (
            "MEASURED, not estimated. annual_revenue is fee-INCLUSIVE; "
            "nightly_rate is the rate actually paid per booked night, fees "
            "EXCLUDED; occupancy_pct is adjusted (booked / open nights)."
        )
    else:
        months = sp.months_with_data
        covered = (f"only {months} of the trailing 12 months" if months
                   else "less than a full year")
        note = (
            f"MEASURED, but NOT a full year: this listing reported {covered}, "
            "so occupancy_pct and nights_listed are computed over months it was "
            "not on the market and understate it. Do not compare them to the "
            "comp set, do not call the property under-performing on their "
            "basis, and do not present them as a full-year result. The real "
            "bookings (nights_booked, annual_revenue, nightly_rate) are still "
            "real. annual_revenue is fee-INCLUSIVE; nightly_rate is fee-EXCLUSIVE."
        )

    return {
        "_note": note,
        "is_stabilized": stabilized,
        "months_with_data": sp.months_with_data,
        "annual_revenue": _round(sp.annual_revenue, 0),
        "occupancy_pct": _round(sp.occupancy_pct),
        "nights_booked": sp.nights_booked,
        "nights_listed": sp.nights_listed,
        "nightly_rate": _round(sp.nightly_rate, 0),
        "revenue_per_booked_night": _round(sp.revenue_per_booked_night, 0),
        "l90d_occupancy_pct": (_round(sp.l90d_occupancy_pct)
                               if sp.l90d_occupancy_pct is not None else None),
        "position_vs_comp_median": position,
    }


def _comp_row(index: int, comp: CompProperty) -> dict:
    """One comp, flattened to the fields the copy is allowed to reference."""
    return {
        "n": index,
        "name": comp.name,
        "bedrooms": comp.bedrooms,
        "bathrooms": comp.bathrooms,
        "sleeps": comp.sleeps,
        "occupancy_pct": _round(comp.occupancy_pct),
        "nightly_rate": _round(comp.nightly_rate, 0),
        "annual_revenue": _round(comp.annual_revenue, 0),
        "distance_km": _round(comp.distance_km, 2),
        # None means too few reviews to rate. AirROI sends 0.0 for that; the
        # adapter converts it. Do not render it as a zero score.
        "rating": comp.rating,
        "review_count": comp.review_count,
        "nights_booked": comp.nights_booked,
        "nights_listed": comp.nights_listed,
        "feature_badges": list(comp.feature_badges or []),
    }


def build_narrative_brief(
    prop: PropertyBasics,
    rentalizer: RentalizerData,
    comps: list[CompProperty],
    calculator: CalculatorDefaults,
    peak_season_label: str = "",
    shoulder_season_label: str = "",
    monthly_distribution: Optional[list[float]] = None,
    seasonal_data: Optional[list[float]] = None,
    input_ref: str = "",
    narratives_path: str = "",
    data_path: str = "",
) -> dict:
    """Assemble everything Claude Code needs to write the narrative copy.

    Pure: builds and returns the dict, writes nothing. Every figure in it comes
    from the pipeline that just ran, so the copy written from it can be checked
    against the report.
    """
    occ = occupancy_stats(comps)
    rate = nightly_rate_stats(comps)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instructions": (
            "You are writing the narrative copy for a short-term-rental income "
            "report. Read `subject`, `market_seasonality`, `comp_set` and "
            "`calculator_defaults` below, obey every rule in `rules`, then "
            "write a JSON file matching `output_schema` "
            f"(see `output_example`) to {narratives_path or '<slug>.narratives.json'} "
            "and re-run the agent with --narratives pointing at it."
        ),
        "rerun_command": rerun_command(data_path, narratives_path),
        "rules": NARRATIVE_RULES,
        "subject": {
            "address": prop.address,
            "market": prop.market,
            "property_type": prop.property_type,
            "bedrooms": prop.bedrooms,
            "bathrooms": prop.bathrooms,
            "max_guests": prop.max_guests,
            "currency": prop.currency,
            "title": prop.title,
            "rating": prop.rating,
            "review_count": prop.review_count,
            "amenities": list(prop.amenities or []),
            "lacks_features": list(getattr(prop, "lacking_features", None) or []),
            "description": prop.description,
            "own_performance": _own_performance(prop, occ),
        },
        "revenue_estimate": {
            "_note": (
                "AirROI's estimate for the subject. revenue_potential is the "
                "75th percentile of AirROI's prediction range for a property of "
                "this profile in this market, fee-inclusive. It is an upper "
                "estimate, not a forecast and not a measured result: describe it "
                "as what a strong operator could reach, never as what this "
                "property earns or will earn. adr is AirROI's MODELLED rate for "
                "this profile, not a measured rate and not on a verified fee "
                "basis: do not compare it to any nightly_rate. occupancy_pct is "
                "adjusted."
            ),
            "revenue_potential": _round(rentalizer.revenue_potential, 0),
            "adr": _round(rentalizer.adr, 0),
            "occupancy_pct": _round(rentalizer.occupancy_pct),
        },
        "market_seasonality": {
            "_note": (
                "Derived from this market's own monthly revenue distribution. "
                "Empty labels mean the season is UNKNOWN here — in that case do "
                "not name any months at all."
            ),
            "peak_season_label": peak_season_label or "",
            "shoulder_season_label": shoulder_season_label or "",
            "peak_months": months_from_label(peak_season_label),
            "shoulder_months": months_from_label(shoulder_season_label),
            "peak_share_of_annual_revenue_pct": share_from_label(peak_season_label),
            "monthly_revenue_distribution": list(monthly_distribution or []),
            "monthly_occupancy_pct": list(seasonal_data or []),
        },
        "comp_set": {
            "_note": (
                "The comps the scorer actually selected. occupancy_pct is "
                "adjusted (booked / open nights); nightly_rate is the rate "
                "actually paid per booked night, fees excluded; annual_revenue "
                "includes fees; rating null = too few reviews."
            ),
            "count": len(comps or []),
            "occupancy_pct_summary": occ,
            "nightly_rate_summary": rate,
            "comps": [_comp_row(i, c) for i, c in enumerate(comps or [], 1)],
        },
        "calculator_defaults": {
            "_note": (
                "basis says where each default came from, and the report "
                "discloses it beside the headline. 'subject' = the property's "
                "own stabilized year; 'market_typical' = market median "
                "occupancy because the listing is too young to project from; "
                "'market_strong' = market upper quartile, same case but it beat "
                "the market median in every month it ran; 'market_pool' = "
                "median of every comparable AirROI returned; 'comp_set' = "
                "median of the six shown. Never describe a market-based "
                "default as this property's own result."
            ),
            "occupancy_pct": {
                "min": calculator.occ_min,
                "max": calculator.occ_max,
                "default": calculator.occ_default,
                "basis": calculator.occ_basis,
            },
            "adr": {
                "min": calculator.adr_min,
                "max": calculator.adr_max,
                "default": calculator.adr_default,
                "basis": calculator.adr_basis,
            },
            "days": {
                "min": calculator.days_min,
                "max": calculator.days_max,
                "default": calculator.days_default,
            },
        },
        "field_guide": FIELD_GUIDE,
        "output_schema": NARRATIVE_INPUT_SCHEMA,
        "output_example": _output_example(prop, occ),
    }


def _output_example(prop: PropertyBasics, occ: Optional[dict]) -> dict:
    """A filled-in shape, so nobody has to infer the JSON from the schema.

    Deliberately written as placeholders in <angle brackets>: an example with
    plausible-looking prose invites copy-paste, and copy-pasted prose is how a
    market gets a claim it cannot support.
    """
    market = prop.market or "<market>"
    # Placeholders never nest: a bracket inside a bracket reads as a typo and
    # invites the writer to leave one of them in.
    occ_hint = (
        f"Any occupancy figure must sit inside {occ['low']:.0f}-{occ['high']:.0f}%."
        if occ
        else "The comp set gave no occupancy, so quote no occupancy figure."
    )
    return {
        "positioning_summary": f"<2-3 sentences on how this competes in {market}>",
        "guest_profile": "<1 sentence naming the target guest segments>",
        "amenity_upside": f"<paragraph on off-season amenity upside> ({occ_hint})",
        "config_description": "<1 sentence on the layout advantage>",
        "guests_description": "<1 sentence on capacity vs bylaws and comp set>",
        "peak_season_text": "<2 sentences using ONLY market_seasonality.peak_months>",
        "shoulder_season_text": "<2 sentences using ONLY market_seasonality.shoulder_months>",
        "amenity_badges": [
            {"emoji": "✨", "text": "<amenity opportunity>"},
            {"emoji": "🛋️", "text": "<amenity opportunity>"},
            {"emoji": "🌡️", "text": "<amenity opportunity>"},
            {"emoji": "📈", "text": "Off-Season Conversion"},
        ],
        "positioning_cards": [
            {"emoji": "📍", "title": "Location Premium", "text": "<2-3 sentences>"},
            {"emoji": "⭐", "title": "Statement Quality", "text": "<2-3 sentences>"},
            {"emoji": "💰", "title": "Revenue Potential", "text": "<2-3 sentences>"},
        ],
    }


# --------------------------------------------------------------------------
# Writing the brief, and telling the user what to do with it
# --------------------------------------------------------------------------

def slugify(text: str, max_len: int = 60) -> str:
    """Filename-safe slug. Mirrors `report.template_engine._slugify` so the
    brief lands beside the report under the same name."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "report"


def brief_slug(prop: PropertyBasics) -> str:
    """The report's own identity slug: short_address, else market, else report.

    Same priority order as `save_report()`, so `<slug>.narrative-brief.json`
    sits next to `Report-Report-<slug>-<date>.html`.
    """
    identity = (
        prop.short_address
        or (prop.market if prop.market != "Unknown Market" else "")
        or "report"
    )
    return slugify(identity)


def narrative_brief_path(output_dir: Path, prop: PropertyBasics) -> Path:
    return Path(output_dir) / f"{brief_slug(prop)}.narrative-brief.json"


def narratives_output_path(output_dir: Path, prop: PropertyBasics) -> Path:
    """Where the brief tells Claude Code to put the copy it writes."""
    return Path(output_dir) / f"{brief_slug(prop)}.narratives.json"


def report_data_path(output_dir, prop) -> Path:
    """Where pass 1 caches its assembled output for `--render` to reuse."""
    return Path(output_dir) / f"{brief_slug(prop)}.report-data.json"


def rerun_command(data_path: str, narratives_path: str) -> str:
    """The command that applies new copy.

    MUST be the `--render` form. The `--input` form re-runs the whole pipeline,
    which costs the user another round of AirROI credit and ~30s just to change
    the wording — the exact thing `--render` exists to avoid.
    """
    data = data_path or "<slug>.report-data.json"
    out = narratives_path or "<slug>.narratives.json"
    return f'python agent.py --render "{data}" --narratives "{out}"'


def write_narrative_brief(brief: dict, path: Path) -> Path:
    """Write the brief to `path`. Returns the path actually written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(brief, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def handoff_message(brief_path: Path, narratives_path: Path, rerun_command: str) -> str:
    """The copy-pasteable loop, printed after a keyless run.

    Written as one block a first-time user can paste into Claude Code without
    editing anything. The paths are absolute for the same reason: a relative
    path is only correct from the directory the agent happened to run in.
    """
    brief_path = Path(brief_path).resolve()
    narratives_path = Path(narratives_path).resolve()
    bar = "=" * 70
    return f"""
{bar}
  NARRATIVE HANDOFF — this report shipped with template copy
{bar}

  No ANTHROPIC_API_KEY is set, which is the intended way to run this: you
  already have Claude here in Claude Code. The report is complete and valid;
  its narrative sections are data-driven boilerplate.

  To replace them with real copy, paste this to Claude Code:

  ----------------------------------------------------------------------
  Read {brief_path}
  Write the narratives it asks for to
  {narratives_path}
  Then run:
  {rerun_command}
  ----------------------------------------------------------------------

  The brief carries the comp table, this market's real peak/shoulder months,
  the calculator defaults, and the exact JSON shape to write back.
{bar}
"""
