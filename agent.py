#!/usr/bin/env python3
"""STR Comping Agent — CLI orchestrator (AirROI edition).

Usage:
    python agent.py --input "https://www.airbnb.ca/rooms/39508095"
    python agent.py --input "5005 Valley Drive Unit 13, Sun Peaks BC"
    python agent.py --input "https://www.realtor.ca/real-estate/29620437/..."
    python agent.py --input "<address>" --beds 2 --baths 2 --guests 6
    python agent.py --input "<address>" --email buyer@example.com
"""

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent))

import httpx

import config
import kit
from schema import PropertyBasics, ReportData, RentalizerData
from scrapers.airbnb import scrape_airbnb_listing
from scrapers.airroi import (run_airroi_pipeline, get_listing, get_listing_metrics,
                             get_comparables, lookup_market, get_market_occupancy, AirROIError,
                             search_radius)
from scrapers.property_search import scrape_listing_url, search_for_property, search_hero_image
from adapters.airroi_to_comp import (
    map_batch_for_scorer, to_comp_property, subject_for_scorer,
    subject_performance_from_listing,
)
from comp_scorer import bedroom_tolerance, guest_tolerance, rank_comps
import comp_filters
import comp_similarity


from generators.calculator import (derive_calculator_defaults,
                                   derive_seasonal_data_with_basis, derive_season_labels,
                                   market_occupancy_band, market_months_missing)
from generators.narratives import (
    generate_narratives, load_narratives_from_file, NarrativeFileError,
)
from generators.narrative_brief import report_data_path
from generators.methodology import build_methodology
from validators.sanity import (
    check_subject_hero, run_phase_a, run_phase_b, write_failure_report, validate_calculator_defaults,
    url_ok,
)
from report.template_engine import save_report
from report.email_sender import send_report_email

# Force UTF-8 output on Windows (cp1252 can't encode emoji in listing names).
# reconfigure() keeps line buffering; a fresh TextIOWrapper did not, so a piped
# run held its whole log in a buffer and lost it if the run was killed.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")


# ── Comp-pool helpers ─────────────────────────────────────────────────

# Every report shows exactly this many comps (Phase A and B both check it).
COMPS_NEEDED = 6


def _file_slug(market: str | None) -> str:
    """The market as a safe filename part. --market is free text, and a "/" or
    ":" in it made the failure-report write raise and hide the real failure."""
    return re.sub(r"[^A-Za-z0-9]+", "-", market or "").strip("-") or "report"


def _listing_id(c: dict) -> str:
    """The Airbnb listing id of a raw AirROI listing or a mapped scorer dict."""
    li = c.get("listing_info") or {}
    return str(li.get("listing_id") or c.get("airbnbId") or "")


def _airroi_currency(prop: PropertyBasics) -> str:
    """AirROI's currency parameter for this report: CAD comes back as "native"."""
    return "native" if prop.currency == "CA$" else "usd"


def _airbnb_id_to_url(c: dict) -> str:
    aid = _listing_id(c) or c.get("id") or ""
    return f"https://www.airbnb.com/rooms/{aid}" if aid else ""


async def _check_liveness(urls: list[str]) -> list[bool]:
    """Batch-check URL liveness with a single shared client.

    Uses the sanity gate's own check (HEAD, then a small GET when a CDN
    refuses HEAD with 403/405), so selection never drops a comp that Phase A
    and B would have accepted. Redirects and 429 (throttled) count as live.
    """
    async def _head(client: httpx.AsyncClient, url: str) -> bool:
        return (await url_ok(client, url))[1]

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (airroi-report-agent sanity gate)"},
        timeout=12,
        follow_redirects=False,
    ) as client:
        return await asyncio.gather(*[_head(client, u) for u in urls])


async def _check_comps_usable(cands: list[dict]) -> list[bool]:
    """A comp is usable when its listing page AND its cover photo are live.

    Liveness used to probe only the listing page. A Destin comp (2026-09-26)
    had a live listing and a 404 cover photo; Phase A then blocked the whole
    report over it while a replacement candidate sat unused. A missing photo
    counts as dead: Phase A requires one.
    """
    urls = [_airbnb_id_to_url(c) for c in cands]
    photos = [(c.get("listing_info") or {}).get("cover_photo_url") or "" for c in cands]
    listing_live, photo_live = await asyncio.gather(
        _check_liveness(urls), _check_liveness(photos))
    return [a and b for a, b in zip(listing_live, photo_live)]


# ── Constants ─────────────────────────────────────────────────────────

# US state abbreviations for currency auto-detection
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}
# Canadian province abbreviations
_CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE",
    "QC", "SK", "YT",
}
# Written out, as property sites often do ("Whistler, British Columbia, Canada").
_CA_WORDS = re.compile(
    r"\b(CANADA|BRITISH COLUMBIA|ALBERTA|SASKATCHEWAN|MANITOBA|ONTARIO|QU[EÉ]BEC|"
    r"NEW BRUNSWICK|NOVA SCOTIA|PRINCE EDWARD ISLAND|NEWFOUNDLAND|YUKON|NUNAVUT|"
    r"NORTHWEST TERRITORIES)\b")
# Canadian postal code: letter-digit-letter, optional space, digit-letter-digit.
# No US ZIP or state abbreviation has this shape.
_CA_POSTAL = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[A-Z] ?\d[A-Z]\d\b")


# ── Setup verification ───────────────────────────────────────────────

def _require_branding() -> bool:
    """False (with what to do) until branding.json holds a real brand."""
    problems = config.branding_problems()
    if not problems:
        return True
    if config._BRANDING_PATH.exists():
        print("[Branding] branding.json is not usable yet: " + "; ".join(problems) + ".")
        print(f"Fix those fields in {config._BRANDING_PATH} with the student, or rebuild it:")
    else:
        print("[Branding] No branding.json yet: the report would say 'Your Company' with no logo.")
    print("\n".join(config.BRANDING_ASK))
    print("Look at the logo it saves, confirm the name, logo and colours with them, then run this again.")
    print("Nothing has been spent.")
    return False


def _verify_setup() -> bool:
    """Check required API keys are present. Direct to scripts/check_setup.py if not.

    Returns True if all required keys are set. Returns False (and prints help)
    if any required key is missing.
    """
    # Both keys are required at setup (scripts/check_setup.py), but only AirROI
    # is checked here: a run from an Airbnb URL resolves entirely through
    # AirROI, and Firecrawl is needed only for an address or a Zillow/Realtor
    # link, which fails loudly without it. There is deliberately NO Anthropic key: the narrative copy
    # comes from Claude Code via the narrative-brief handoff.
    required = [
        ("AIRROI_API_KEY", config.AIRROI_API_KEY, "AirROI",
         "https://www.airroi.com/api/developer/activate"),
    ]
    missing = [(k, label, url) for k, val, label, url in required if not val]
    if not missing:
        # Say where the key came from (never the key itself): an attendee who
        # ran the STR Secrets connections kit should see it was picked up.
        print(f"[Config] AirROI key: {config.AIRROI_KEY_SOURCE}"
              + (f"; Firecrawl key: {config.FIRECRAWL_KEY_SOURCE}"
                 if config.FIRECRAWL_API_KEY else "; no Firecrawl key (Airbnb links only)"))
        return True

    print()
    print("=" * 62)
    print("  Setup incomplete — missing required API keys")
    print("=" * 62)
    for k, label, url in missing:
        print(f"  [MISSING] {label:<12} ({k})")
        print(f"            Get one at: {url}")
    print()
    print("The keys live in the STR Secrets connections kit's .env. Run:")
    print('    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/check_setup.py')
    print()
    print("It finds the kit, opens its .env for the key to be pasted in, and tests it.")
    print("Never bare python: on a fresh Windows machine it opens the Microsoft Store.")
    print()
    return False


# ── Currency detection ───────────────────────────────────────────────

def _detect_currency(address: str) -> str:
    """Detect USD vs CAD from address text. Returns "$" or "CA$".

    Checks for US state or Canadian province abbreviations at the end of
    the address (e.g., "Arvada, CO" → "$", "Sun Peaks, BC" → "CA$").
    Falls back to "$" (USD) as AirROI defaults to USD.

    Periods are ignored and a Canadian postal code (A1A 1A1) counts as Canada:
    property sites write "Sun Peaks, B.C. V0E 5N0", and matching only "BC"
    sent that address to AirROI as USD (2026-09-28).
    """
    if not address:
        return "$"
    if _CA_POSTAL.search(address.upper()):
        return "CA$"
    # Tokenize the last few words — state/province is usually at the end
    tokens = [t.strip().replace(".", "").rstrip(",").upper() for t in address.split() if t.strip()]
    # Check last 3 tokens for a state/province match
    for token in reversed(tokens[-3:]):
        # Strip trailing zip/postal code digits
        cleaned = re.sub(r"\d.*$", "", token).strip()
        if cleaned in _CA_PROVINCES:
            return "CA$"
        if cleaned in _US_STATES:
            return "$"
    # Checked after the abbreviations so "Ontario, CA" (California) stays USD.
    if _CA_WORDS.search(address.upper()):
        return "CA$"
    return "$"


# ── Input type detection ──────────────────────────────────────────────

# Any Airbnb domain (airbnb.com, .ca, .com.au, .co.uk, .de, fr.airbnb.ca ...)
# and the /rooms/plus/<id> form. The old pattern knew .com, .ca and .co.xx
# only, so an airbnb.com.au link went to Firecrawl instead of AirROI.
_AIRBNB_ROOM = re.compile(r"^https?://(?:[\w-]+\.)*airbnb\.[a-z.]+/rooms/(?:plus/)?(\d+)", re.I)


