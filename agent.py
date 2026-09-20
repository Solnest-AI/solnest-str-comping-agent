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
import io
import os
import re
import sys
from datetime import date
from pathlib import Path

# Force UTF-8 output on Windows (cp1252 can't encode emoji in listing names)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent))

import httpx

import config
from schema import PropertyBasics, ReportData, RentalizerData
from scrapers.airbnb import scrape_airbnb_listing
from scrapers.airroi import (run_airroi_pipeline, get_listing, get_listing_metrics,
                             get_comparables, lookup_market, get_market_occupancy, AirROIError)
from scrapers.airbtics import get_market_overlay
from scrapers.property_search import scrape_listing_url, search_for_property, search_hero_image
from adapters.airroi_to_comp import (
    map_batch_for_scorer, to_comp_property, subject_for_scorer,
    subject_performance_from_listing,
)
from comp_scorer import rank_comps
import comp_filters


from generators.calculator import derive_calculator_defaults, derive_seasonal_data, derive_seasonal_data_with_basis, derive_season_labels, airbtics_to_seasonal
from generators.narratives import (
    generate_narratives, load_narratives_from_file, NarrativeFileError,
)
from generators.narrative_brief import report_data_path
from generators.methodology import build_methodology
from validators.sanity import (
    run_phase_a, run_phase_b, write_failure_report, validate_calculator_defaults,
)
from report.template_engine import save_report
from report.email_sender import send_report_email


# ── Comp-pool helpers (module scope: the rescue path runs when `selected` is
#    empty, so these must not be nested inside a conditional) ──────────────

def max_bed_diff(subject_bedrooms: int) -> int:
    """Bedroom tolerance, mirroring comp_scorer's hard gate exactly."""
    if subject_bedrooms <= 4:
        return 1
    if subject_bedrooms <= 7:
        return 2
    return 3


def max_guest_diff(subject_guests: int) -> int:
    """Guest-capacity tolerance, mirroring comp_scorer's hard gate exactly."""
    if subject_guests <= 6:
        return 3
    if subject_guests <= 10:
        return 4
    if subject_guests <= 16:
        return 6
    return 8


def _airbnb_id_to_url(c: dict) -> str:
    li = c.get("listing_info") or {}
    aid = li.get("listing_id") or c.get("airbnbId") or c.get("id") or ""
    return f"https://www.airbnb.com/rooms/{aid}" if aid else ""


async def _check_liveness(urls: list[str]) -> list[bool]:
    """Batch-check URL liveness with a single shared client."""
    async def _head(client: httpx.AsyncClient, url: str) -> bool:
        if not url:
            return False
        try:
            r = await client.head(url)
            # 429 = throttled but the listing exists; do not drop a good comp
            # just because Airbnb rate-limited our probe.
            return r.status_code < 300 or r.status_code in (301, 302, 429)
        except Exception:
            return False

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (airroi-report-agent sanity gate)"},
        timeout=12,
        follow_redirects=False,
    ) as client:
        return await asyncio.gather(*[_head(client, u) for u in urls])


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


# ── Setup verification ───────────────────────────────────────────────

def _verify_setup() -> bool:
    """Check required API keys are present. Direct user to setup.py if not.

    Returns True if all required keys are set. Returns False (and prints help)
    if any required key is missing.
    """
    # AirROI is the only hard requirement. Firecrawl is needed only to resolve a
    # street address or a Zillow/Realtor link; an Airbnb URL resolves entirely
    # through AirROI. There is deliberately NO Anthropic key: the narrative copy
    # comes from Claude Code via the narrative-brief handoff.
    required = [
        ("AIRROI_API_KEY", config.AIRROI_API_KEY, "AirROI",
         "https://www.airroi.com/api/developer/activate"),
    ]
    missing = [(k, label, url) for k, val, label, url in required if not val]
    if not missing:
        return True

    print()
    print("=" * 62)
    print("  Setup incomplete — missing required API keys")
    print("=" * 62)
    for k, label, url in missing:
        print(f"  [MISSING] {label:<12} ({k})")
        print(f"            Get one at: {url}")
    print()
    print("Run the interactive setup to fix this:")
    print("    python setup.py")
    print()
    print("It walks you through each key one at a time and writes them to .env.")
    print("Everything else is optional. Firecrawl is only needed for address or\nZillow input; Airbtics adds market seasonality; Gmail enables --email.")
    print()
    return False


# ── Currency detection ───────────────────────────────────────────────

def _detect_currency(address: str) -> str:
    """Detect USD vs CAD from address text. Returns "$" or "CA$".

    Checks for US state or Canadian province abbreviations at the end of
    the address (e.g., "Arvada, CO" → "$", "Sun Peaks, BC" → "CA$").
    Falls back to "$" (USD) as AirROI defaults to USD.
    """
    if not address:
        return "$"
    # Tokenize the last few words — state/province is usually at the end
    tokens = [t.strip().rstrip(",").upper() for t in address.split() if t.strip()]
    # Check last 3 tokens for a state/province match
    for token in reversed(tokens[-3:]):
        # Strip trailing zip/postal code digits
        cleaned = re.sub(r"\d.*$", "", token).strip()
        if cleaned in _CA_PROVINCES:
            return "CA$"
        if cleaned in _US_STATES:
            return "$"
    return "$"


# ── Input type detection ──────────────────────────────────────────────

def _is_airbnb_url(value: str) -> bool:
    return bool(re.match(r"https?://(www\.)?airbnb\.(com|ca|co\.\w+)/rooms/", value or ""))


