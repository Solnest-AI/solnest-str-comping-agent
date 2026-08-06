#!/usr/bin/env python3
"""Solnest Report Agent — CLI orchestrator (AirROI edition).

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
from schema import PropertyBasics, ReportData, RevenueEstimate
from scrapers.airbnb import scrape_airbnb_listing
from scrapers.airroi import run_airroi_pipeline, get_listing, get_listing_metrics, get_comparables, AirROIError
from scrapers.airbtics import get_market_overlay
from scrapers.property_search import scrape_listing_url, search_for_property, search_hero_image
from adapters.airroi_to_comp import (
    map_batch_for_scorer, to_comp_property, subject_for_scorer,
)
from comp_scorer import rank_comps
from generators.calculator import (
    derive_calculator_defaults, derive_revenue_projection, derive_seasonal_data,
    align_calculator_to_base_case,
)
from generators.narratives import generate_narratives
from generators.methodology import build_methodology
from validators.sanity import (
    run_phase_a, run_phase_b, write_failure_report,
)
from report.template_engine import save_report
from report.email_sender import send_report_email


# ── Water-proximity classifier ──────────────────────────────────────
# Detects whether a listing is on the water (oceanfront/beachfront),
# near the water (beach access / short walk), or inland — using
# listing name, description, and amenity flags from AirROI.

_ON_WATER_PHRASES = (
    "oceanfront", "ocean front", "beachfront", "beach front",
    "on the sand", "on the beach", "right on the ocean", "right on the beach",
    "toes in the sand", "toes on the sand",
    "steps to the sand", "steps to sand", "steps to the beach", "steps to beach",
    "beach at your doorstep", "beach at the doorstep",
    "private beach", "direct beach", "direct ocean", "ocean's edge", "oceans edge",
    "waterfront", "water's edge", "waters edge",
    "lakefront", "lake front", "on the lake", "on the water",
)
_NEAR_WATER_PHRASES = (
    "beach access", "lake access", "ocean access", "walk to the beach",
    "walk to beach", "short walk to the beach", "minutes to the beach",
    "close to the beach", "near the beach", "steps from the beach",
)


def classify_water_proximity(name: str, description: str, amenities: list) -> str:
    """Return 'on_water', 'near_water', or 'inland' from listing data."""
    text = f"{name or ''} {description or ''}".lower()
    amen = {str(a).lower() for a in (amenities or [])}

    if any(p in text for p in _ON_WATER_PHRASES):
        return "on_water"
    if "waterfront" in amen:
        return "on_water"
    if any(p in text for p in _NEAR_WATER_PHRASES):
        return "near_water"
    if "beach_access" in amen or "lake_access" in amen:
        return "near_water"
    return "inland"


def classify_comp_water_proximity(comp: dict) -> str:
    """Same classifier but reads from AirROI's nested comp structure."""
    li = comp.get("listing_info") or {}
    pd = comp.get("property_details") or {}
    return classify_water_proximity(
        li.get("listing_name") or "",
        li.get("description") or "",
        pd.get("amenities") or [],
    )


# ── Must-have feature detector ────────────────────────────────────
# When subject has a high-impact amenity (pool, hot tub), comps without
# that amenity skew the projection. This drops them before scoring.

_FEATURE_TEXT_KEYWORDS = {
    "pool": ("pool", "swimming pool", "private pool", "heated pool", "plunge pool"),
    "hot_tub": ("hot tub", "hottub", "hot-tub", "jacuzzi", "jetted tub"),
}
_FEATURE_AMENITY_KEYS = {
    "pool": ("pool",),
    "hot_tub": ("hot_tub",),
}


def has_feature(name: str, description: str, amenities: list, feature: str) -> bool:
    """Detect whether a listing has a feature (pool, hot_tub) using
    name + description text and AirROI amenity flags."""
    text = f"{name or ''} {description or ''}".lower()
    if any(kw in text for kw in _FEATURE_TEXT_KEYWORDS.get(feature, ())):
        return True
    amen = {str(a).lower() for a in (amenities or [])}
    if any(k in amen for k in _FEATURE_AMENITY_KEYS.get(feature, ())):
        return True
    return False