def _is_airbnb_url(value: str) -> bool:
    return bool(re.match(r"^https?://(?:[\w-]+\.)*airbnb\.[a-z.]+/rooms/", value or "", re.I))


def _airbnb_room_id(value: str) -> str:
    m = _AIRBNB_ROOM.match(value or "")
    return m.group(1) if m else ""


def _as_int(value) -> int:
    """AirROI and scraped listings send counts as 3, 3.0 or "3.0"."""
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


# ── Interactive fallback ──────────────────────────────────────────────

def _ask_number(prompt: str, cast, *, attempts: int = 3):
    """Prompt until the answer parses; None after `attempts` bad answers.
    A typo here used to raise ValueError and abort the run AFTER the paid
    AirROI calls had already been made."""
    for remaining in range(attempts - 1, -1, -1):
        raw = input(prompt).strip()
        try:
            value = cast(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        if remaining:
            print(f"    '{raw}' is not a usable number, try again ({remaining} left).")
    return None


def _prompt_missing_details(prop: PropertyBasics) -> PropertyBasics:
    """Interactively ask for missing critical property details."""
    if prop.bedrooms <= 0:
        prop.bedrooms = _ask_number("  Number of bedrooms: ", int) or 0
    if prop.bathrooms <= 0:
        prop.bathrooms = _ask_number("  Number of bathrooms: ", float) or 0
    if prop.max_guests <= 0:
        prop.max_guests = _ask_number("  Max guests: ", int) or 0
    if not prop.market or prop.market == "Unknown Market":
        prop.market = input("  Market/city name (e.g. Sun Peaks): ").strip() or "Unknown Market"
    return prop


def _missing_details(prop: PropertyBasics) -> list[str]:
    missing = []
    if prop.bedrooms <= 0:   missing.append("--beds")
    if prop.bathrooms <= 0:  missing.append("--baths")
    if prop.max_guests <= 0: missing.append("--guests")
    if not prop.market or prop.market == "Unknown Market":
        missing.append("--market")
    return missing


def _require_details(prop: PropertyBasics) -> None:
    """Every path ends here, before anything is spent. The Airbnb path used to
    return early and skip it, so a listing AirROI sent without a size ran the
    paid comp search on 0BR / sleeps 0."""
    missing = _missing_details(prop)
    if not missing:
        return
    no_prompt = (f"\n[Input] Missing required fields: {', '.join(missing)}. "
                 f"Pass them as CLI args (non-TTY environment).")
    if not sys.stdin.isatty():
        print(no_prompt, file=sys.stderr)
        sys.exit(1)
    # isatty() is not proof anyone can type: on Windows the NUL device
    # reports as a terminal, and Claude Code runs commands that way. The
    # prompt then hit EOF and crashed with a traceback (2026-09-28).
    print("\n[Input] Missing property details. Please provide:")
    try:
        _prompt_missing_details(prop)
    except EOFError:
        print(no_prompt, file=sys.stderr)
        sys.exit(1)
    still = _missing_details(prop)
    if still:
        print(f"\n[Input] Still missing: {', '.join(still)}. Pass them as CLI args.",
              file=sys.stderr)
        sys.exit(1)


# ── Subject enrichment ────────────────────────────────────────────────

def _apply_size_overrides(prop: PropertyBasics, args) -> None:
    """--beds/--baths/--guests/--market are overrides: they always win."""
    if args.beds:   prop.bedrooms = args.beds
    if args.baths:  prop.bathrooms = args.baths
    if args.guests: prop.max_guests = args.guests
    if args.market: prop.market = args.market


def _subject_from_airroi(data: dict, raw: str, listing_id: int, args) -> PropertyBasics:
    """PropertyBasics from AirROI's /listings response for the subject."""
    li = data.get("listing_info") or {}
    pd = data.get("property_details") or {}
    hi = data.get("host_info") or {}
    loc = data.get("location_info") or {}
    ratings = data.get("ratings") or {}

    title = li.get("listing_name") or ""
    location_text = ", ".join(filter(None, [loc.get("locality"), loc.get("region")]))

    prop = PropertyBasics(
        address=location_text or raw,
        short_address=title or location_text or raw,
        market=loc.get("locality") or "Unknown Market",
        bedrooms=_as_int(pd.get("bedrooms")),
        bathrooms=float(pd.get("baths") or 0),
        max_guests=_as_int(pd.get("guests")),
        property_type=li.get("listing_type") or "Property",
        # AirROI gives us the country directly (0% null). Use it instead
        # of guessing currency from address text, which mislabelled
        # every Canadian subject as USD and understated by ~29.7%.
        country_code=(loc.get("country_code") or "").upper(),
        hero_image_url=li.get("cover_photo_url") or "",
        airbnb_url=raw,
        title=title,
        rating=ratings.get("rating_overall"),
        review_count=ratings.get("num_reviews"),
        is_superhost=bool(hi.get("superhost")),
        description=li.get("description") or "",
        latitude=loc.get("latitude"),
        longitude=loc.get("longitude"),
        amenities=[str(a) for a in (pd.get("amenities") or [])],
        min_nights=(data.get("booking_settings") or {}).get("min_nights"),
    )
    _apply_size_overrides(prop, args)

    # The subject's own trailing-12-month history. This response already
    # contains it; before this it was parsed for 15 other fields and the
    # performance block thrown away, so the report inferred the subject's
    # occupancy from comps even when we were holding its measured number.
    prop.subject_performance = subject_performance_from_listing(data)
    prop.airroi_listing_id = listing_id

    print(f"[AirROI] Found: {prop.title or prop.short_address}")
    print(f"         {prop.bedrooms}BR / {prop.bathrooms}BA / Sleeps {prop.max_guests}")
    print(f"         Rating: {prop.rating} ({prop.review_count} reviews) / Superhost: {prop.is_superhost}")
    print(f"         Amenities: {len(prop.amenities)}")
    sp = prop.subject_performance
    if sp:
        print(f"[AirROI] Subject's OWN trailing 12mo: "
              f"{prop.currency}{sp.annual_revenue:,.0f} / "
              f"{sp.occupancy_pct:.1f}% adj occ / "
              f"{sp.nights_booked} nights booked")
    else:
        print("[AirROI] No trailing history for subject "
              "— estimating from the market")
    if prop.title:
        prop.short_address = prop.title
    return prop


async def _subject_from_airbnb(raw: str, args) -> PropertyBasics:
    """Path A - Airbnb URL: AirROI get_listing (primary), HTML scrape (fallback)."""
    print(f"[Input] Airbnb URL detected: {raw}")
    room_id = _airbnb_room_id(raw)
    listing_id = int(room_id) if room_id else None

    # Try AirROI first — structured data, no scraping, full metrics
    if listing_id:
        try:
            print(f"[AirROI] Fetching listing {listing_id}...")
            return _subject_from_airroi(await get_listing(listing_id), raw, listing_id, args)
        except AirROIError as e:
            print(f"[AirROI] Listing fetch failed: {e}")
            print("[AirROI] Falling back to HTML scraper...")
        except Exception as e:
            print(f"[AirROI] Unexpected error: {e}")
            print("[AirROI] Falling back to HTML scraper...")

    # Fallback: HTML scrape (fragile, but covers edge cases)
    try:
        print("[Airbnb] HTML scraping fallback...")
        prop = await scrape_airbnb_listing(raw)
        print(f"[Airbnb] Scraped: {prop.title or prop.short_address}")
        print(f"         {prop.bedrooms}BR / {prop.bathrooms}BA / Sleeps {prop.max_guests}")
        _apply_size_overrides(prop, args)
        if prop.title:
            prop.short_address = prop.title
        return prop
    except Exception as e:
        print(f"[Airbnb] HTML scrape also failed: {e}")
        print("[Input] Falling back to manual input...")
        return PropertyBasics(
            address=raw, short_address=raw,
            market=args.market or "Unknown Market",
            bedrooms=args.beds or 0, bathrooms=args.baths or 0, max_guests=args.guests or 0,
            airbnb_url=raw,
        )


def _accept_trusted_hero(url: str) -> bool:
    return not check_subject_hero(url)


async def _subject_from_search(raw: str, args) -> PropertyBasics:
    """Path B - any listing URL (Zillow, Redfin, realtor.ca, VRBO, ...).
    Path C - plain address: search the internet for the property."""
    is_url = raw.startswith(("http://", "https://"))

    if is_url:
        print(f"[Input] Listing URL provided: {raw}")
        print("[Search] Scraping property data...")
        listing_data = await scrape_listing_url(raw)
    else:
        print(f"[Input] Address provided: {raw}")
        print("[Search] Searching for property listing...")
        listing_data = await search_for_property(raw)
        # The search found a listing at the right street number but maybe not
        # the unit asked for. Its address is what AirROI gets queried with, so
        # stop here, before any paid call, unless the user accepted it.
        note = (listing_data or {}).get("unit_mismatch")
        if note:
            if getattr(args, "allow_other_unit", False):
                print(f"[Search] {note} Continuing: --allow-other-unit.")
            else:
                print(f"\n[Search] {note}")
                print(f"         Listing found: {listing_data.get('listing_url') or ''}")
                print("  Nothing has been spent on AirROI. If that unit is a fair stand-in "
                      "(same building and layout), re-run with --allow-other-unit. Otherwise "
                      "pass this unit's own Airbnb, Zillow or Realtor link as --input.")
                sys.exit(2)

    if not listing_data:
        print("[Search] No listing found online.")
        # Still build from CLI args — but try to at least find a hero image
        hero_url = None
        if not args.hero_url and not is_url:
            hero_url = await search_hero_image(raw, accept=_accept_trusted_hero)
        return PropertyBasics(
            address=raw, short_address=raw,
            market=args.market or "Unknown Market",
            bedrooms=args.beds or 0, bathrooms=args.baths or 0, max_guests=args.guests or 0,
            hero_image_url=hero_url or "",
        )

    print(f"[Search] Found: {listing_data.get('raw_address') or listing_data.get('title')}")
    hero = listing_data.get("hero_image_url") or ""
    if hero:
        print(f"         Hero: {hero[:70]}...")
    print(f"         {listing_data.get('bedrooms')}BR / {listing_data.get('bathrooms')}BA / "
          f"{listing_data.get('sqft') or '?'} sqft / {listing_data.get('property_type')}")

    prop = PropertyBasics(
        address=listing_data.get("raw_address") or raw,
        short_address=listing_data.get("raw_address") or raw,
        market=listing_data.get("market") or "Unknown Market",
        bedrooms=_as_int(listing_data.get("bedrooms")),
        bathrooms=float(listing_data.get("bathrooms") or 0.0),
        max_guests=_as_int(listing_data.get("max_guests")),
        property_type=listing_data.get("property_type") or "Property",
        hero_image_url=hero,
        listing_url=listing_data.get("listing_url"),
        description=listing_data.get("description"),
        title=listing_data.get("title"),
        sqft=_as_int(listing_data.get("sqft")) or None,
        # The listing's own features (porch, deck, hot tub...). Without
        # them an address subject reached the feature gate with nothing,
        # and six hot-tub comps priced a house with no hot tub.
        amenities=list(listing_data.get("features") or []),
    )
    # --market is an OVERRIDE (see argparse help), so it wins over the scraped
    # value, exactly like --beds/--baths/--guests.
    _apply_size_overrides(prop, args)
    # Guest capacity is never inferred. A for-sale listing does not state it,
    # and bedrooms x 2 + 2 put a six-bedroom Salem building at 14 guests
    # (2026-09-30). _require_details stops the run and Claude asks the user.
    # The listing had no usable photo (a map or a logo is refused), or one
    # the photo gate will refuse (an address often resolves to a local
    # rental company's site: Sun Peaks, 2026-09-28), so look for one on a
    # trusted host before the gate stops the run and asks the user.
    if not args.hero_url and (not prop.hero_image_url
                              or check_subject_hero(prop.hero_image_url)):
        if prop.hero_image_url:
            print(f"[Search] Listing photo is from an untrusted site "
                  f"({urlparse(prop.hero_image_url).netloc}); searching for a trusted one...")
        found = await search_hero_image(prop.address or raw, accept=_accept_trusted_hero)
        prop.hero_image_url = found or prop.hero_image_url
    return prop


async def _resolve_subject(args) -> PropertyBasics:
    """Turn the raw --input into a fully-populated PropertyBasics.

    Path A - Airbnb URL: AirROI get_listing (primary), HTML scrape (fallback)
    Path B - listing URL or address: scrape / search
    Every path: CLI overrides, then prompts (or exit) for missing details.
    """
    raw = args.input
    if _is_airbnb_url(raw):
        prop = await _subject_from_airbnb(raw, args)
    else:
        prop = await _subject_from_search(raw, args)

    # Manual overrides from CLI flags (always take precedence). The Airbnb
    # path returned before these, so --hero-url could not fix a refused photo.
    if args.hero_url:
        prop.hero_image_url = args.hero_url
    if args.listing_url:
        prop.listing_url = args.listing_url

    _refuse_multi_unit(prop, args)
    _require_details(prop)
    return prop


# Words a portal uses for a building of several separate dwellings.
_MULTI_UNIT = re.compile(r"multi[\s_-]?family|duplex|triplex|fourplex|quadplex|\b\d+[\s-]?units?\b|"
                         r"apartment building", re.I)


def _refuse_multi_unit(prop: PropertyBasics, args) -> None:
    """Stop, before anything is spent, on a multi-unit building sized as a whole.

    393 Essex St, Salem (2026-09-30) is five apartments; Zillow sums them to
    6BR / 7BA, and the run comped it as one six-bedroom house. The comps and
    the headline were for a property that does not exist. Sizing one unit
    with --beds, --baths and --guests is the way through."""
    if not _MULTI_UNIT.search(prop.property_type or ""):
        return
    if args.beds and args.baths and args.guests:
        print(f"[Input] Multi-unit building: comping one unit as {args.beds}BR / "
              f"{args.baths}BA / sleeps {args.guests}, as given.")
        return
    print(f"\n[Input] {prop.short_address} is listed as a multi-unit building "
          f"({prop.property_type}, {prop.bedrooms}BR / {prop.bathrooms:g}BA in total).", file=sys.stderr)
    print("[Input] One comp set for the whole building would price it as a single "
          f"{prop.bedrooms}-bedroom home. Nothing has been spent.", file=sys.stderr)
    print("NEXT: ask the user which unit to comp (its bedrooms, bathrooms and how many guests it "
          "sleeps), then re-run the same command with --beds N --baths N --guests N for that unit. "
          "Comp each unit separately if they want the whole building.", file=sys.stderr)
    sys.exit(1)


# ── AirROI estimate -> RentalizerData ────────────────────────────────

def _adopt_estimate_location(prop: PropertyBasics, estimate_data: dict | None) -> bool:
    """Give an address or Zillow subject the coordinates AirROI geocoded.

    Only the Airbnb path sets prop.latitude/longitude. Every other subject
    reached Step 8 with none, so the market curve was never bought and the
    scorer's distance category scored every comp 0, while /calculator/estimate
    had already geocoded the address in the same run and returned it as
    `location`. Measured on 1131 Tanrac Trl, Gatlinburg (2026-09-25):
    35.7461037, -83.4811331.

    A listing's own coordinates are never overwritten. Returns True only when
    coordinates were adopted.
    """
    if prop.latitude is not None and prop.longitude is not None:
        return False
    loc = (estimate_data or {}).get("location")
    if not isinstance(loc, dict):
        return False
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng)):
        return False
    prop.latitude, prop.longitude = float(lat), float(lng)
    return True