# ── Interactive fallback ──────────────────────────────────────────────

def _ask_number(prompt: str, cast, *, attempts: int = 3):
    """Prompt until the answer parses. A typo here used to raise ValueError and
    abort the run AFTER the paid AirROI calls had already been made."""
    for remaining in range(attempts - 1, -1, -1):
        raw = input(prompt) or "0"
        try:
            return cast(raw)
        except (TypeError, ValueError):
            if remaining:
                print(f"    '{raw}' is not a number, try again ({remaining} left).")
    print(f"    Giving up on '{prompt.strip()}', using 0.")
    return cast("0")


def _prompt_missing_details(prop: PropertyBasics) -> PropertyBasics:
    """Interactively ask for missing critical property details."""
    if prop.bedrooms <= 0:
        prop.bedrooms = _ask_number("  Number of bedrooms: ", int)
    if prop.bathrooms <= 0:
        prop.bathrooms = _ask_number("  Number of bathrooms: ", float)
    if prop.max_guests <= 0:
        prop.max_guests = _ask_number("  Max guests: ", int)
    if not prop.market or prop.market == "Unknown Market":
        prop.market = input("  Market/city name (e.g. Sun Peaks): ") or "Unknown Market"
    return prop


# ── Subject enrichment ────────────────────────────────────────────────

async def _resolve_subject(args) -> PropertyBasics:
    """Turn the raw --input into a fully-populated PropertyBasics.

    Path A - Airbnb URL: AirROI get_listing (primary), HTML scrape (fallback)
    Path B - realtor.ca URL or address: scrape_mls
    Fallback: interactive prompts for missing beds/baths/guests
    """
    raw = args.input

    # Path A: Airbnb URL → extract listing ID → AirROI API
    if _is_airbnb_url(raw):
        print(f"[Input] Airbnb URL detected: {raw}")
        listing_id_match = re.search(r"/rooms/(\d+)", raw)
        listing_id = int(listing_id_match.group(1)) if listing_id_match else None

        # Try AirROI first — structured data, no scraping, full metrics
        if listing_id:
            try:
                print(f"[AirROI] Fetching listing {listing_id}...")
                data = await get_listing(listing_id)

                li = data.get("listing_info") or {}
                pd = data.get("property_details") or {}
                hi = data.get("host_info") or {}
                loc = data.get("location_info") or {}
                ratings = data.get("ratings") or {}

                title = li.get("listing_name") or ""
                # AirROI gives us the country directly (0% null). Use it instead
                # of guessing currency from address text, which mislabelled
                # every Canadian subject as USD and understated by ~29.7%.
                _country_code = (loc.get("country_code") or "").upper()
                location_text = ", ".join(filter(None, [
                    loc.get("locality"), loc.get("region"),
                ]))

                prop = PropertyBasics(
                    address=location_text or raw,
                    short_address=title or location_text or raw,
                    market=args.market or loc.get("locality") or "Unknown Market",
                    bedrooms=args.beds or int(pd.get("bedrooms") or 0),
                    bathrooms=args.baths or float(pd.get("baths") or 0),
                    max_guests=args.guests or int(pd.get("guests") or 0),
                    property_type=li.get("listing_type") or "Property",
                    country_code=_country_code,
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
                )

                # The subject's own trailing-12-month history. This response
                # already contains it; before this it was parsed for 15 other
                # fields and the performance block thrown away, so the report
                # inferred the subject's occupancy from comps even when we
                # were holding its measured number.
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
            if args.beds:   prop.bedrooms = args.beds
            if args.baths:  prop.bathrooms = args.baths
            if args.guests: prop.max_guests = args.guests
            if args.market: prop.market = args.market

            if prop.title:
                prop.short_address = prop.title
            return prop
        except Exception as e:
            print(f"[Airbnb] HTML scrape also failed: {e}")
            print("[Input] Falling back to manual input...")
            return PropertyBasics(
                address=raw, short_address=raw,
                market=args.market or "Unknown Market",
                bedrooms=args.beds, bathrooms=args.baths, max_guests=args.guests,
                airbnb_url=raw,
            )

    # Path B: Any listing URL (Zillow, Redfin, realtor.ca, VRBO, etc.)
    # Path C: Plain address → search the internet for the property
    is_url = raw.startswith(("http://", "https://"))

    if is_url:
        print(f"[Input] Listing URL provided: {raw}")
        print("[Search] Scraping property data...")
        listing_data = await scrape_listing_url(raw)
    else:
        print(f"[Input] Address provided: {raw}")
        print("[Search] Searching for property listing...")
        listing_data = await search_for_property(raw)

    if listing_data:
        print(f"[Search] Found: {listing_data.get('raw_address') or listing_data.get('title')}")
        hero = listing_data.get("hero_image_url") or ""
        if hero:
            print(f"         Hero: {hero[:70]}...")
        print(f"         {listing_data.get('bedrooms')}BR / {listing_data.get('bathrooms')}BA / "
              f"{listing_data.get('sqft') or '?'} sqft / {listing_data.get('property_type')}")

        prop = PropertyBasics(
            address=listing_data.get("raw_address") or raw,
            short_address=listing_data.get("raw_address") or raw,
            # --market is an OVERRIDE (see argparse help), so it must win over the
            # scraped value, exactly like --beds/--baths/--guests below. It was
            # inverted here, which discarded the flag precisely when the scraper
            # had guessed a market and the user was correcting it.
            market=args.market or listing_data.get("market") or "Unknown Market",
            bedrooms=args.beds or int(listing_data.get("bedrooms") or 0),
            bathrooms=args.baths or float(listing_data.get("bathrooms") or 0.0),
            max_guests=args.guests or int(listing_data.get("max_guests") or 0),
            property_type=listing_data.get("property_type") or "Property",
            hero_image_url=listing_data.get("hero_image_url") or "",
            listing_url=listing_data.get("listing_url"),
            description=listing_data.get("description"),
            title=listing_data.get("title"),
            sqft=int(listing_data.get("sqft")) if listing_data.get("sqft") else None,
        )
        # Infer guest capacity from bedrooms if not found
        if prop.max_guests <= 0 and prop.bedrooms > 0:
            prop.max_guests = prop.bedrooms * 2 + 2
    else:
        print("[Search] No listing found online.")
        # Still build from CLI args — but try to at least find a hero image
        hero_url = None
        if not args.hero_url and not is_url:
            hero_url = await search_hero_image(raw)

        prop = PropertyBasics(
            address=raw, short_address=raw,
            market=args.market or "Unknown Market",
            bedrooms=args.beds, bathrooms=args.baths, max_guests=args.guests,
            hero_image_url=hero_url or "",
        )

    # Manual overrides from CLI flags (always take precedence)
    if args.hero_url:
        prop.hero_image_url = args.hero_url
    if args.listing_url:
        prop.listing_url = args.listing_url

    if (prop.bedrooms <= 0 or prop.bathrooms <= 0 or prop.max_guests <= 0
            or not prop.market or prop.market == "Unknown Market"):
        if not sys.stdin.isatty():
            missing = []
            if prop.bedrooms <= 0:   missing.append("--beds")
            if prop.bathrooms <= 0:  missing.append("--baths")
            if prop.max_guests <= 0: missing.append("--guests")
            if not prop.market or prop.market == "Unknown Market":
                missing.append("--market")
            print(f"\n[Input] Missing required fields: {', '.join(missing)}. "
                  f"Pass them as CLI args (non-TTY environment).", file=sys.stderr)
            sys.exit(1)
        print("\n[Input] Missing property details. Please provide:")
        prop = _prompt_missing_details(prop)

    return prop