def comp_has_feature(comp: dict, feature: str) -> bool:
    """Same detector but reads from AirROI's nested comp structure."""
    li = comp.get("listing_info") or {}
    pd = comp.get("property_details") or {}
    return has_feature(
        li.get("listing_name") or "",
        li.get("description") or "",
        pd.get("amenities") or [],
        feature,
    )


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
# Full province/territory names — AirROI returns these instead of abbreviations
_CA_PROVINCE_NAMES = {
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "newfoundland", "nova scotia",
    "northwest territories", "nunavut", "ontario",
    "prince edward island", "quebec", "saskatchewan", "yukon",
}


# ── Currency detection ───────────────────────────────────────────────

def _detect_currency(address: str) -> str:
    """Detect USD vs CAD from address text. Returns "$" or "CA$".

    Checks for US state or Canadian province abbreviations AND full names
    (AirROI returns "British Columbia" not "BC" for Airbnb listings).
    Falls back to "$" (USD) as AirROI defaults to USD.
    """
    if not address:
        return "$"
    # Check full province names first (case-insensitive)
    addr_lower = address.lower()
    if any(prov in addr_lower for prov in _CA_PROVINCE_NAMES):
        return "CA$"
    if "canada" in addr_lower:
        return "CA$"
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

def _ask_int(prompt: str, default: int = 0) -> int:
    """Prompt for a whole number, retrying on invalid input. Empty input → default."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def _ask_float(prompt: str, default: float = 0.0) -> float:
    """Prompt for a number, retrying on invalid input. Empty input → default."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            print("  Please enter a number.")