def _build_rentalizer(estimate_data: dict, prop: PropertyBasics) -> RentalizerData:
    """Construct a RentalizerData from AirROI's calculator/estimate response."""
    adr = float(estimate_data.get("average_daily_rate") or 0)
    occ = float(estimate_data.get("occupancy") or 0)
    # AirROI returns occupancy as 0-1; convert to 0-100
    if occ <= 1.0:
        occ *= 100

    annual_rev = float(estimate_data.get("revenue") or 0)

    # Revenue potential: p75, NOT p90.
    #
    # AirROI's `percentiles` block is the spread of its MODEL'S PREDICTIONS for
    # this property profile, not the spread of observed results. Measured on Sun
    # Peaks against the 25 comparable listings returned in the same response:
    #
    #     percentile   pool actually did   AirROI predicted   gap
    #     p25                 74,129             72,447        2%
    #     p50                102,509            108,062        5%
    #     p75                143,641            150,068        4%
    #     p90                153,509            196,645       28%
    #
    # p25 through p75 track the real pool. p90 is where the model's tail leaves
    # the data behind: it claimed a ceiling 28% above what the single best of 25
    # listings actually earned. p75 is the last percentile still anchored to
    # observed performance, so it is the last one we can defend to a client.
    rev_potential = 0.0
    percentiles = estimate_data.get("percentiles") or {}
    if isinstance(percentiles, dict):
        rev_pct = percentiles.get("revenue") or {}
        if isinstance(rev_pct, dict) and rev_pct.get("p75"):
            rev_potential = float(rev_pct["p75"])

    if not rev_potential:
        # No p75 in the response: the estimate itself is the only defensible
        # figure. A multiplier on it would be a number nobody measured.
        rev_potential = annual_rev

    # Monthly seasonality from revenue distribution ratios
    # These are proportions (sum to 1.0) — convert to monthly revenue estimates
    monthly_rev: list[float] = []
    monthly_occ: list[float] = []
    distributions = estimate_data.get("monthly_revenue_distributions") or []
    if isinstance(distributions, list) and len(distributions) == 12:
        monthly_rev = [round(ratio * annual_rev, 2) for ratio in distributions]
        # We can't derive occupancy from revenue ratios — leave empty so
        # the per-comp metrics fetch (Step 8) provides real occupancy data
        monthly_occ = []

    return RentalizerData(
        revenue_potential=round(rev_potential, 2),
        adr=round(adr, 2),
        occupancy_pct=round(occ, 2),
        monthly_occupancy=monthly_occ,
        monthly_revenue=monthly_rev,
    )


# ── Main orchestration ────────────────────────────────────────────────