# ── AirROI estimate -> RentalizerData ────────────────────────────────

def _build_rentalizer(estimate_data: dict, prop: PropertyBasics) -> RentalizerData:
    """Construct a RentalizerData from AirROI's calculator/estimate response."""
    adr = float(estimate_data.get("average_daily_rate") or 0)
    occ = float(estimate_data.get("occupancy") or 0)
    # AirROI returns occupancy as 0-1; convert to 0-100
    if occ <= 1.0:
        occ *= 100

    annual_rev = float(estimate_data.get("revenue") or 0)

    # Revenue potential: use p90 percentile if available
    rev_potential = 0.0
    percentiles = estimate_data.get("percentiles") or {}
    if isinstance(percentiles, dict):
        rev_pct = percentiles.get("revenue") or {}
        if isinstance(rev_pct, dict) and rev_pct.get("p90"):
            rev_potential = float(rev_pct["p90"])

    if not rev_potential:
        rev_potential = annual_rev * 1.3 if annual_rev else 0.0

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

    This is the second half of the Claude Code loop. It touches NO network and
    spends NO API credit: the expensive work (AirROI, Airbtics, liveness probes)
    already happened on the first pass and its result is on disk. Swapping in
    better copy should be free, otherwise nobody does it twice.
    """
    data_path = Path(args.render)
    if not data_path.exists():
        print(f"[Render] No such file: {data_path}")
        sys.exit(2)

    try:
        report_data = ReportData.model_validate_json(data_path.read_text())
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
    slug = report_data.property.market.replace(" ", "-") or "report"
    output_path = await _render_and_gate(report_data, slug)

    if args.email:
        print(f"\n--- Emailing report to {args.email} ---")
        try:
            send_report_email(args.email, report_data.property.short_address, output_path)
            print("[Email] Sent.")
        except Exception as e:
            print(f"[Email] Failed: {e}")


async def main() -> None:
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
                        help="Airbnb URL, realtor.ca URL, or physical property address")
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
    parser.add_argument("--skip-financials", action="store_true",
                        help="Skip AirROI (dev/testing only — produces an empty rentalizer)")
    parser.add_argument("--hero-url", default=None,
                        help="Manual subject hero image URL (use when MLS scrape is blocked)")
    parser.add_argument("--listing-url", default=None,
                        help="Manual realtor.ca listing URL (use when MLS search fails)")
    parser.add_argument("--currency", default=None, choices=["$", "CA$"],
                        help="Override currency (auto-detected from address: $ for US, CA$ for Canada)")

    args = parser.parse_args()

    if getattr(args, "no_cache", False):
        os.environ["AIRROI_CACHE"] = "0"

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

    # Currency: CLI override > AirROI country_code (authoritative) > address text
    if args.currency:
        prop.currency = args.currency
    elif getattr(prop, "country_code", None):
        prop.currency = "CA$" if prop.country_code.upper() == "CA" else "$"
    else:
        prop.currency = _detect_currency(prop.address)

    print(f"\n[Subject] {prop.short_address}")
    print(f"          {prop.bedrooms}BR / {prop.bathrooms}BA / Sleeps {prop.max_guests} / Market: {prop.market}")
    print(f"          Currency: {prop.currency}")
    if prop.hero_image_url:
        print(f"          Hero: {prop.hero_image_url[:80]}")

    # Step 2-3: AirROI pipeline (estimate + comparables)
    if args.skip_financials:
        print("\n--- Steps 2-3: AirROI skipped (--skip-financials) ---")
        estimate_data: dict = {}
        candidates_raw: list = []
        rentalizer = RentalizerData(revenue_potential=0, adr=0, occupancy_pct=0)
    else:
        # Map internal currency to AirROI param
        airroi_currency = "native" if prop.currency == "CA$" else "usd"

        print("\n--- Step 2: Calling AirROI (estimate + comparables) ---")
        try:
            estimate_data, candidates_raw = await run_airroi_pipeline(
                prop, currency=airroi_currency,
            )
            rev = estimate_data.get("revenue") or 0
            adr = estimate_data.get("average_daily_rate") or 0
            occ = estimate_data.get("occupancy") or 0
            print(f"[AirROI] Estimate: rev ${rev:,.0f} / ADR ${adr:.0f} / Occ {occ:.0%}")
            print(f"[AirROI] Comp candidates: {len(candidates_raw)}")

            # CURRENCY FIX. Step 1's get_listing() call defaults to currency="usd"
            # and cannot pass the right one, because the currency is derived from
            # location_info.country_code IN that same response. So on a Canadian
            # property the subject's money fields came back USD while the comps and
            # the estimate came back CAD, and both were then labelled CA$.
            # Measured on Sun Peaks: own_performance 127,618 USD sat next to
            # revenue_estimate 188,668 CAD, a 1.3737x gap, and the report read as
            # though the property earned 32% below its own potential.
            # The subject is normally inside the comp pool, and that copy is in the
            # SAME currency as everything else, so re-source the performance block
            # from there. Costs nothing: the data is already in hand.
            if prop.subject_performance is not None and prop.airroi_listing_id:
                for cand in candidates_raw:
                    li = cand.get("listing_info") or {}
                    if str(li.get("listing_id") or "") == str(prop.airroi_listing_id):
                        recast = subject_performance_from_listing(cand)
                        if recast is not None:
                            was = prop.subject_performance.annual_revenue
                            prop.subject_performance = recast
                            if abs(recast.annual_revenue - was) > 1:
                                print(f"[AirROI] Subject performance re-sourced from the "
                                      f"comp pool for currency consistency: "
                                      f"{prop.currency}{was:,.0f} -> "
                                      f"{prop.currency}{recast.annual_revenue:,.0f}")
                        break
                else:
                    if airroi_currency != "usd":
                        print("[AirROI] WARNING: subject not found in the comp pool; its "
                              "performance figures are USD while the report is "
                              f"{prop.currency}. Treat own_performance with caution.",
                              file=sys.stderr)
        except AirROIError as e:
            print(f"\n[AirROI] Pipeline error: {e}")
            print("[AirROI] Check address/coordinates; try specifying --beds/--baths/--guests.")
            sys.exit(1)

        print("\n--- Step 3: Building subject rentalizer ---")
        rentalizer = _build_rentalizer(estimate_data, prop)
        implied_rev = rentalizer.adr * 365 * (rentalizer.occupancy_pct / 100)
        print(f"[Rentalizer] ADR: {prop.currency}{rentalizer.adr:.0f} / Occ: {rentalizer.occupancy_pct:.0f}% / Implied yr rev: {prop.currency}{implied_rev:,.0f}")

    # Step 4: Airbtics overlay (optional)
    print("\n--- Step 4: Airbtics market overlay (optional) ---")
    # Pass coordinates so the precise coord lookup (Strategy 1) is reachable.
    # Without them it was name-search-only, which is how "Destin" resolved to
    # "Sandestin".
    overlay = await get_market_overlay(
        prop.market, latitude=prop.latitude, longitude=prop.longitude,
    )
    if overlay:
        s = overlay.get("summary") or {}
        print(f"[Airbtics] Market: {overlay['market'].get('name')}")
        print(f"           Market avg occ: {s.get('occupancy')}% / ADR: ${s.get('average_daily_rate')} / {s.get('active_listings_count')} listings")
        metrics = overlay.get("metrics") or []
        if metrics and not rentalizer.monthly_occupancy:
            # MUST map by the "month" key. Airbtics returns a TRAILING window
            # (e.g. 2025-07 .. 2026-06), so slicing positionally and calling
            # element 0 "January" rotates the whole seasonal curve by however
            # many months into the year the window starts. Verified live:
            # Gatlinburg's real July (83%) was being plotted as January.
            rentalizer.monthly_occupancy = airbtics_to_seasonal(metrics)
    else:
        print(f"[Airbtics] No coverage for {prop.market!r} - skipping overlay.")

    # Step 5: Adapt comps + score
    print("\n--- Step 5: Adapter + scorer ---")

    # Filter the subject's own listing out of the comp pool — AirROI may
    # return the queried property as one of its own "comparables" when the
    # property is itself an active Airbnb listing.
    subject_airbnb_id = ""
    if prop.airbnb_url:
        m = re.search(r"/rooms/(\d+)", prop.airbnb_url)
        if m:
            subject_airbnb_id = m.group(1)

    def _normalize(text: str) -> str:
        """Lowercase, strip non-alphanumeric for fuzzy name comparison."""
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    subject_name_norm = _normalize(prop.title or prop.short_address)

    before = len(candidates_raw)
    filtered = []
    for c in candidates_raw:
        # AirROI uses nested structure — extract listing_id
        li = c.get("listing_info") or {}
        listing_id = str(li.get("listing_id") or "")

        # Pass 1: Airbnb ID match
        if subject_airbnb_id and listing_id == subject_airbnb_id:
            continue
        # Pass 2: Name match — ONLY when we have no listing id to compare on.
        # Sibling units routinely share a title (9 Nashville listings share
        # one), so with a known, different id this dropped 33% of the pool.
        if not subject_airbnb_id:
            comp_name_norm = _normalize(li.get("listing_name") or "")
            if (len(subject_name_norm) >= 10 and len(comp_name_norm) >= 10
                    and (subject_name_norm in comp_name_norm
                         or comp_name_norm in subject_name_norm)):
                continue
        filtered.append(c)
    candidates_raw = filtered
    if len(candidates_raw) < before:
        print(f"[Scorer] Filtered subject's own listing out of comp pool ({before} -> {len(candidates_raw)})")

    _unfiltered_candidates = list(candidates_raw)

    # ── Comp-set filters (water proximity + must-have features) ────────
    # An oceanfront comp inflates the projection for an inland subject, and a
    # comp without the subject's pool/hot tub is not comparable. Both are
    # decided from AirROI's own amenity list where it exists; host marketing
    # copy never overrules it.
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
        required_features = [f.strip() for f in args.require.split(",") if f.strip()]
    elif args.no_feature_filter:
        required_features = []
    else:
        required_features = comp_filters.detect_required_features(
            prop.title or "", prop.description or "", prop.amenities or [],
        )

    print(f"[Filters] Subject water proximity: {subject_water}"
          f"{' — dropping on-water comps' if drop_on_water else ''}")
    if required_features:
        print(f"[Filters] Subject requires: {', '.join(required_features)}")

    before_filters = len(candidates_raw)
    candidates_raw, filter_report = comp_filters.apply_comp_filters(
        candidates_raw, drop_on_water=drop_on_water,
        required_features=required_features,
    )
    for line in filter_report.lines():
        print(f"[Filters] {line}")

    # Keyword exclusions the operator asked for by hand.
    if args.exclude:
        terms = [t.strip().lower() for t in args.exclude.split(",") if t.strip()]
        if terms:
            kept = []
            for c in candidates_raw:
                nm = ((c.get("listing_info") or {}).get("listing_name") or "").lower()
                if any(t in nm for t in terms):
                    print(f"[Filters] Excluded by keyword: {nm[:50]}")
                    continue
                kept.append(c)
            candidates_raw = kept

    # A filter that empties the pool is worse than no filter. Back off rather
    # than fail the run, and say so out loud.
    if len(candidates_raw) < 6 and before_filters >= 6:
        print(f"[Filters] Only {len(candidates_raw)} comps survived filtering "
              f"(from {before_filters}). Relaxing filters to keep the report usable.")
        candidates_raw, _ = comp_filters.apply_comp_filters(
            _unfiltered_candidates, drop_on_water=False, required_features=[],
        )

    mapped = map_batch_for_scorer(candidates_raw)
    subject_for_scoring = subject_for_scorer(prop, estimate_data if not args.skip_financials else {})
    # Over-select so we have replacement candidates for any dead Airbnb listings
    result = rank_comps(subject_for_scoring, mapped, top_n=12)

    # Drop any selected comp whose Airbnb URL is gone (HTTP 4xx/5xx).
    # Walk the ranked list and pick the first 6 with live URLs.
    if not args.skip_financials and (result["selected"] or result["ranked"] or result["hard_fails"]):
        # Liveness is the slowest step in the pipeline and Airbnb throttles it.
        # Probe the top 6 first; only reach further down the ranking for as many
        # replacements as we actually need. Previously every ranked comp (20+)
        # was probed to fill 6 slots.
        ranked = result["ranked"]
        live_selected = []
        checked = 0
        probes = 0
        while len(live_selected) < 6 and checked < len(ranked):
            need = 6 - len(live_selected)
            batch = ranked[checked:checked + need]
            batch_urls = [_airbnb_id_to_url(c) for c in batch]
            batch_live = await _check_liveness(batch_urls)
            probes += len(batch)
            for cand, url, is_live in zip(batch, batch_urls, batch_live):
                if is_live:
                    live_selected.append(cand)
                else:
                    print(f"[Scorer] Dropping dead listing: {cand.get('name', '?')[:40]} ({url})")
            checked += len(batch)
        print(f"[Scorer] Liveness: {probes} probe(s) to fill {len(live_selected)} slot(s)")

        # Rescue pass — when strict scoring + dead-listing filter leaves <6,
        # dip into the hard-failed pool and pick the closest matches to the
        # subject.
        if len(live_selected) < 6:
            need = 6 - len(live_selected)
            print(f"[Scorer] Rescue pass — need {need} more comp(s) from disqualified pool")

            subj_beds = subject_for_scoring.get("bedrooms") or 0
            subj_sleeps = subject_for_scoring.get("max_guests") or subject_for_scoring.get("guests") or 0
            subj_adr = subject_for_scoring.get("adr") or 0

            def _closeness(c: dict) -> float:
                """Lower = closer to subject."""
                comp_beds = c.get("bedrooms") or 0
                comp_sleeps = c.get("sleeps") or c.get("accommodates") or c.get("max_guests") or 0
                comp_adr = c.get("adr_raw") or c.get("adr") or 0
                bed_diff = abs(comp_beds - subj_beds)
                sleep_diff = abs(comp_sleeps - subj_sleeps) / max(1, subj_sleeps)
                adr_diff = abs(comp_adr - subj_adr) / max(1, subj_adr)
                return (bed_diff * 2.0) + sleep_diff + adr_diff

            already_picked_ids = set()
            for c in live_selected:
                li = c.get("listing_info") or {}
                already_picked_ids.add(str(li.get("listing_id") or c.get("airbnbId") or ""))

            rescue_candidates = []
            for raw in result.get("hard_fails", []):
                li = raw.get("listing_info") or {}
                aid = str(li.get("listing_id") or raw.get("airbnbId") or "")
                if not aid or aid in already_picked_ids:
                    continue
                rescue_candidates.append(raw)

            rescue_candidates.sort(key=_closeness)

            rescue_urls = [_airbnb_id_to_url(c) for c in rescue_candidates]
            rescue_liveness = await _check_liveness(rescue_urls)

            bed_tol = max_bed_diff(subj_beds or 0)
            guest_tol = max_guest_diff(subj_sleeps or 0)
            for cand, url, is_live in zip(rescue_candidates, rescue_urls, rescue_liveness):
                if len(live_selected) >= 6:
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

        result["selected"] = live_selected

    print(f"[Scorer] Candidates: {len(mapped)} / Hard fails: {len(result['hard_fails'])} / Passing: {len(result['ranked'])}")
    print(f"[Scorer] Score range: {result['score_range']}")

    # ── Widening pass: if <6 comps, search wider area with relaxed bedrooms ──
    if not args.skip_financials and len(result["selected"]) < 6:
        need = 6 - len(result["selected"])
        print(f"\n[Widening] Only {len(result['selected'])} comps — need {need} more. Searching wider area...")

        airroi_currency = "native" if prop.currency == "CA$" else "usd"
        already_ids = set()
        for c in result["selected"]:
            li = c.get("listing_info") or {}
            already_ids.add(str(li.get("listing_id") or ""))
        # Also exclude all candidates we already scored (they failed for a reason)
        for c in mapped:
            li = c.get("listing_info") or {}
            already_ids.add(str(li.get("listing_id") or ""))

        # Never re-admit the subject's own listing through the widening pass.
        if subject_airbnb_id:
            already_ids.add(subject_airbnb_id)

        # Widen only as far as the scorer will actually accept. Querying ±2 for
        # a 4BR subject returns 0/25 admissible comps: two wasted API calls
        # whose ids then occupy `already_ids` slots.
        tol = max_bed_diff(prop.bedrooms)
        wider_candidates = []
        bed_deltas = [d for d in range(-tol, tol + 1) if d and prop.bedrooms + d >= 1]
        bed_deltas.sort(key=abs)

        has_coords = (
            getattr(prop, "latitude", None) is not None
            and getattr(prop, "longitude", None) is not None
        )

        async def _fetch_wider(beds_delta: int) -> list[dict]:
            try:
                beds_x = prop.bedrooms + beds_delta
                guests_x = max(2, prop.max_guests + int(beds_delta * 2.5))
                if has_coords:
                    return await get_comparables(
                        latitude=float(prop.latitude), longitude=float(prop.longitude),
                        bedrooms=beds_x, baths=prop.bathrooms, guests=guests_x,
                        currency=airroi_currency,
                    )
                else:
                    return await get_comparables(
                        address=prop.address,
                        bedrooms=beds_x, baths=prop.bathrooms, guests=guests_x,
                        currency=airroi_currency,
                    )
            except Exception as e:
                print(f"[Widening] {prop.bedrooms + beds_delta}BR query failed: {e}", file=sys.stderr)
                return []

        wider_batches = await asyncio.gather(*[_fetch_wider(d) for d in bed_deltas])
        for batch in wider_batches:
            for c in batch:
                li = c.get("listing_info") or {}
                lid = str(li.get("listing_id") or "")
                if not lid or lid in already_ids:
                    continue
                if subject_airbnb_id and lid == subject_airbnb_id:
                    continue
                already_ids.add(lid)
                wider_candidates.append(c)

        if wider_candidates:
            print(f"[Widening] Found {len(wider_candidates)} new candidates from adjacent bedroom counts")
            wider_mapped = map_batch_for_scorer(wider_candidates)
            wider_result = rank_comps(subject_for_scoring, wider_mapped, top_n=need * 2)

            # Check liveness + fill remaining slots
            wider_ranked = wider_result.get("ranked") or []
            if wider_ranked:
                wider_urls = [_airbnb_id_to_url(c) for c in wider_ranked]
                wider_liveness = await _check_liveness(wider_urls)
                for cand, url, is_live in zip(wider_ranked, wider_urls, wider_liveness):
                    if len(result["selected"]) >= 6:
                        break
                    if is_live:
                        print(f"[Widening] Added: {cand.get('name', '?')[:40]} ({cand.get('bedrooms')}BR)")
                        result["selected"].append(cand)
                    else:
                        print(f"[Widening] Dead listing skipped: {cand.get('name', '?')[:35]}")

        if len(result["selected"]) < 6:
            print(f"[Widening] Still only {len(result['selected'])} comps after widening — proceeding with what we have")

    comps = [to_comp_property(c) for c in result["selected"]]

    print(f"[Scorer] Selected {len(comps)} comps:")
    for i, c in enumerate(comps, 1):
        print(f"         #{i} {c.bedrooms}BR sleeps {c.sleeps} / {prop.currency}{c.annual_revenue:,.0f}/yr / {c.occupancy_pct:.0f}% / {c.name[:40]}")

    # Step 6: Sanity checks — Phase A (blocking, pre-render)
    print("\n--- Step 6: Sanity Phase A (pre-render, blocking) ---")
    report_data = ReportData(property=prop, rentalizer=rentalizer, comps=comps)
    slug = prop.market.replace(" ", "-") if prop.market else "report"
    phase_a_failures = await run_phase_a(report_data)
    if phase_a_failures:
        write_failure_report(
            "A", phase_a_failures, config.OUTPUT_DIR, subject_slug=slug,
        )
        print("\n[SANITY] Phase A blocking — NOT generating report.")
        sys.exit(2)
    print("[Sanity] Phase A passed — all 6 comps complete, hero images live, math sane.")

    # Step 7: Calculator defaults
    print("\n--- Step 7: Deriving calculator defaults ---")
    # Anchor occupancy to the WHOLE market pool, not the six comps that made
    # the report. Those six are picked for quality and sit near the market's
    # 81st percentile; anchoring to them over-projected by +47% across 125
    # backtested listings. `mapped` is every candidate the market query
    # returned, before selection. Exclude the subject's own listing so a
    # strong subject cannot inflate its own market baseline.
    pool_occupancies = []
    for cand in mapped:
        li = cand.get("listing_info") or {}
        if subject_airbnb_id and str(li.get("listing_id") or "") == subject_airbnb_id:
            continue
        occ = cand.get("occupancy_pct")
        if occ is not None and occ > 0:
            pool_occupancies.append(float(occ))

    calculator = derive_calculator_defaults(
        comps, rentalizer, prop, pool_occupancies=pool_occupancies,
    )
    _basis_label = {
        "subject":     "the subject's own trailing 12 months",
        "market_pool": f"market pool median of {len(pool_occupancies)} listings",
        "comp_set":    "comp-set median (no pool or subject history available)",
    }
    print(f"[Calculator] Occ: {calculator.occ_min}-{calculator.occ_max}% (default {calculator.occ_default}%)")
    print(f"[Calculator]   occupancy basis: {_basis_label.get(calculator.occ_basis, calculator.occ_basis)}")
    print(f"[Calculator] ADR: {prop.currency}{calculator.adr_min:,} - {prop.currency}{calculator.adr_max:,} (default {prop.currency}{calculator.adr_default:,})")
    print(f"[Calculator]   rate basis: {_basis_label.get(calculator.adr_basis, calculator.adr_basis)}")

    # Step 8: Seasonal data — pull per-comp monthly metrics from AirROI
    print("\n--- Step 8: Seasonal occupancy from market data ---")

    # COST GATE: /listings/metrics/all is $0.10 per comp ($0.60 for six), and
    # derive_seasonal_data prioritises Airbtics over these. Fetching them first
    # and discarding them was 60% of the AirROI bill on every market Airbtics
    # covers. Try Airbtics first; only pay for per-comp metrics if it came up short.
    airbtics_metrics = (overlay or {}).get("metrics") if overlay else None
    seasonal_data, seasonal_basis = derive_seasonal_data_with_basis(
        rentalizer, comp_monthly_data=None, airbtics_metrics=airbtics_metrics)

    # ONLY an Airbtics-supplied curve justifies skipping the per-comp calls.
    # A "subject" curve is one property's own history, which is not a market
    # seasonality signal, and skipping on it both mislabels the source and
    # suppresses the fallback that would have produced a real one.
    if seasonal_data and seasonal_basis != "airbtics":
        print(f"[Seasonal] Curve came from '{seasonal_basis}', not Airbtics — "
              "still fetching per-comp metrics for a real market curve.")
        seasonal_data = []
    elif seasonal_data:
        print("[Seasonal] Airbtics covered this market — skipping "
              f"{len(result['selected'])} per-comp metric calls (saved ~${0.10 * len(result['selected']):.2f})")

    # No Airbtics curve. Before paying $0.60 for six per-comp metric calls, buy
    # the whole-market curve for $0.11 (/markets/lookup $0.01 + occupancy $0.10).
    # It is cheaper AND better sourced: the six comps are selected for quality
    # and run above the market, which is the bias the occupancy anchor removes
    # from the headline. Verified live on Gatlinburg: 12 monthly rows with
    # p25-p90. Needs coordinates; falls through silently to the per-comp path.
    market_occ: list[dict] = []
    if (not seasonal_data and not args.skip_financials
            and getattr(prop, "latitude", None) is not None
            and getattr(prop, "longitude", None) is not None):
        try:
            mkt = await lookup_market(float(prop.latitude), float(prop.longitude))
            market_occ = await get_market_occupancy(mkt)
            covered = sum(1 for r in market_occ if isinstance(r, dict) and r.get("avg") is not None)
            where = mkt.get("locality") or mkt.get("region") or "market"
            print(f"[Seasonal] AirROI market curve for {where}: {covered}/12 months")
            seasonal_data, seasonal_basis = derive_seasonal_data_with_basis(
                rentalizer, comp_monthly_data=None,
                airbtics_metrics=airbtics_metrics, market_occupancy=market_occ)
            if seasonal_data and seasonal_basis == "market":
                n = len(result["selected"])
                print(f"[Seasonal] Market curve used — skipping {n} per-comp metric "
                      f"calls (${0.10 * n:.2f} -> $0.11)")
            else:
                seasonal_data = []
                print("[Seasonal] Market curve too thin — falling back to per-comp metrics")
        except (AirROIError, httpx.HTTPError, asyncio.TimeoutError) as e:
            print(f"[Seasonal] Market curve unavailable ({type(e).__name__}) — "
                  "falling back to per-comp metrics", file=sys.stderr)
            seasonal_data = []

    comp_monthly_data: list[list[float | None]] = []
    if not seasonal_data and not args.skip_financials and result["selected"]:
        airroi_currency = "native" if prop.currency == "CA$" else "usd"

        async def _fetch_comp_monthly(c: dict) -> list[float | None]:
            """Fetch per-listing monthly occupancy from AirROI metrics endpoint."""
            try:
                li = c.get("listing_info") or {}
                listing_id = li.get("listing_id") or c.get("airbnbId") or c.get("id")
                if not listing_id:
                    return [None] * 12
                metrics = await get_listing_metrics(
                    listing_id=int(listing_id),
                    num_months=12,
                    currency=airroi_currency,
                )
                monthly: list[float | None] = [None] * 12
                for entry in metrics:
                    if not isinstance(entry, dict):
                        continue
                    date_str = entry.get("date") or ""
                    try:
                        mo = int(str(date_str).split("-")[1]) - 1
                        if 0 <= mo <= 11:
                            occ_data = entry.get("occupancy") or {}
                            if isinstance(occ_data, dict):
                                occ_val = occ_data.get("avg")
                                if occ_val is not None:
                                    # AirROI returns 0-1; convert to percent
                                    occ_pct = float(occ_val) * 100 if float(occ_val) <= 1 else float(occ_val)
                                    monthly[mo] = occ_pct
                    except (ValueError, IndexError, AttributeError):
                        pass
                return monthly
            except Exception as e:
                print(f"[Seasonal] Comp monthly fetch failed: {e}", file=sys.stderr)
                return [None] * 12

        # Fetch all comps in parallel
        comp_monthly_data = await asyncio.gather(
            *[_fetch_comp_monthly(c) for c in result["selected"]]
        )

    if not seasonal_data:
        seasonal_data = derive_seasonal_data(
            rentalizer,
            comp_monthly_data=comp_monthly_data,
            airbtics_metrics=airbtics_metrics,
        )

    # Fallback: derive seasonal occupancy from AirROI's monthly revenue distributions
    if not seasonal_data and not args.skip_financials:
        distributions = estimate_data.get("monthly_revenue_distributions") or []
        if isinstance(distributions, list) and len(distributions) == 12:
            annual_occ = rentalizer.occupancy_pct  # e.g., 46%
            # Revenue ratios are proportional to occupancy × ADR. Assume ADR
            # is roughly constant month-to-month, so ratios approximate
            # relative occupancy. Scale so the average equals annual_occ.
            avg_ratio = sum(distributions) / 12  # should be ~0.0833
            if avg_ratio > 0:
                seasonal_data = [
                    min(round((ratio / avg_ratio) * annual_occ, 1), 95.0)
                    for ratio in distributions
                ]
                print("[Seasonal] Derived from AirROI monthly revenue distributions")

    if not seasonal_data:
        print("[Seasonal] ERROR: no usable monthly data from Airbtics or AirROI.")
        print("[Seasonal] Cannot deliver report without real seasonal data — exiting.")
        sys.exit(2)

    source = ("Airbtics" if airbtics_metrics
              else "AirROI per-comp average" if comp_monthly_data and any(any(v is not None for v in c) for c in comp_monthly_data)
              else "AirROI revenue distribution")
    print(f"[Seasonal] Source: {source}")
    print(f"[Seasonal] Monthly occ: {[int(v) for v in seasonal_data]}")

    # Derive the season labels from THIS market's actual revenue distribution
    # rather than the hardcoded BC-ski calendar. Must happen BEFORE narratives
    # are generated, or the prose falls back to month-free text while the
    # header shows the real season.
    peak_label, shoulder_label = derive_season_labels(
        (estimate_data or {}).get("monthly_revenue_distributions"),
        seasonal_data,
    )
    if peak_label:
        print(f"[Seasonal] {peak_label}")

    # Step 9: Narratives
    print("\n--- Step 9: Generating narrative content ---")
    narratives = await generate_narratives(
        prop, rentalizer, comps, calculator,
        peak_season_label=peak_label,
        shoulder_season_label=shoulder_label,
        monthly_distribution=(estimate_data or {}).get("monthly_revenue_distributions"),
        seasonal_data=seasonal_data,
        narratives_file=args.narratives,
        output_dir=config.OUTPUT_DIR,
        input_ref=args.input,
    )
    narratives.peak_season_label = peak_label
    narratives.shoulder_season_label = shoulder_label
    print(f"[Narratives] Positioning: {narratives.positioning_summary[:80]}...")

    # Step 10: Methodology + final report
    methodology = build_methodology(
        prop, comps, peak_label, shoulder_label, calculator=calculator,
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
        seasonal_data=seasonal_data,
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
        sys.exit(1)

    # Cache the assembled pipeline output so the copy can be improved later
    # without paying for the data again. This is what makes the Claude Code
    # narrative loop free instead of a second full run.
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data_path = report_data_path(config.OUTPUT_DIR, prop)
    data_path.write_text(report_data.model_dump_json(indent=1))
    print(f"[Data] Pipeline output cached: {data_path.name}")

    output_path = await _render_and_gate(report_data, slug)

    # Email
    if args.email:
        print(f"\n--- Emailing report to {args.email} ---")
        try:
            send_report_email(args.email, prop.short_address, output_path)
        except Exception as e:
            print(f"[Email] Failed: {e}")
            print("[Email] Report was still saved locally.")
    else:
        print("\n--- Email skipped (no --email provided) ---")

    print()
    print("=" * 62)
    print("                     Report Complete!")
    print("=" * 62)
    print(f"\n  File: {output_path.resolve()}\n")


if __name__ == "__main__":
    asyncio.run(main())