def _prompt_missing_details(prop: PropertyBasics) -> PropertyBasics:
    """Interactively ask for missing critical property details."""
    if prop.bedrooms <= 0:
        prop.bedrooms = _ask_int("  Number of bedrooms: ")
    if prop.bathrooms <= 0:
        prop.bathrooms = _ask_float("  Number of bathrooms: ")
    if prop.max_guests <= 0:
        prop.max_guests = _ask_int("  Max guests: ")
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
                location_text = ", ".join(filter(None, [
                    loc.get("locality"), loc.get("region"),
                ]))

                # AirROI structured data wins over CLI args (CLI is fallback only)
                airroi_beds = int(pd.get("bedrooms") or 0)
                airroi_baths = float(pd.get("baths") or 0)
                airroi_guests = int(pd.get("guests") or 0)

                prop = PropertyBasics(
                    address=location_text or raw,
                    short_address=title or location_text or raw,
                    market=args.market or loc.get("locality") or "Unknown Market",
                    bedrooms=airroi_beds or args.beds,
                    bathrooms=airroi_baths or args.baths,
                    max_guests=airroi_guests or args.guests,
                    property_type=li.get("listing_type") or "Property",
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

                print(f"[AirROI] Found: {prop.title or prop.short_address}")
                print(f"         {prop.bedrooms}BR / {prop.bathrooms}BA / Sleeps {prop.max_guests}")
                print(f"         Rating: {prop.rating} ({prop.review_count} reviews) / Superhost: {prop.is_superhost}")
                print(f"         Amenities: {len(prop.amenities)}")

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
            # CLI args fill gaps only — scraped data wins
            if not prop.bedrooms and args.beds:
                prop.bedrooms = args.beds
            if not prop.bathrooms and args.baths:
                prop.bathrooms = args.baths
            if not prop.max_guests and args.guests:
                prop.max_guests = args.guests
            if (not prop.market or prop.market == "Unknown Market") and args.market:
                prop.market = args.market

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

        # Validate scraped address against the original input — Firecrawl can
        # hallucinate placeholder data (e.g. "123 Main St, Anytown, USA").
        # Skip validation when input is a URL — the scraped address IS the
        # truth; comparing it against "zillow.com/homedetails/..." is meaningless.
        scraped_addr = listing_data.get("raw_address") or ""
        scrape_trusted = True
        if not is_url and scraped_addr:
            # Extract the street number from the input (first numeric token)
            raw_street_num = ""
            for t in raw.split():
                if re.match(r"\d+", t.strip("., ")):
                    raw_street_num = t.strip("., ")
                    break
            # The scraped address must contain the same street number.
            # City/state overlap alone is not enough — "Prince George" matching
            # doesn't mean "1299 Alward" == "5924 Highway".
            if raw_street_num and raw_street_num not in scraped_addr:
                print(f"[Search] WARNING: Scraped address '{scraped_addr}' doesn't match input '{raw}'")
                print("[Search] Using original address for AirROI queries")
                scraped_addr = ""
                scrape_trusted = False
            elif not raw_street_num:
                # No street number in input — fall back to token overlap
                raw_tokens = {t.strip("., ").lower() for t in raw.split() if len(t.strip("., ")) >= 3}
                scraped_tokens = {t.strip("., ").lower() for t in scraped_addr.split() if len(t.strip("., ")) >= 3}
                if len(raw_tokens & scraped_tokens) < 3:
                    print(f"[Search] WARNING: Scraped address '{scraped_addr}' doesn't match input '{raw}'")
                    print("[Search] Using original address for AirROI queries")
                    scraped_addr = ""
                    scrape_trusted = False

        # Priority logic for bed/bath/guests:
        # - If user provided CLI args AND scrape found data → CLI wins
        #   (user knows their property; scrape could be stale/wrong listing)
        # - If user didn't provide CLI args → scraped data wins
        # - If scrape has no data → CLI args fill the gap
        scraped_beds = int(listing_data.get("bedrooms") or 0) if scrape_trusted else 0
        scraped_baths = float(listing_data.get("bathrooms") or 0) if scrape_trusted else 0
        scraped_guests = int(listing_data.get("max_guests") or 0) if scrape_trusted else 0

        if args.beds and scraped_beds and args.beds != scraped_beds:
            print(f"[Search] NOTE: Using --beds {args.beds} (scraped data showed {scraped_beds}BR)")
            final_beds = args.beds
        else:
            final_beds = scraped_beds or args.beds

        if args.baths and scraped_baths and args.baths != scraped_baths:
            print(f"[Search] NOTE: Using --baths {args.baths} (scraped data showed {scraped_baths}BA)")
            final_baths = args.baths
        else:
            final_baths = scraped_baths or args.baths

        # Guest capacity: CLI wins if provided; otherwise use scraped
        # (but reject scraped guests <= bedrooms as bad data)
        if args.guests:
            final_guests = args.guests
        elif scraped_guests and final_beds and scraped_guests <= final_beds:
            final_guests = 0  # bad data, will be inferred later
        else:
            final_guests = scraped_guests

        prop = PropertyBasics(
            address=scraped_addr or raw,
            short_address=scraped_addr or raw,
            market=(listing_data.get("market") if scraped_addr else None) or args.market or "Unknown Market",
            bedrooms=final_beds,
            bathrooms=final_baths,
            max_guests=final_guests,
            property_type=(listing_data.get("property_type") if scrape_trusted else None) or "Property",
            hero_image_url=(listing_data.get("hero_image_url") if scrape_trusted else "") or "",
            listing_url=listing_data.get("listing_url"),  # always keep — URL is valid even if extracted data was bad
            description=listing_data.get("description") if scrape_trusted else None,
            title=listing_data.get("title") if scrape_trusted else None,
            sqft=int(listing_data.get("sqft")) if (scrape_trusted and listing_data.get("sqft")) else None,
        )
        # Infer guest capacity from bedrooms if not found
        if prop.max_guests <= 0 and prop.bedrooms > 0:
            prop.max_guests = prop.bedrooms * 2 + 2
        # When scrape was untrusted, search for a hero image separately
        if not scrape_trusted and not prop.hero_image_url and not args.hero_url:
            hero_url = await search_hero_image(raw)
            if hero_url:
                prop.hero_image_url = hero_url
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
            listing_url=raw if is_url else None,  # preserve the input URL as listing link
        )

    # If the hero image is missing or a map placeholder, search for a real
    # property photo first. Only fall back to Google Street View as last resort.
    _map_domains = ("api.mapbox.com", "maps.googleapis.com/maps/api/staticmap",
                    "maps.googleapis.com/maps/api/streetview")
    _hero_is_map = prop.hero_image_url and any(d in prop.hero_image_url for d in _map_domains)
    _hero_missing = not prop.hero_image_url

    if (_hero_is_map or _hero_missing) and not args.hero_url:
        # Priority 1: Search the web for actual property photos
        web_hero = await search_hero_image(prop.address)
        if web_hero and not any(d in web_hero for d in _map_domains):
            print("[Search] Found property photo from web search")
            prop.hero_image_url = web_hero
        else:
            # Priority 2: Google Street View (needs API key)
            from scrapers.property_search import get_street_view_url
            sv_url = get_street_view_url(prop.address)
            if sv_url:
                print("[Search] Using Google Street View as fallback hero")
                prop.hero_image_url = sv_url
            else:
                # No photo found at all — report will use placeholder
                prop.hero_image_url = ""
                print("\n[Search] No property photo found.")
                print("         To add one, re-run with: --hero-url \"<photo URL>\"")
                print()

    # Manual overrides from CLI flags (always take precedence)
    if args.hero_url:
        prop.hero_image_url = args.hero_url
    if args.listing_url:
        prop.listing_url = args.listing_url

    if (prop.bedrooms <= 0 or prop.bathrooms <= 0 or prop.max_guests <= 0
            or not prop.market or prop.market == "Unknown Market"):
        if not sys.stdin.isatty():
            missing = []
            if prop.bedrooms <= 0:
                missing.append("--beds")
            if prop.bathrooms <= 0:
                missing.append("--baths")
            if prop.max_guests <= 0:
                missing.append("--guests")
            if not prop.market or prop.market == "Unknown Market":
                missing.append("--market")
            print(f"\n[Input] Missing required fields: {', '.join(missing)}. "
                  f"Pass them as CLI args (non-TTY environment).", file=sys.stderr)
            sys.exit(1)
        print("\n[Input] Missing property details. Please provide:")
        prop = _prompt_missing_details(prop)

    return prop