async def _render_and_gate(report_data: ReportData, slug: str) -> Path:
    """Render to staging, run the Phase B gate, promote on success.

    Shared by the full pipeline and by --render so both paths get identical
    treatment: the user never sees a half-broken HTML file either way.
    """
    staging_dir = config.OUTPUT_DIR / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    staging_path = save_report(report_data, staging_dir)
    print(f"[Staging] Rendered to: {staging_path.resolve()}")

    print("\n--- Sanity Phase B (post-render, blocking) ---")
    phase_b_failures = await run_phase_b(staging_path)
    if phase_b_failures:
        write_failure_report("B", phase_b_failures, config.OUTPUT_DIR, subject_slug=slug)
        staging_path.unlink(missing_ok=True)
        print("\n[SANITY] Phase B blocking — staging file deleted, no report delivered.")
        sys.exit(2)
    print("[Sanity] Phase B passed — 6 comp cards, all images live, "
          "no AirDNA strings, no Jinja artifacts.")

    final_path = config.OUTPUT_DIR / staging_path.name
    staging_path.replace(final_path)
    print(f"[Report] Saved to: {final_path.resolve()}")
    return final_path



async def _render_only(args) -> None:
    """--render: rebuild the HTML from cached data plus new narrative copy.

    This is the second half of the Claude Code loop. It spends NO API credit:
    the expensive work (AirROI, comp liveness probes) already happened on the
    first pass and its result is on disk. The only network use is the free
    Phase B image check. Swapping in better copy should be free, otherwise
    nobody does it twice.
    """
    data_path = Path(args.render)
    if not data_path.exists():
        print(f"[Render] No such file: {data_path}")
        sys.exit(2)

    try:
        report_data = ReportData.model_validate_json(data_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Render] {data_path} is not a valid report-data file: {e}")
        sys.exit(2)

    print(f"[Render] Loaded pipeline output from {data_path.name} "
          f"({len(report_data.comps)} comps, no API calls needed)")

    if args.narratives:
        try:
            report_data.narratives = load_narratives_from_file(
                args.narratives,
                report_data.narratives.peak_season_label,
                report_data.narratives.shoulder_season_label,
            )
            print(f"[Render] Applied narrative copy from {args.narratives}")
        except NarrativeFileError as e:
            print(f"[Render] {e}")
            sys.exit(2)

    report_data.report_date = date.today().strftime("%B %d, %Y")
    slug = _file_slug(report_data.property.market)
    output_path = await _render_and_gate(report_data, slug)

    if args.email:
        print(f"\n--- Emailing report to {args.email} ---")
        try:
            send_report_email(args.email, report_data.property.short_address, output_path)
            print("[Email] Sent.")
        except Exception as e:
            print(f"[Email] Failed: {e}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{config.BRANDING.get('company_name', 'STR')} Income Analysis Report Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent.py --input "https://www.airbnb.ca/rooms/39508095"
  python agent.py --input "5005 Valley Drive Unit 13, Sun Peaks BC"
  python agent.py --input "<airbnb_url>" --email buyer@example.com
        """,
    )
    parser.add_argument("--input", required=False,
                        help="Airbnb URL, Zillow/Realtor URL, or a street address "
                             "(required unless --render)")
    parser.add_argument("--email", default=None,
                        help="Email address to send the report to (optional)")
    parser.add_argument("--beds", type=int, default=0,
                        help="Override bedrooms (useful for address input)")
    parser.add_argument("--baths", type=float, default=0,
                        help="Override bathrooms")
    parser.add_argument("--guests", type=int, default=0,
                        help="Override max guests")
    parser.add_argument("--market", default=None,
                        help="Market name override (e.g. 'Sun Peaks')")
    # ── Comp-set filters (ported from the public release) ──────────────
    parser.add_argument("--exclude", "--exclude-comps", default=None, dest="exclude",
                        help="Comma-separated keywords to drop from comp listing names "
                             "(e.g. 'oceanfront,beachfront'). Also accepts a full listing "
                             "name to drop one specific comp.")
    parser.add_argument("--subject-on-water", action="store_true",
                        help="Declare the subject is on the ocean/lake, which keeps waterfront "
                             "comps in the set (skips the auto water-proximity filter).")
    parser.add_argument("--allow-oceanfront-comps", action="store_true",
                        help="Disable the auto water-proximity filter entirely — keep waterfront "
                             "comps even when the subject is inland.")
    parser.add_argument("--require", default=None,
                        help="Comma-separated must-have features (e.g. 'pool,hot_tub'). Comps "
                             "missing any of them are dropped. Auto-detected from the subject "
                             "when omitted.")
    parser.add_argument("--no-feature-filter", action="store_true",
                        help="Disable the auto must-have feature filter.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass the 24h vendor-response cache and force fresh "
                             "(paid) AirROI calls.")
    # ── Narrative copy ─────────────────────────────────────────────────
    parser.add_argument("--render", default=None, metavar="DATA_JSON",
                        help="Re-render an existing report from its *.report-data.json "
                             "without re-running the pipeline. Costs nothing and makes no "
                             "API calls. Pair with --narratives to swap in better copy.")
    parser.add_argument("--narratives", default=None, metavar="FILE",
                        help="Path to a narratives JSON file (as written by Claude Code from the "
                             "emitted *.narrative-brief.json). Skips any LLM call.")
    parser.add_argument("--hero-url", default=None,
                        help="Manual subject hero image URL (use when MLS scrape is blocked)")
    parser.add_argument("--allow-other-unit", action="store_true",
                        help="Accept a listing for a different unit at the same street address "
                             "(the address search stops and asks otherwise)")
    parser.add_argument("--listing-url", default=None,
                        help="Link for the report's View Listing button")
    parser.add_argument("--currency", default=None, choices=["$", "CA$"],
                        help="Override currency (auto-detected from address: $ for US, CA$ for Canada)")
    return parser


# ── Pipeline steps ───────────────────────────────────────────────────

def _apply_currency(args, prop: PropertyBasics) -> None:
    """CLI override > AirROI country_code (authoritative) > address text."""
    if args.currency:
        prop.currency = args.currency
    elif prop.country_code:
        prop.currency = "CA$" if prop.country_code.upper() == "CA" else "$"
    else:
        prop.currency = _detect_currency(prop.address)


def _print_subject(prop: PropertyBasics) -> None:
    print(f"\n[Subject] {prop.short_address}")
    print(f"          {prop.bedrooms}BR / {prop.bathrooms}BA / Sleeps {prop.max_guests} / Market: {prop.market}")
    print(f"          Currency: {prop.currency}")
    if prop.hero_image_url:
        print(f"          Hero: {prop.hero_image_url[:80]}")


def _stop_if_hero_refused(prop: PropertyBasics) -> None:
    """Phase A refuses a missing or untrusted subject photo, but it runs after
    the paid AirROI calls. An address often resolves to a local rental
    company's site, whose photo is always refused, so check it here, free,
    before spending anything (Sun Peaks address run, 2026-09-28)."""
    hero_failures = check_subject_hero(prop.hero_image_url)
    if not hero_failures:
        return
    print("\n[SANITY] Subject photo refused — stopping before any paid AirROI call.")
    for failure in hero_failures:
        print(f"  - {failure}")
    print("  Fix: re-run with --hero-url set to a photo of this property on Airbnb, "
          "Zillow, Realtor.ca or Redfin (right-click the photo > Copy image address).")
    sys.exit(2)


async def _resource_subject_performance(prop: PropertyBasics, candidates_raw: list[dict]) -> None:
    """Re-read the subject's performance from the comp pool, in the report's currency.

    Step 1's get_listing() call defaults to currency="usd" and cannot pass the
    right one, because the currency is derived from location_info.country_code
    IN that same response. So on a Canadian property the subject's money fields
    came back USD while the comps and the estimate came back CAD, and both were
    then labelled CA$. Measured on Sun Peaks: own_performance 127,618 USD sat
    next to revenue_estimate 188,668 CAD, a 1.3737x gap, and the report read as
    though the property earned 32% below its own potential.
    The subject is normally inside the comp pool, and that copy is in the SAME
    currency as everything else, so re-source the performance block from there.
    Costs nothing: the data is already in hand. When the subject is not in the
    pool, fetch it again in the right currency ($0.10) rather than ship USD
    figures labelled CA$.
    """
    if prop.subject_performance is None or not prop.airroi_listing_id:
        return
    for cand in candidates_raw:
        if _listing_id(cand) == str(prop.airroi_listing_id):
            recast = subject_performance_from_listing(cand)
            if recast is not None:
                was = prop.subject_performance.annual_revenue
                prop.subject_performance = recast
                if abs(recast.annual_revenue - was) > 1:
                    print(f"[AirROI] Subject performance re-sourced from the "
                          f"comp pool for currency consistency: "
                          f"{prop.currency}{was:,.0f} -> "
                          f"{prop.currency}{recast.annual_revenue:,.0f}")
            return
    currency = _airroi_currency(prop)
    if currency == "usd":
        return
    try:
        recast = subject_performance_from_listing(
            await get_listing(int(prop.airroi_listing_id), currency=currency))
    except Exception as e:  # kit.KeyFailure is a BaseException and still stops the run
        print(f"[AirROI] Could not re-fetch the subject in {prop.currency} "
              f"({type(e).__name__}); leaving out its trailing figures rather than "
              f"showing USD as {prop.currency}", file=sys.stderr)
        recast = None
    prop.subject_performance = recast
    if recast is not None:
        print(f"[AirROI] Subject performance re-fetched in {prop.currency}: "
              f"{prop.currency}{recast.annual_revenue:,.0f}")


async def _fetch_estimate_and_pool(prop: PropertyBasics) -> tuple[dict, list[dict], RentalizerData]:
    """Steps 2-3: AirROI estimate + comparables, and the subject's rentalizer."""
    print("\n--- Step 2: Calling AirROI (estimate + comparables) ---")
    try:
        estimate_data, candidates_raw = await run_airroi_pipeline(
            prop, currency=_airroi_currency(prop),
        )
        rev = estimate_data.get("revenue") or 0
        adr = estimate_data.get("average_daily_rate") or 0
        occ = estimate_data.get("occupancy") or 0
        occ_pct = occ * 100 if occ <= 1 else occ
        print(f"[AirROI] Estimate: rev {prop.currency}{rev:,.0f} / ADR {prop.currency}{adr:.0f} "
              f"/ Occ {occ_pct:.0f}%")
        print(f"[AirROI] Comp candidates: {len(candidates_raw)}")
        if _adopt_estimate_location(prop, estimate_data):
            print(f"[AirROI] Subject located from the estimate: "
                  f"{prop.latitude:.5f}, {prop.longitude:.5f}")
        await _resource_subject_performance(prop, candidates_raw)
    except AirROIError as e:
        print(f"\n[AirROI] Pipeline error: {e}")
        print("[AirROI] Check address/coordinates; try specifying --beds/--baths/--guests.")
        sys.exit(1)

    print("\n--- Step 3: Building subject rentalizer ---")
    rentalizer = _build_rentalizer(estimate_data, prop)
    implied_rev = rentalizer.adr * 365 * (rentalizer.occupancy_pct / 100)
    print(f"[Rentalizer] ADR: {prop.currency}{rentalizer.adr:.0f} / Occ: {rentalizer.occupancy_pct:.0f}% / Implied yr rev: {prop.currency}{implied_rev:,.0f}")
    return estimate_data, candidates_raw, rentalizer


def _subject_airbnb_id(prop: PropertyBasics) -> str:
    return _airbnb_room_id(prop.airbnb_url or "")


def _normalize_name(text: str) -> str:
    """Lowercase, strip non-alphanumeric for fuzzy name comparison."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _drop_subject_from_pool(candidates_raw: list[dict], prop: PropertyBasics,
                            subject_id: str) -> list[dict]:
    """AirROI may return the queried property as one of its own "comparables"
    when the property is itself an active Airbnb listing."""
    subject_name_norm = _normalize_name(prop.title or prop.short_address)
    filtered = []
    for c in candidates_raw:
        # Pass 1: Airbnb ID match
        if subject_id and _listing_id(c) == subject_id:
            continue
        # Pass 2: Name match — ONLY when we have no listing id to compare on.
        # Sibling units routinely share a title (9 Nashville listings share
        # one), so with a known, different id this dropped 33% of the pool.
        if not subject_id:
            comp_name_norm = _normalize_name((c.get("listing_info") or {}).get("listing_name") or "")
            if (len(subject_name_norm) >= 10 and len(comp_name_norm) >= 10
                    and (subject_name_norm in comp_name_norm
                         or comp_name_norm in subject_name_norm)):
                continue
        filtered.append(c)
    if len(filtered) < len(candidates_raw):
        print(f"[Scorer] Filtered subject's own listing out of comp pool "
              f"({len(candidates_raw)} -> {len(filtered)})")
    return filtered


@dataclass
class PoolFilters:
    """What the subject needs from a comp, decided once and applied everywhere
    a candidate can enter the pool."""
    drop_on_water: bool
    required: list[str]
    lacking: list[str]
    exclude_terms: list[str]

    def select(self, candidates: list[dict]) -> comp_filters.PoolSelection:
        return comp_filters.select_comp_pool(
            candidates, drop_on_water=self.drop_on_water,
            required_features=self.required, lacking_features=self.lacking,
            exclude_terms=self.exclude_terms,
        )

    def enforce_like(self, selection: comp_filters.PoolSelection,
                     candidates: list[dict]) -> list[dict]:
        """Apply to `candidates` exactly what `selection` enforced on the main
        pool: the same filters (unless it had to relax them), the operator's
        --exclude always, and every lacking feature it actually dropped on.
        Never relaxes on its own, so a late candidate cannot bring back a comp
        kind the main pool kept out."""
        kept = candidates
        if not selection.relaxed:
            kept, _ = comp_filters.apply_comp_filters(
                kept, drop_on_water=self.drop_on_water, required_features=self.required)
        kept, _ = comp_filters.apply_keyword_exclusions(kept, self.exclude_terms)
        dropped_on = {f for _, fs in selection.lacking_dropped for f in fs}
        return [c for c in kept
                if not any(comp_filters.comp_mentions_feature(c, f) for f in dropped_on)]


def _decide_filters(args, prop: PropertyBasics) -> PoolFilters:
    """Water proximity + must-have / must-not-have features.

    An oceanfront comp inflates the projection for an inland subject, and a
    comp without the subject's pool/hot tub is not comparable. Both are
    decided from AirROI's own amenity list where it exists; host marketing
    copy never overrules it.
    """
    subject_water = comp_filters.classify_water_proximity(
        prop.title or "", prop.description or "", prop.amenities or [],
    )
    if args.subject_on_water:
        subject_water = comp_filters.WATER_ON
    drop_on_water = (
        not args.allow_oceanfront_comps
        and subject_water == comp_filters.WATER_INLAND
    )

    if args.require is not None:
        required = [comp_filters.normalize_feature(f)
                    for f in args.require.split(",") if f.strip()]
    elif args.no_feature_filter:
        required = []
    else:
        required = comp_filters.detect_required_features(
            prop.title or "", prop.description or "", prop.amenities or [],
        )

    # The mirror image: premium features the subject does NOT have, so comps
    # carrying them are left out (or ranked down when too few lack them).
    lacking = [] if args.no_feature_filter else comp_filters.detect_lacking_features(
        prop.title or "", prop.description or "", prop.amenities or [],
        exclude=required,
    )
    prop.lacking_features = lacking

    print(f"[Filters] Subject water proximity: {subject_water}"
          f"{' — dropping on-water comps' if drop_on_water else ''}")
    if required:
        print(f"[Filters] Subject requires: {', '.join(required)}")
    if lacking:
        print(f"[Filters] Subject lacks: {', '.join(lacking)}")
    elif not args.no_feature_filter and not comp_filters.subject_features_readable(
            prop.description, prop.amenities):
        print("[Filters] Subject amenities could not be read — comps not matched on pool/hot tub/ski access")

    exclude_terms = (
        [t.strip() for t in args.exclude.split(",") if t.strip()]
        if args.exclude else []
    )
    return PoolFilters(drop_on_water, required, lacking, exclude_terms)


def _print_selection(selection: comp_filters.PoolSelection, before_filters: int) -> None:
    for line in selection.report.lines():
        print(f"[Filters] {line}")
    if selection.lacking_dropped:
        print(f"[Filters] Dropped {len(selection.lacking_dropped)} comps with a feature the "
              f"subject lacks:")
        for name, extras in selection.lacking_dropped:
            print(f"[Filters]     - {name[:50]}  (has: {', '.join(extras)})")
    for feature in selection.lacking_relaxed:
        print(f"[Filters] Too few comps without "
              f"{comp_similarity.PREMIUM.get(feature, (0, feature.replace('_', ' ')))[1]} "
              f"to leave them out — keeping them, ranked down by the scorer and disclosed")
    for name in selection.excluded:
        print(f"[Filters] Excluded by keyword: {name[:50]}")
    if selection.relaxed:
        # A filter that empties the pool is worse than no filter. Back off
        # rather than fail the run, and say so out loud.
        print(f"[Filters] Only {selection.report.kept - len(selection.excluded)} comps "
              f"survived filtering (from {before_filters}). Relaxing filters to "
              f"keep the report usable; --exclude still applies.")


@dataclass
class CompPool:
    """The filtered comp pool plus what it took to build it."""
    selection: comp_filters.PoolSelection
    unfiltered: list[dict]       # every candidate seen, before filters
    targeted_added: int = 0


async def _targeted_search(args, prop: PropertyBasics, filters: PoolFilters,
                           selection: comp_filters.PoolSelection,
                           unfiltered: list[dict], subject_id: str) -> list[dict]:
    """When AirROI's comparables are the wrong KIND, buy one radius search.

    The comparables endpoint takes only location and size, so it can return
    24 ski-in/ski-out listings for a cabin with no ski access (Sunburst, Sun
    Peaks, 2026-09-26) and no ranking can fix a pool like that. One radius
    search ($0.50, max 10 results) asks for listings without the features
    the subject lacks, with the ones it has; they join the pool and go
    through the same filters and scoring as everything else.

    Returns the listings that are new to the pool.
    """
    reasons, by_type = comp_similarity.targeted_search_reasons(
        selection.lacking_relaxed, selection.kept, prop.property_type,
        required_short=comp_similarity.required_shortfall(unfiltered, filters.required))
    if (not reasons or args.no_feature_filter
            or prop.latitude is None or prop.longitude is None):
        return []
    print(f"[Targeted] {'; '.join(reasons)} — one radius search "
          f"({comp_similarity.TARGETED_RADIUS_MILES} mi, $0.50) for listings that match")
    flt = comp_similarity.targeted_search_filter(
        bedrooms=prop.bedrooms, bed_tolerance=bedroom_tolerance(prop.bedrooms),
        required=filters.required, lacking=filters.lacking,
        listing_type=prop.property_type if by_type and not selection.lacking_relaxed else None,
    )
    try:
        found = await search_radius(
            latitude=float(prop.latitude), longitude=float(prop.longitude),
            radius_miles=comp_similarity.TARGETED_RADIUS_MILES, filter=flt,
            sort={"num_reviews": "desc"},
            currency=_airroi_currency(prop),
        )
    except (AirROIError, httpx.HTTPError, asyncio.TimeoutError) as e:
        print(f"[Targeted] Search failed ({type(e).__name__}) — continuing with "
              f"AirROI's comparables", file=sys.stderr)
        found = []
    have = {_listing_id(c) for c in unfiltered}
    new = [c for c in found
           if _listing_id(c) not in have and _listing_id(c) != subject_id]
    print(f"[Targeted] {len(found)} found, {len(new)} new to the pool")
    return new


async def _build_comp_pool(args, prop: PropertyBasics, candidates_raw: list[dict],
                           filters: PoolFilters, subject_id: str) -> CompPool:
    """Filters, then the operator's --exclude, then relax the FILTERS only if
    the pool went thin. The exclusions survive the relaxation: an earlier
    version re-read the unfiltered pool and quietly re-admitted the very comps
    the operator had just removed by hand. See comp_filters.select_comp_pool
    and tests/test_comp_pool_selection.py."""
    unfiltered = list(candidates_raw)
    selection = filters.select(unfiltered)
    _print_selection(selection, len(unfiltered))

    new = await _targeted_search(args, prop, filters, selection, unfiltered, subject_id)
    if new:
        unfiltered = unfiltered + new
        selection = filters.select(unfiltered)
        for name, extras in selection.lacking_dropped:
            print(f"[Filters] (after targeted search) dropped {name[:50]}  "
                  f"(has: {', '.join(extras)})")
        for feature in selection.lacking_relaxed:
            print(f"[Filters] (after targeted search) still too few comps without "
                  f"{comp_filters.normalize_feature(feature).replace('_', ' ')}; "
                  f"kept, ranked down and disclosed")
    return CompPool(selection=selection, unfiltered=unfiltered, targeted_added=len(new))


async def _first_usable(candidates: list[dict], need: int, *, log_prefix: str,
                        ) -> tuple[list[dict], int]:
    """Walk `candidates` in order and return the first `need` with a live
    listing page AND a live cover photo, plus how many were probed.

    Liveness is the slowest step in the pipeline and Airbnb throttles it, so
    probe only as many as there are open slots, then reach further down for
    replacements. Previously every ranked comp (20+) was probed to fill 6.
    """
    usable: list[dict] = []
    checked = 0
    while len(usable) < need and checked < len(candidates):
        batch = candidates[checked:checked + (need - len(usable))]
        batch_live = await _check_comps_usable(batch)
        for cand, is_live in zip(batch, batch_live):
            if is_live:
                usable.append(cand)
            else:
                print(f"{log_prefix}{cand.get('name', '?')[:40]} ({_airbnb_id_to_url(cand)})")
        checked += len(batch)
    return usable, checked


async def _rescue(live_selected: list[dict], hard_fails: list[dict],
                  subject_for_scoring: dict) -> None:
    """When strict scoring + the dead-listing filter leaves <6, dip into the
    hard-failed pool and pick the closest matches to the subject. Appends to
    `live_selected` in place."""
    need = COMPS_NEEDED - len(live_selected)
    print(f"[Scorer] Rescue pass — need {need} more comp(s) from disqualified pool")

    subj_beds = subject_for_scoring.get("bedrooms") or 0
    subj_sleeps = subject_for_scoring.get("max_guests") or subject_for_scoring.get("guests") or 0
    subj_adr = subject_for_scoring.get("adr") or 0

    def _closeness(c: dict) -> float:
        """Lower = closer to subject."""
        comp_beds = c.get("bedrooms") or 0
        comp_sleeps = c.get("sleeps") or c.get("accommodates") or c.get("max_guests") or 0
        comp_adr = c.get("nightly_rate") or 0  # rate paid, not ttm_avg_rate
        bed_diff = abs(comp_beds - subj_beds)
        sleep_diff = abs(comp_sleeps - subj_sleeps) / max(1, subj_sleeps)
        adr_diff = abs(comp_adr - subj_adr) / max(1, subj_adr)
        return (bed_diff * 2.0) + sleep_diff + adr_diff

    already_picked_ids = {_listing_id(c) for c in live_selected}
    rescue_candidates = [c for c in hard_fails
                         if _listing_id(c) and _listing_id(c) not in already_picked_ids]
    rescue_candidates.sort(key=_closeness)
    rescue_liveness = await _check_comps_usable(rescue_candidates)

    bed_tol = bedroom_tolerance(subj_beds or 0)
    guest_tol = guest_tolerance(subj_sleeps or 0)
    for cand, is_live in zip(rescue_candidates, rescue_liveness):
        if len(live_selected) >= COMPS_NEEDED:
            break
        if not is_live:
            continue

        # NEVER rescue past the size gate. A 2BR/sleeps-6 is not a comp
        # for a 5BR/sleeps-12 subject no matter how thin the pool is.
        cb = cand.get("bedrooms")
        cs = cand.get("sleeps") or cand.get("max_guests")
        if subj_beds and cb is not None and abs(int(cb) - subj_beds) > bed_tol:
            continue
        if subj_sleeps and cs is not None and abs(int(cs) - subj_sleeps) > guest_tol:
            continue

        # Never rescue a dormant or dead listing.
        cand_occ = float(cand.get("occupancy_pct") or 0)
        if cand_occ < 25:
            print(f"[Scorer] Skipping dormant rescue: {cand.get('name','?')[:35]} ({cand_occ:.0f}% occ)")
            continue
        if cand.get("l90d_nights_booked") == 0:
            print(f"[Scorer] Skipping stale rescue: {cand.get('name','?')[:35]} (0 nights booked in 90d)")
            continue

        fail_reason = (cand.get("hard_fail_reason") or "")[:60]
        print(f"[Scorer] Rescued [{cand.get('name','?')[:35]}] (was: {fail_reason})")
        # Keep the provenance. Do NOT wipe hard_fail_reason — the
        # methodology section discloses that this comp was rescued.
        cand["rescued"] = True
        cand["hard_fail"] = False
        live_selected.append(cand)


async def _pick_live_comps(result: dict, subject_for_scoring: dict) -> list[dict]:
    """The top 6 ranked comps whose listing and photo are live, topped up from
    the hard-failed pool when the ranking alone cannot fill six."""
    if not (result["selected"] or result["ranked"] or result["hard_fails"]):
        return result["selected"]
    live_selected, probes = await _first_usable(
        result["ranked"], COMPS_NEEDED,
        log_prefix="[Scorer] Dropping dead listing or photo: ")
    print(f"[Scorer] Liveness: {probes} probe(s) to fill {len(live_selected)} slot(s)")
    if len(live_selected) < COMPS_NEEDED:
        await _rescue(live_selected, result.get("hard_fails", []), subject_for_scoring)
    return live_selected


async def _widen(prop: PropertyBasics, selected: list[dict], pool: CompPool,
                 filters: PoolFilters, subject_for_scoring: dict, subject_id: str) -> None:
    """Fewer than 6 comps: query adjacent bedroom counts. Appends to `selected`.

    New candidates go through the same filters the main pool did. The pass
    used to score them raw, so a comp dropped by --exclude, the water filter
    or a feature filter came straight back through an adjacent-bedroom query.
    """
    need = COMPS_NEEDED - len(selected)
    print(f"\n[Widening] Only {len(selected)} comps — need {need} more. Searching wider area...")

    already_ids = {_listing_id(c) for c in selected}
    # Every candidate already seen, filtered out or scored: each is out for a reason.
    already_ids |= {_listing_id(c) for c in pool.unfiltered}
    # Never re-admit the subject's own listing through the widening pass.
    if subject_id:
        already_ids.add(subject_id)

    # Widen only as far as the scorer will actually accept. Querying ±2 for
    # a 4BR subject returns 0/25 admissible comps: two wasted API calls
    # whose ids then occupy `already_ids` slots.
    tol = bedroom_tolerance(prop.bedrooms)
    bed_deltas = sorted((d for d in range(-tol, tol + 1) if d and prop.bedrooms + d >= 1), key=abs)
    has_coords = prop.latitude is not None and prop.longitude is not None

    async def _fetch_wider(beds_delta: int) -> list[dict]:
        try:
            where = (dict(latitude=float(prop.latitude), longitude=float(prop.longitude))
                     if has_coords else dict(address=prop.address))
            return await get_comparables(
                **where,
                bedrooms=prop.bedrooms + beds_delta, baths=prop.bathrooms,
                guests=max(2, prop.max_guests + int(beds_delta * 2.5)),
                currency=_airroi_currency(prop),
            )
        except Exception as e:
            print(f"[Widening] {prop.bedrooms + beds_delta}BR query failed: {e}", file=sys.stderr)
            return []

    wider_candidates = []
    for batch in await asyncio.gather(*[_fetch_wider(d) for d in bed_deltas]):
        for c in batch:
            lid = _listing_id(c)
            if not lid or lid in already_ids:
                continue
            already_ids.add(lid)
            wider_candidates.append(c)

    if wider_candidates:
        print(f"[Widening] Found {len(wider_candidates)} new candidates from adjacent bedroom counts")
        admissible = filters.enforce_like(pool.selection, wider_candidates)
        if len(admissible) < len(wider_candidates):
            print(f"[Widening] {len(wider_candidates) - len(admissible)} left out by the "
                  f"same filters and exclusions as the main pool")
        wider_result = rank_comps(subject_for_scoring, map_batch_for_scorer(admissible),
                                  top_n=need * 2)
        # Listing page AND cover photo, like every other comp: a live page
        # with a dead photo passed here and then blocked the report at Phase A.
        added, _ = await _first_usable(wider_result.get("ranked") or [], need,
                                       log_prefix="[Widening] Dead listing or photo skipped: ")
        for cand in added:
            print(f"[Widening] Added: {cand.get('name', '?')[:40]} ({cand.get('bedrooms')}BR)")
            selected.append(cand)

    if len(selected) < COMPS_NEEDED:
        print(f"[Widening] Still only {len(selected)} comps after widening — proceeding with what we have")


def _pool_occupancies(mapped: list[dict], subject_id: str) -> list[float]:
    """Occupancy of every candidate in the pool, not just the six comps.

    Those six are picked for quality and sit near the market's 81st
    percentile; anchoring to them over-projected by +47% across 125
    backtested listings. Exclude the subject's own listing so a strong
    subject cannot inflate its own market baseline.
    """
    out = []
    for cand in mapped:
        if subject_id and _listing_id(cand) == subject_id:
            continue
        occ = cand.get("occupancy_pct")
        if occ is not None and occ > 0:
            out.append(float(occ))
    return out


def _monthly_occupancy(rows: list) -> list[float | None]:
    """Calendar-indexed monthly occupancy (percent) from metrics rows.

    AirROI sends occupancy as a 0-1 fraction; a month with no row stays None.
    """
    monthly: list[float | None] = [None] * 12
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        try:
            mo = int(str(r.get("date") or "").split("-")[1]) - 1
        except (ValueError, IndexError):
            continue
        if not (0 <= mo <= 11):
            continue
        occ = r.get("occupancy")
        v = occ.get("avg") if isinstance(occ, dict) else occ
        if isinstance(v, (int, float)):
            monthly[mo] = float(v) * 100 if float(v) <= 1 else float(v)
    return monthly


@dataclass
class Seasonality:
    data: list[float] = field(default_factory=list)
    basis: str = ""
    p25: list = field(default_factory=list)
    p75: list = field(default_factory=list)
    market_occ: list[dict] = field(default_factory=list)
    missing_months: int = 0
    subject_monthly: list[float | None] = field(default_factory=list)


async def _market_curve(prop: PropertyBasics, rentalizer: RentalizerData,
                        n_selected: int, out: Seasonality) -> None:
    """Buy the whole-market curve for $0.11 (/markets/lookup $0.01 + occupancy
    $0.10) before paying $0.60 for six per-comp metric calls.

    It is cheaper AND better sourced: the six comps are selected for quality
    and run above the market, which is the bias the occupancy anchor removes
    from the headline. Verified live on Gatlinburg: 12 monthly rows with
    p25-p90. Needs coordinates; falls through silently to the per-comp path.
    """
    if prop.latitude is None or prop.longitude is None:
        return
    try:
        mkt = await lookup_market(float(prop.latitude), float(prop.longitude))
        out.market_occ = await get_market_occupancy(mkt)
        # Count months with REAL data. A no-data month still arrives as a
        # row, with every percentile identical, so counting rows reported
        # 12/12 for a market that only reported 9.
        out.missing_months = market_months_missing(out.market_occ)
        covered = 12 - out.missing_months
        where = mkt.get("locality") or mkt.get("region") or "market"
        print(f"[Seasonal] AirROI market curve for {where}: {covered}/12 months"
              + (f" ({out.missing_months} with no market data, filled from "
                 f"adjacent months)" if out.missing_months else ""))
        out.data, out.basis = derive_seasonal_data_with_basis(
            rentalizer, comp_monthly_data=None,
            market_occupancy=out.market_occ)
        if out.data and out.basis == "market":
            band = market_occupancy_band(out.market_occ)
            out.p25 = [v for v in band["p25"] if v is not None] and band["p25"] or []
            out.p75 = [v for v in band["p75"] if v is not None] and band["p75"] or []
            print(f"[Seasonal] Market curve used — skipping {n_selected} per-comp metric "
                  f"calls (${0.10 * n_selected:.2f} -> $0.11)")
        else:
            out.data = []
            print("[Seasonal] Market curve too thin — falling back to per-comp metrics")
    except Exception as e:  # kit.KeyFailure is a BaseException and still stops the run
        print(f"[Seasonal] Market curve unavailable ({type(e).__name__}) — "
              "falling back to per-comp metrics", file=sys.stderr)
        out.data = []


async def _subject_monthly(prop: PropertyBasics) -> list[float | None]:
    """THIS property's own monthly line, to overlay on the market band.

    Only bought when the property actually has a track record: for a
    pre-purchase comp there is nothing to plot and nothing is invented to fill
    it. One /listings/metrics/all call, $0.10. Gated on subject_performance,
    NOT on the market curve: this call is also what tells us how many months
    the listing has actually been live, which decides whether the calculator
    may anchor to its trailing year at all.
    """
    if prop.subject_performance is None or not prop.airroi_listing_id:
        return []
    try:
        rows = await get_listing_metrics(
            listing_id=int(prop.airroi_listing_id), num_months=12,
            currency=_airroi_currency(prop),
        )
    except Exception as e:  # kit.KeyFailure is a BaseException and still stops the run
        print(f"[Seasonal] Subject monthly unavailable ({type(e).__name__}) — "
              "chart will show the market band only", file=sys.stderr)
        return []
    monthly = _monthly_occupancy(rows)
    open_months = sum(1 for v in monthly if v is not None)
    prop.subject_performance.months_with_data = open_months
    print(f"[Seasonal] Subject's own line: {open_months}/12 months with data "
          f"(months with no data = listing was not open)")
    if not prop.subject_performance.is_stabilized:
        print(f"[Seasonal] Subject is NOT stabilized ({open_months}/12 months) — "
              f"its trailing-12-month figures cover a period it was not listed "
              f"for, so the projection will anchor to the market instead")
    return monthly


async def _comp_monthly(prop: PropertyBasics, selected: list[dict]) -> list[list[float | None]]:
    """Per-comp monthly occupancy, $0.10 each. Only when the market curve failed."""
    async def _one(c: dict) -> list[float | None]:
        try:
            listing_id = _listing_id(c) or c.get("id")
            if not listing_id:
                return [None] * 12
            rows = await get_listing_metrics(
                listing_id=int(listing_id), num_months=12,
                currency=_airroi_currency(prop),
            )
            return _monthly_occupancy(rows)
        except Exception as e:
            print(f"[Seasonal] Comp monthly fetch failed: {e}", file=sys.stderr)
            return [None] * 12

    return list(await asyncio.gather(*[_one(c) for c in selected]))


async def _seasonality(prop: PropertyBasics, rentalizer: RentalizerData,
                       estimate_data: dict, selected: list[dict]) -> Seasonality:
    """Step 8: the monthly occupancy curve, cheapest good source first."""
    print("\n--- Step 8: Seasonal occupancy from market data ---")
    out = Seasonality()
    await _market_curve(prop, rentalizer, len(selected), out)
    out.subject_monthly = await _subject_monthly(prop)

    if not out.data:
        comp_monthly_data = await _comp_monthly(prop, selected) if selected else []
        # Keep the basis: the methodology names the chart's source from it.
        out.data, out.basis = derive_seasonal_data_with_basis(
            rentalizer, comp_monthly_data=comp_monthly_data,
        )

    # Fallback: derive seasonal occupancy from AirROI's monthly revenue distributions
    if not out.data:
        distributions = estimate_data.get("monthly_revenue_distributions") or []
        if isinstance(distributions, list) and len(distributions) == 12:
            # Revenue ratios are proportional to occupancy × ADR. Assume ADR
            # is roughly constant month-to-month, so ratios approximate
            # relative occupancy. Scale so the average equals annual_occ.
            avg_ratio = sum(distributions) / 12  # should be ~0.0833
            if avg_ratio > 0:
                out.data = [
                    min(round((ratio / avg_ratio) * rentalizer.occupancy_pct, 1), 95.0)
                    for ratio in distributions
                ]
                out.basis = "revenue_distribution"
                print("[Seasonal] Derived from AirROI monthly revenue distributions")

    if not out.data:
        print("[Seasonal] ERROR: no usable monthly data from AirROI.")
        print("[Seasonal] Cannot deliver report without real seasonal data — exiting.")
        sys.exit(2)
    return out


_BASIS_LABEL = {
    "subject":        "the subject's own trailing 12 months",
    "market_typical": "market median occupancy (no stabilized year of the subject's own)",
    "market_strong":  "market UPPER QUARTILE (subject beat the market median in every month it ran)",
    "comp_set":       "comp-set median (no pool or subject history available)",
}

_SEASONAL_SOURCE = {
    "market": "AirROI market curve (p50, with p25-p75 band)",
    "comps": "AirROI per-comp average",
    "subject": "this property's own monthly history",
    "revenue_distribution": "AirROI revenue distribution",
}


def _print_calculator(prop: PropertyBasics, calculator, n_pool: int,
                      season: Seasonality) -> None:
    labels = {**_BASIS_LABEL, "market_pool": f"market pool median of {n_pool} listings"}
    print(f"[Calculator] Occ: {calculator.occ_min}-{calculator.occ_max}% (default {calculator.occ_default}%)")
    print(f"[Calculator]   occupancy basis: {labels.get(calculator.occ_basis, calculator.occ_basis)}")
    print(f"[Calculator] ADR: {prop.currency}{calculator.adr_min:,} - {prop.currency}{calculator.adr_max:,} (default {prop.currency}{calculator.adr_default:,})")
    print(f"[Calculator]   rate basis: {labels.get(calculator.adr_basis, calculator.adr_basis)}")
    print(f"[Calculator] Nights listed default: {calculator.days_default}")
    print(f"[Seasonal] Source: {_SEASONAL_SOURCE.get(season.basis, 'unknown')}")
    print(f"[Seasonal] Monthly occ: {[int(v) for v in season.data]}")


def _write_report_data(report_data: ReportData) -> None:
    """Cache the assembled pipeline output so the copy can be improved later
    without paying for the data again. This is what makes the Claude Code
    narrative loop free instead of a second full run."""
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data_path = report_data_path(config.OUTPUT_DIR, report_data.property)
    data_path.write_text(report_data.model_dump_json(indent=1), encoding="utf-8")
    print(f"[Data] Pipeline output cached: {data_path.name}")


def _maybe_email(args, short_address: str, output_path: Path) -> None:
    if not args.email:
        print("\n--- Email skipped (no --email provided) ---")
        return
    print(f"\n--- Emailing report to {args.email} ---")
    try:
        send_report_email(args.email, short_address, output_path)
    except Exception as e:
        print(f"[Email] Failed: {e}")
        print("[Email] Report was still saved locally.")


async def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not args.input and not args.render:
        parser.error("--input is required (an Airbnb link, a Zillow/Realtor link or an address)")

    if args.no_cache:
        os.environ["AIRROI_CACHE"] = "0"

    # Every student brands the report before the first run: without
    # branding.json it would ship as "Your Company" with no logo (2026-09-28).
    # Checked before --render too, and before anything is spent.
    if not _require_branding():
        sys.exit(2)

    # --render is a pure local operation: no keys, no network, no cost.
    if args.render:
        await _render_only(args)
        return

    if not _verify_setup():
        sys.exit(1)

    print()
    print("=" * 62)
    print(f"  {config.BRANDING['company_name']} - STR Income Analysis Agent")
    print("=" * 62)
    print()

    # Step 1: Resolve subject
    print("--- Step 1/10: Resolving subject property ---")
    prop = await _resolve_subject(args)
    _apply_currency(args, prop)
    _print_subject(prop)
    _stop_if_hero_refused(prop)

    # Steps 2-3: AirROI estimate + comparables
    estimate_data, candidates_raw, rentalizer = await _fetch_estimate_and_pool(prop)

    # Step 5: Adapt comps + score
    print("\n--- Step 5: Adapter + scorer ---")
    subject_id = _subject_airbnb_id(prop)
    n_candidates = len(candidates_raw)
    candidates_raw = _drop_subject_from_pool(candidates_raw, prop, subject_id)
    n_subject_removed = n_candidates - len(candidates_raw)

    filters = _decide_filters(args, prop)
    pool = await _build_comp_pool(args, prop, candidates_raw, filters, subject_id)

    # Comps kept despite a feature the subject lacks (lacking_relaxed) are
    # marked down by the scorer's two-way premium comparison (comp_similarity).
    mapped = map_batch_for_scorer(pool.selection.kept)
    subject_for_scoring = subject_for_scorer(prop, estimate_data)
    # Over-select so we have replacement candidates for any dead Airbnb listings
    result = rank_comps(subject_for_scoring, mapped, top_n=12)
    result["selected"] = await _pick_live_comps(result, subject_for_scoring)

    comp_funnel = {
        "candidates":      n_candidates + pool.targeted_added,
        "subject_removed": n_subject_removed,
        "filtered_out":    max(0, len(pool.unfiltered) - len(mapped)),
        "hard_fails":      len(result["hard_fails"]),
        "selected":        len(result["selected"]),
    }
    if pool.targeted_added:
        comp_funnel["targeted"] = {"added": pool.targeted_added,
                                   "radius_miles": comp_similarity.TARGETED_RADIUS_MILES}
    print(f"[Scorer] Candidates: {len(mapped)} / Hard fails: {len(result['hard_fails'])} / Passing: {len(result['ranked'])}")
    print(f"[Scorer] Score range: {result['score_range']}")

    if len(result["selected"]) < COMPS_NEEDED:
        await _widen(prop, result["selected"], pool, filters, subject_for_scoring, subject_id)
        # The widening pass pulls from a second, larger query, so the funnel
        # above no longer describes where the delivered set came from. Say so
        # rather than printing a tally that does not add up.
        comp_funnel["widened"] = True
        comp_funnel["selected"] = len(result["selected"])

    comps = [to_comp_property(c) for c in result["selected"]]

    print(f"[Scorer] Selected {len(comps)} comps:")
    for i, c in enumerate(comps, 1):
        print(f"         #{i} {c.bedrooms}BR sleeps {c.sleeps} / {prop.currency}{c.annual_revenue:,.0f}/yr / {c.occupancy_pct:.0f}% / {c.name[:40]}")

    # Step 6: Sanity checks — Phase A (blocking, pre-render)
    print("\n--- Step 6: Sanity Phase A (pre-render, blocking) ---")
    report_data = ReportData(property=prop, rentalizer=rentalizer, comps=comps)
    slug = _file_slug(prop.market)
    phase_a_failures = await run_phase_a(report_data)
    if phase_a_failures:
        write_failure_report(
            "A", phase_a_failures, config.OUTPUT_DIR, subject_slug=slug,
        )
        print("\n[SANITY] Phase A blocking — NOT generating report.")
        # Say why here too: the reasons used to reach only the JSON file.
        for failure in phase_a_failures:
            print(f"  - {failure}")
        sys.exit(2)
    print("[Sanity] Phase A passed — all 6 comps complete, hero images live, math sane.")

    # Step 7: Calculator defaults
    print("\n--- Step 7: Deriving calculator defaults ---")
    pool_occupancies = _pool_occupancies(mapped, subject_id)
    # The derive call itself happens AFTER Step 8. It needs the market
    # occupancy curve and the subject's own monthly line to decide whether the
    # subject's trailing year is real, and both are bought down there.
    print(f"[Calculator] Market pool: {len(pool_occupancies)} listings "
          f"(deferred until the market curve is in)")

    season = await _seasonality(prop, rentalizer, estimate_data, result["selected"])

    calculator = derive_calculator_defaults(
        comps, rentalizer, prop,
        pool_occupancies=pool_occupancies,
        market_occupancy=season.market_occ,
        subject_monthly=season.subject_monthly,
    )
    _print_calculator(prop, calculator, len(pool_occupancies), season)

    # Derive the season labels from THIS market's actual revenue distribution
    # rather than the hardcoded BC-ski calendar. Must happen BEFORE narratives
    # are generated, or the prose falls back to month-free text while the
    # header shows the real season.
    monthly_distribution = estimate_data.get("monthly_revenue_distributions")
    peak_label, shoulder_label = derive_season_labels(monthly_distribution, season.data)
    if peak_label:
        print(f"[Seasonal] {peak_label}")

    # Step 9: Narratives
    print("\n--- Step 9: Generating narrative content ---")
    narratives = await generate_narratives(
        prop, rentalizer, comps, calculator,
        peak_season_label=peak_label,
        shoulder_season_label=shoulder_label,
        monthly_distribution=monthly_distribution,
        seasonal_data=season.data,
        narratives_file=args.narratives,
        output_dir=config.OUTPUT_DIR,
    )
    narratives.peak_season_label = peak_label
    narratives.shoulder_season_label = shoulder_label
    print(f"[Narratives] Positioning: {narratives.positioning_summary[:80]}...")

    # Step 10: Methodology + final report
    methodology = build_methodology(
        prop, comps, peak_label, shoulder_label, calculator=calculator,
        comp_funnel=comp_funnel,
        market_months_missing=season.missing_months,
        seasonal_basis=season.basis,
        lacking_features=pool.selection.lacking_acted,
        lacking_relaxed=pool.selection.lacking_relaxed,
    )

    print("\n--- Step 10: Rendering HTML report (to staging) ---")
    report_data = ReportData(
        property=prop,
        rentalizer=rentalizer,
        comps=comps,
        calculator=calculator,
        narratives=narratives,
        methodology=methodology,
        report_date=date.today().strftime("%B %d, %Y"),
        seasonal_data=season.data,
        seasonal_p25=season.p25,
        seasonal_p75=season.p75,
        subject_monthly=season.subject_monthly,
    )

    # The Phase A gate ran before this calculator existed, so it only ever saw
    # the pydantic defaults. Validate the real one here, where a broken slider
    # (zero ADR, bounds that do not bracket the default) can still be caught
    # before the report is written.
    calc_failures = validate_calculator_defaults(calculator)
    if calc_failures:
        write_failure_report(
            "A", calc_failures, config.OUTPUT_DIR, subject_slug=slug,
        )
        print("\n[FAIL] Calculator sanity failed:", file=sys.stderr)
        for f in calc_failures:
            print(f"  - {f}", file=sys.stderr)
        # Same exit as every other blocking sanity gate.
        sys.exit(2)

    _write_report_data(report_data)
    output_path = await _render_and_gate(report_data, slug)
    _maybe_email(args, prop.short_address, output_path)

    print()
    print("=" * 62)
    print("                     Report Complete!")
    print("=" * 62)
    print(f"\n  File: {output_path.resolve()}\n")


def run() -> None:
    """main(), stopped cleanly when a vendor rejects the key or is out of
    credit anywhere in the run (kit.KeyFailure skips every fallback)."""
    try:
        asyncio.run(main())
    except kit.KeyFailure as e:
        kit.forget_setup_pass()
        print(f"\n{e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    run()