# ── AirROI estimate -> RevenueEstimate ────────────────────────────────

def _build_revenue_estimate(estimate_data: dict, prop: PropertyBasics) -> RevenueEstimate:
    """Construct a RevenueEstimate from AirROI's calculator/estimate response."""
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

    return RevenueEstimate(
        revenue_potential=round(rev_potential, 2),
        adr=round(adr, 2),
        occupancy_pct=round(occ, 2),
        monthly_occupancy=monthly_occ,
        monthly_revenue=monthly_rev,
    )


# ── Main orchestration ────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{config.BRANDING.get('company_name', 'STR Comping Agent')} STR Income Analysis Report Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent.py --input "https://www.airbnb.ca/rooms/39508095"
  python agent.py --input "5005 Valley Drive Unit 13, Sun Peaks BC"
  python agent.py --input "<airbnb_url>" --email buyer@example.com
        """,
    )
    parser.add_argument("--input", required=True,
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
    parser.add_argument("--skip-financials", action="store_true",
                        help="Skip AirROI (dev/testing only — produces an empty revenue_estimate)")
    parser.add_argument("--hero-url", default=None,
                        help="Manual subject hero image URL (use when MLS scrape is blocked)")
    parser.add_argument("--listing-url", default=None,
                        help="Manual realtor.ca listing URL (use when MLS search fails)")
    parser.add_argument("--currency", default=None, choices=["$", "CA$"],
                        help="Override currency (auto-detected from address: $ for US, CA$ for Canada)")
    parser.add_argument("--exclude", "--exclude-comps", default=None,
                        help="Comma-separated keywords to exclude from comp listing names (e.g. 'oceanfront,beachfront'). "
                             "Also accepts a full listing name to drop one specific oversized comp")
    parser.add_argument("--subject-on-water", action="store_true",
                        help="Declare the subject is on the ocean/lake (skips auto water-proximity filter)")
    parser.add_argument("--allow-oceanfront-comps", action="store_true",
                        help="Disable auto water-proximity filter — include oceanfront/beachfront comps even when subject is inland")
    parser.add_argument("--require", default=None,
                        help="Comma-separated must-have features (e.g. 'pool,hot_tub') — drops comps missing any of these. Auto-detected from subject if omitted.")
    parser.add_argument("--no-feature-filter", action="store_true",
                        help="Disable auto must-have feature filter (pool, hot tub)")
    parser.add_argument("--radius", type=int, default=None,
                        help="AirROI comp search radius in miles (default: 5 for 5+BR, none otherwise)")

    args = parser.parse_args()

    print()
    print("=" * 62)
    print(f"  {config.BRANDING['company_name']} - STR Income Analysis Agent")
    print("=" * 62)
    print()

    # Clean previous run data — staging dir and old sanity failure logs
    staging_dir = config.OUTPUT_DIR / ".staging"
    if staging_dir.exists():
        for f in staging_dir.iterdir():
            f.unlink(missing_ok=True)
    for f in config.OUTPUT_DIR.glob("*.sanity-failed-phase-*.json"):
        f.unlink(missing_ok=True)

    # Step 1: Resolve subject
    print("--- Step 1/10: Resolving subject property ---")
    prop = await _resolve_subject(args)

    # Currency: CLI override > auto-detect from address
    if args.currency:
        prop.currency = args.currency
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
        revenue_estimate = RevenueEstimate(revenue_potential=0, adr=0, occupancy_pct=0)
    else:
        # Map internal currency to AirROI param
        airroi_currency = "native" if prop.currency == "CA$" else "usd"

        print("\n--- Step 2: Calling AirROI (estimate + comparables) ---")
        try:
            estimate_data, candidates_raw = await run_airroi_pipeline(
                prop, currency=airroi_currency, radius_override=args.radius,
            )
            rev = estimate_data.get("revenue") or 0
            adr = estimate_data.get("average_daily_rate") or 0
            occ = estimate_data.get("occupancy") or 0
            print(f"[AirROI] Estimate: rev ${rev:,.0f} / ADR ${adr:.0f} / Occ {occ:.0%}")
            print(f"[AirROI] Comp candidates: {len(candidates_raw)}")
        except AirROIError as e:
            print(f"\n[AirROI] Pipeline error: {e}")
            print("[AirROI] Check address/coordinates; try specifying --beds/--baths/--guests.")
            sys.exit(1)

        print("\n--- Step 3: Building subject revenue_estimate ---")
        revenue_estimate = _build_revenue_estimate(estimate_data, prop)
        implied_rev = revenue_estimate.adr * 365 * (revenue_estimate.occupancy_pct / 100)
        print(f"[Estimate] ADR: {prop.currency}{revenue_estimate.adr:.0f} / Occ: {revenue_estimate.occupancy_pct:.0f}% / Implied yr rev: {prop.currency}{implied_rev:,.0f}")

    # Step 4: Airbtics overlay (optional)
    print("\n--- Step 4: Airbtics market overlay (optional) ---")
    overlay = await get_market_overlay(prop.market)
    if overlay:
        s = overlay.get("summary") or {}
        print(f"[Airbtics] Market: {overlay['market'].get('name')}")
        print(f"           Market avg occ: {s.get('occupancy')}% / ADR: ${s.get('average_daily_rate')} / {s.get('active_listings_count')} listings")
        metrics = overlay.get("metrics") or []
        if metrics and not revenue_estimate.monthly_occupancy:
            revenue_estimate.monthly_occupancy = [float(m.get("occupancy") or 0) for m in metrics[-12:]]
            revenue_estimate.monthly_revenue = [float(m.get("revenue") or 0) for m in metrics[-12:]]
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
        # Pass 2: Name match
        comp_name_norm = _normalize(li.get("listing_name") or "")
        if (len(subject_name_norm) >= 10 and len(comp_name_norm) >= 10
                and (subject_name_norm in comp_name_norm
                     or comp_name_norm in subject_name_norm)):
            continue
        filtered.append(c)
    candidates_raw = filtered
    if len(candidates_raw) < before:
        print(f"[Scorer] Filtered subject's own listing out of comp pool ({before} -> {len(candidates_raw)})")

    # Water-proximity classification (used by initial filter AND widening pass)
    if args.subject_on_water:
        subject_water = "on_water"
    else:
        subject_water = classify_water_proximity(
            prop.title or "", prop.description or "", prop.amenities or [],
        )
    water_filter_active = (not args.allow_oceanfront_comps) and subject_water == "inland"
    if not args.allow_oceanfront_comps:
        print(f"[WaterProximity] Subject classified as: {subject_water}")

    # Must-have feature detection (used by initial filter AND widening pass)
    if args.no_feature_filter:
        required_features: list[str] = []
    elif args.require:
        required_features = [f.strip().lower() for f in args.require.split(",") if f.strip()]
    else:
        required_features = [
            feat for feat in ("pool", "hot_tub")
            if has_feature(prop.title or "", prop.description or "", prop.amenities or [], feat)
        ]

    def _apply_comp_filters(candidates: list, label: str = "Scorer") -> list:
        """Apply water-proximity + must-have feature filters. Reused by widening."""
        kept = []
        water_dropped: list[str] = []
        feat_dropped: list[tuple[str, list[str]]] = []
        for c in candidates:
            li = c.get("listing_info") or {}
            name = li.get("listing_name") or "(unnamed)"
            if water_filter_active and classify_comp_water_proximity(c) == "on_water":
                water_dropped.append(name)
                continue
            if required_features:
                missing = [f for f in required_features if not comp_has_feature(c, f)]
                if missing:
                    feat_dropped.append((name, missing))
                    continue
            kept.append(c)
        if water_dropped:
            print(f"[{label}/Water] Dropped {len(water_dropped)} on-water comps:")
            for n in water_dropped:
                print(f"    - {n}")
        if feat_dropped:
            print(f"[{label}/Features] Dropped {len(feat_dropped)} comps missing required features:")
            for n, missing in feat_dropped:
                print(f"    - {n}  (missing: {', '.join(missing)})")
        return kept

    if required_features:
        print(f"[Features] Subject requires: {required_features}")
    # Drop private-room / studio listings — must be a whole-property rental
    before_pr = len(candidates_raw)
    candidates_raw = [
        c for c in candidates_raw
        if int((c.get("property_details") or {}).get("bedrooms") or 0) >= 1
        and int((c.get("property_details") or {}).get("guests") or 0) >= 1
    ]
    if len(candidates_raw) < before_pr:
        print(f"[Scorer] Dropped {before_pr - len(candidates_raw)} private-room/studio listings (0BR or 0 guests)")
    candidates_raw = _apply_comp_filters(candidates_raw, label="Scorer")

    if args.exclude:
        keywords = [k.strip().lower() for k in args.exclude.split(",") if k.strip()]
        before_exc = len(candidates_raw)
        kept = []
        for c in candidates_raw:
            name = ((c.get("listing_info") or {}).get("listing_name") or "").lower()
            if any(kw in name for kw in keywords):
                continue
            kept.append(c)
        candidates_raw = kept
        print(f"[Scorer] Excluded {before_exc - len(candidates_raw)} candidates matching: {keywords}")

    mapped = map_batch_for_scorer(candidates_raw)
    subject_for_scoring = subject_for_scorer(prop, estimate_data if not args.skip_financials else {})
    # Over-select so we have replacement candidates for any dead Airbnb listings
    result = rank_comps(subject_for_scoring, mapped, top_n=12)

    # Helpers used by both the liveness check and the widening pass.
    # Hoisted above the conditional so widening still works when filters
    # wipe out result["selected"].
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
                return r.status_code < 300 or r.status_code in (301, 302)
            except Exception:
                return False

        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (airroi-report-agent sanity gate)"},
            timeout=12,
            follow_redirects=False,
        ) as client:
            return await asyncio.gather(*[_head(client, u) for u in urls])

    # Drop any selected comp whose Airbnb URL is gone (HTTP 4xx/5xx).
    # Walk the ranked list and pick the first 6 with live URLs.
    if not args.skip_financials and result["selected"]:

        # Batch-check all ranked comps: Airbnb URL + hero image must both be live
        ranked = result["ranked"]
        ranked_urls = [_airbnb_id_to_url(c) for c in ranked]
        hero_urls = [
            (c.get("listing_info") or {}).get("cover_photo_url") or ""
            for c in ranked
        ]
        listing_liveness = await _check_liveness(ranked_urls)
        hero_liveness = await _check_liveness(hero_urls)

        live_selected = []
        dead_listing_ids: set[str] = set()
        for cand, url, listing_live, hero_live in zip(ranked, ranked_urls, listing_liveness, hero_liveness):
            li = cand.get("listing_info") or {}
            lid = str(li.get("listing_id") or "")
            if not listing_live:
                print(f"[Scorer] Dropping dead listing: {cand.get('name', '?')[:40]} ({url})")
                if lid:
                    dead_listing_ids.add(lid)
                continue
            if not hero_live:
                print(f"[Scorer] Dropping comp with dead hero image: {cand.get('name', '?')[:40]}")
                if lid:
                    dead_listing_ids.add(lid)
                continue
            if len(live_selected) >= 6:
                continue
            live_selected.append(cand)

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

            for cand, url, is_live in zip(rescue_candidates, rescue_urls, rescue_liveness):
                if len(live_selected) >= 6:
                    break
                if is_live:
                    cand_adr = float(cand.get("adr_raw") or cand.get("adr") or 0)
                    cand_days = float(cand.get("days_available") or 365)
                    cand_rev = float(cand.get("annual_revenue") or cand.get("revenue") or 0)
                    if cand_adr > 0 and cand_days > 0 and cand_rev > 0:
                        theoretical_max = cand_adr * cand_days
                        if theoretical_max > 0 and (cand_rev / theoretical_max) < 0.15:
                            print(f"[Scorer] Skipping stale rescue: {cand.get('name','?')[:35]} (rev/max={cand_rev/theoretical_max:.0%})")
                            continue
                    fail_reason = (cand.get("hard_fail_reason") or "")[:60]
                    print(f"[Scorer] Rescued [{cand.get('name','?')[:35]}] (was: {fail_reason})")
                    cand["hard_fail"] = False
                    cand["hard_fail_reason"] = ""
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

        # Strategy: try adjacent bedroom counts (±1, ±2) to widen the pool
        wider_candidates = []
        bed_deltas = [d for d in [-1, 1, -2, 2] if prop.bedrooms + d >= 1]

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
                if lid and lid not in already_ids:
                    already_ids.add(lid)
                    wider_candidates.append(c)

        if wider_candidates:
            print(f"[Widening] Found {len(wider_candidates)} new candidates from adjacent bedroom counts")
            wider_candidates = _apply_comp_filters(wider_candidates, label="Widening")
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

    # Pre-filter: swap out comps with suspicious revenue/ADR ratios before
    # Phase A sanity gate. Only swap if there's a same-or-better bedroom
    # match available — bad revenue data is better than wrong bedroom count.
    if not args.skip_financials and result["selected"]:
        def _revenue_sane(c: dict) -> bool:
            pm = c.get("performance_metrics") or {}
            adr = float(pm.get("ttm_avg_rate") or c.get("adr_raw") or c.get("adr") or 0)
            days = int(pm.get("ttm_available_days") or c.get("days_available") or 365)
            rev = float(pm.get("ttm_revenue") or c.get("annual_revenue_raw") or c.get("annual_revenue") or 0)
            if adr <= 0 or days <= 0 or rev <= 0:
                return True  # let field-check handle missing data
            theoretical_max = adr * days
            if theoretical_max <= 0:
                return True
            ratio = rev / theoretical_max
            # Wider than Phase A sanity (1.6x) because seasonal properties
            # legitimately earn 2-3x their average ADR during peak. Properties
            # with fewer available days but high revenue are seasonal winners,
            # not data errors. Phase A still catches truly broken data.
            return 0.15 <= ratio <= 3.0

        def _comp_beds(c: dict) -> int:
            pd = c.get("property_details") or {}
            return int(pd.get("bedrooms") or c.get("bedrooms") or 0)

        sane = []
        bad_revenue = []  # hold aside — may keep if no same-bed replacement
        for c in result["selected"]:
            if _revenue_sane(c):
                sane.append(c)
            else:
                bad_revenue.append(c)

        # Try to find same-bedroom replacements for bad-revenue comps
        if bad_revenue:
            selected_ids = {str((c.get("listing_info") or {}).get("listing_id") or "") for c in sane}
            skip_ids = selected_ids | (dead_listing_ids if 'dead_listing_ids' in locals() else set())

            for bad_comp in bad_revenue:
                bad_beds = _comp_beds(bad_comp)
                # Look for a replacement with the same bedroom count
                replacement = None
                for cand in result.get("ranked", []):
                    li = cand.get("listing_info") or {}
                    lid = str(li.get("listing_id") or "")
                    if lid in skip_ids:
                        continue
                    if not _revenue_sane(cand):
                        continue
                    cand_beds = _comp_beds(cand)
                    if cand_beds == bad_beds:
                        replacement = cand
                        skip_ids.add(lid)
                        break

                li = bad_comp.get("listing_info") or {}
                name = (li.get("listing_name") or bad_comp.get("name", "?"))[:40]

                if replacement:
                    sane.append(replacement)
                    rli = replacement.get("listing_info") or {}
                    rname = (rli.get("listing_name") or replacement.get("name", "?"))[:40]
                    print(f"[Scorer] Swapping bad revenue comp: {name} → {rname} (same {bad_beds}BR)")
                else:
                    # No same-bed replacement — keep the original despite bad revenue
                    sane.append(bad_comp)
                    print(f"[Scorer] Keeping {name} despite bad revenue (no same-bed replacement)")

        result["selected"] = sane

    comps = [to_comp_property(c) for c in result["selected"]]

    print(f"[Scorer] Selected {len(comps)} comps:")
    for i, c in enumerate(comps, 1):
        print(f"         #{i} {c.bedrooms}BR sleeps {c.sleeps} / {prop.currency}{c.annual_revenue:,.0f}/yr / {c.occupancy_pct:.0f}% / {c.name[:40]}")

    # Step 6: Sanity checks — Phase A (blocking, pre-render)
    print("\n--- Step 6: Sanity Phase A (pre-render, blocking) ---")
    report_data = ReportData(property=prop, revenue_estimate=revenue_estimate, comps=comps)
    slug = prop.market.replace(" ", "-") if prop.market else "report"
    phase_a_failures = await run_phase_a(report_data)
    if phase_a_failures:
        write_failure_report(
            "A", phase_a_failures, config.OUTPUT_DIR, subject_slug=slug,
        )
        print("\n[SANITY] Phase A blocking — NOT generating report.")
        sys.exit(2)
    print("[Sanity] Phase A passed — all 6 comps complete, hero images live, math sane.")

    # Step 7: Calculator defaults + revenue projection
    print("\n--- Step 7: Deriving calculator defaults + revenue projection ---")
    calculator = derive_calculator_defaults(comps, revenue_estimate, prop)
    projection = derive_revenue_projection(comps, revenue_estimate)
    # Align the calculator's default ADR so its opening state reproduces the
    # headline Base Case (median comp revenue). Only the starting ADR moves;
    # the sliders stay fully interactive.
    calculator = align_calculator_to_base_case(calculator, projection.base_case)
    print(f"[Calculator] Occ: {calculator.occ_min}-{calculator.occ_max}% (default {calculator.occ_default}%)")
    print(f"[Calculator] ADR: {prop.currency}{calculator.adr_min:,} - {prop.currency}{calculator.adr_max:,} (default {prop.currency}{calculator.adr_default:,})")
    print(f"[Projection] Conservative: {prop.currency}{projection.conservative:,} / Base: {prop.currency}{projection.base_case:,} / Optimistic: {prop.currency}{projection.optimistic:,}")
    print(f"[Projection] AirROI estimate: {prop.currency}{projection.airroi_estimate:,} (divergence: {projection.airroi_divergence_pct}%{' ⚠️ FLAGGED' if projection.airroi_divergence_flag else ''})")

    # Step 8: Seasonal data — pull per-comp monthly metrics from AirROI
    print("\n--- Step 8: Seasonal occupancy from market data ---")

    comp_monthly_data: list[list[float | None]] = []
    if not args.skip_financials and result["selected"]:
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

    airbtics_metrics = (overlay or {}).get("metrics") if overlay else None
    seasonal_data = derive_seasonal_data(
        revenue_estimate,
        comp_monthly_data=comp_monthly_data,
        airbtics_metrics=airbtics_metrics,
    )

    # Fallback: derive seasonal occupancy from AirROI's monthly revenue distributions
    if not seasonal_data and not args.skip_financials:
        distributions = estimate_data.get("monthly_revenue_distributions") or []
        if isinstance(distributions, list) and len(distributions) == 12:
            annual_occ = revenue_estimate.occupancy_pct  # e.g., 46%
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

    # Step 9: Narratives
    print("\n--- Step 9: Generating narrative content ---")
    narratives = await generate_narratives(prop, revenue_estimate, comps, calculator)
    print(f"[Narratives] Positioning: {narratives.positioning_summary[:80]}...")

    # Step 10: Methodology + final report
    methodology = build_methodology(prop, comps)

    print("\n--- Step 10: Rendering HTML report (to staging) ---")
    report_data = ReportData(
        property=prop,
        revenue_estimate=revenue_estimate,
        projection=projection,
        comps=comps,
        calculator=calculator,
        narratives=narratives,
        methodology=methodology,
        report_date=date.today().strftime("%B %d, %Y"),
        seasonal_data=seasonal_data,
    )

    # Staging-path pattern: render to .staging/, run Phase B, only then
    # move to the final output path. User never sees a broken file.
    staging_dir = config.OUTPUT_DIR / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    staging_path = save_report(report_data, staging_dir)
    print(f"[Staging] Rendered to: {staging_path.resolve()}")

    # Phase B — post-render blocking checks
    print("\n--- Step 10b: Sanity Phase B (post-render, blocking) ---")
    phase_b_failures = await run_phase_b(staging_path)
    if phase_b_failures:
        write_failure_report(
            "B", phase_b_failures, config.OUTPUT_DIR, subject_slug=slug,
        )
        staging_path.unlink(missing_ok=True)
        print("\n[SANITY] Phase B blocking — staging file deleted, no report delivered.")
        sys.exit(2)
    print("[Sanity] Phase B passed — 6 comp cards, all images live, no AirDNA strings, no Jinja artifacts.")

    # Promote staging -> final
    final_path = config.OUTPUT_DIR / staging_path.name
    staging_path.replace(final_path)
    output_path = final_path
    print(f"[Report] Saved to: {output_path.resolve()}")

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
