"""
Universal property search — find and extract property data from any listing URL
or plain address using Firecrawl's REST API.

Replaces the old realtor.ca-only MLS scraper. Works with any real estate site:
Zillow, Redfin, realtor.ca, VRBO, Twiddy, Homes.com, etc.

Flow:
  1. search_for_property(address) — Firecrawl search to find listing URLs
  2. scrape_listing_url(url)      — Firecrawl scrape with JSON schema extraction
  3. search_hero_image(address)   — last-resort photo search

All functions return a normalized dict matching the old MLS scraper's output
shape, or None on failure.
"""

from __future__ import annotations

import re
import sys
from typing import Optional

import httpx

import config


# ── JSON schema for Firecrawl extraction ─────────────────────────────

PROPERTY_SCHEMA = {
    "type": "object",
    "properties": {
        "address":        {"type": "string",  "description": "Full street address of the property"},
        "title":          {"type": "string",  "description": "Listing title or property name"},
        "bedrooms":       {"type": "integer", "description": "Number of bedrooms"},
        "bathrooms":      {"type": "number",  "description": "Number of bathrooms (supports half baths like 2.5)"},
        "sqft":           {"type": "integer", "description": "Square footage of the property"},
        "max_guests":     {"type": "integer", "description": "Maximum guest capacity (if listed, e.g. vacation rentals)"},
        "property_type":  {"type": "string",  "description": "Property type: House, Condo, Townhouse, Cabin, Villa, etc."},
        "description":    {"type": "string",  "description": "Listing description text"},
        "hero_image_url": {"type": "string",  "description": "URL of the main/hero property photo"},
        "market":         {"type": "string",  "description": "City or town name where the property is located"},
        "price":          {"type": "number",  "description": "Listing price or estimated value in local currency"},
    },
}


# ── Locality agreement ───────────────────────────────────────────────
#
# Firecrawl's search returns whatever the web gives it. Ranking hits purely on
# extraction richness happily picks a 5BR lodge in the next town over rather
# than the thin-but-correct listing for the address that was actually asked
# for, and nothing downstream notices: the AirROI pull, the comp scorer and
# the Phase A/B gates all only check internal consistency. So the query's own
# city has to be a first-class ranking term, and a hard reject.

_STREET_SUFFIXES = {
    "rd", "road", "st", "street", "ave", "av", "avenue", "dr", "drive",
    "ln", "lane", "blvd", "boulevard", "ct", "court", "way", "pl", "place",
    "ter", "terr", "terrace", "trl", "trail", "hwy", "highway", "cir",
    "circle", "pkwy", "parkway", "loop", "run", "path", "row", "sq",
    "square", "cres", "crescent", "close", "gate", "mews", "walk",
    "unit", "apt", "suite", "ste", "no", "#",
}

_COUNTRY_WORDS = {
    "usa", "us", "u.s.", "u.s.a.", "united states", "united states of america",
    "canada", "ca",
}

# Region tokens that trail a city name. Two-letter abbreviations plus the
# spelled-out forms, since Firecrawl returns both.
_REGION_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}

_REGION_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "northwest territories",
    "nunavut", "ontario", "prince edward island", "quebec", "saskatchewan",
    "yukon",
}


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _is_region_token(token: str) -> bool:
    t = (token or "").strip().strip(".,").strip()
    if not t:
        return False
    if t.upper() in _REGION_ABBR and len(t) == 2:
        return True
    return t.lower() in _REGION_NAMES


def query_locality(address: str) -> str:
    """Best-effort city/town name out of a free-form query address.

    Handles the shapes users actually pass:
      "812 Ski Mountain Rd, Gatlinburg TN"      -> "Gatlinburg"
      "1234 Fake St, Gatlinburg, TN 37738"      -> "Gatlinburg"
      "5005 Valley Drive Unit 13, Sun Peaks BC" -> "Sun Peaks"
      "Gatlinburg, Tennessee"                   -> "Gatlinburg"
      "812 Ski Mountain Rd Gatlinburg TN"       -> "Gatlinburg"
    Returns "" when no locality can be identified — callers must treat that
    as "no opinion", never as a mismatch.
    """
    if not address:
        return ""

    # Drop postal codes so they cannot be mistaken for a segment.
    text = re.sub(r"\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b", " ", address, flags=re.I)
    text = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", text)

    parts = [p.strip() for p in text.split(",") if p.strip()]
    # Drop trailing country segments.
    while parts and _norm(parts[-1]) in {_norm(c) for c in _COUNTRY_WORDS}:
        parts.pop()
    if not parts:
        return ""

    tokens = parts[-1].split()
    while tokens and _is_region_token(tokens[-1]):
        tokens.pop()

    if not tokens:
        # The last segment was only the region — the city is the segment before.
        if len(parts) >= 2:
            tokens = parts[-2].split()
        else:
            return ""

    # Single-segment input ("812 Ski Mountain Rd Gatlinburg"): the city is
    # whatever follows the last street-suffix token.
    lowered = [t.lower().strip(".,") for t in tokens]
    suffix_idx = -1
    for i, t in enumerate(lowered):
        if t in _STREET_SUFFIXES:
            suffix_idx = i
    if suffix_idx >= 0:
        tokens = tokens[suffix_idx + 1:]

    # Strip leading house/unit numbers.
    while tokens and re.fullmatch(r"[#\d][\d\-/]*", tokens[0]):
        tokens.pop(0)

    return " ".join(tokens).strip(" ,.-")


def _mentions_locality(city: str, *texts: str) -> bool:
    """True when `city` appears as a whole phrase in any of `texts`."""
    needle = _norm(city)
    if not needle:
        return False
    pattern = r"\b" + re.escape(needle) + r"\b"
    return any(re.search(pattern, _norm(t)) for t in texts if t)


def _locality_score(city: str, *texts: str) -> int:
    """+6 when the result agrees with the requested city, -6 when it names a
    different one, 0 when the result says nothing about where it is."""
    if not city:
        return 0
    if _mentions_locality(city, *texts):
        return 6
    if any((t or "").strip() for t in texts):
        return -6
    return 0


def locality_agrees(city: str, result: dict) -> bool:
    """Reject gate. A result only fails when it names a locality AND that
    locality is not the one that was asked for."""
    if not city or not result:
        return True
    address = result.get("raw_address") or ""
    market = result.get("market") or ""
    title = result.get("title") or ""
    if not (address.strip() or market.strip()):
        return True   # nothing to contradict — let the caller decide on merit
    return _mentions_locality(city, address, market, title)


# ── Result parser ────────────────────────────────────────────────────

def _coerce_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _coerce_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_firecrawl_result(raw: dict, source_url: str) -> dict:
    """Normalize a Firecrawl JSON extraction result into the standard
    property dict shape that agent.py expects (same keys as old MLS scraper)."""
    return {
        "hero_image_url": raw.get("hero_image_url") or "",
        "bedrooms":       _coerce_int(raw.get("bedrooms")),
        "bathrooms":      _coerce_float(raw.get("bathrooms")),
        "sqft":           _coerce_int(raw.get("sqft")),
        "max_guests":     _coerce_int(raw.get("max_guests")),
        "property_type":  raw.get("property_type") or "Property",
        "listing_url":    source_url,
        "raw_address":    raw.get("address") or "",
        "title":          raw.get("title") or "",
        "description":    raw.get("description") or "",
        "market":         raw.get("market") or "",
        "price":          _coerce_float(raw.get("price")),
    }


# Each fallback attempt is a full Firecrawl scrape (up to 180s), so the
# locality-aware retry loop is capped rather than walking all 8 hits.
_MAX_FALLBACK_SCRAPES = 3


# ── Firecrawl HTTP helpers ───────────────────────────────────────────

async def _firecrawl_post(endpoint: str, body: dict) -> dict:
    """POST to Firecrawl REST API with auth."""
    config.ensure_firecrawl_configured()
    url = config.FIRECRAWL_BASE_URL.rstrip("/") + endpoint
    headers = {
        "Authorization": f"Bearer {config.FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.ReadTimeout:
        print(f"[firecrawl] {endpoint} timed out after 180s", file=sys.stderr)
        return {}
    except httpx.HTTPError as e:
        print(f"[firecrawl] {endpoint} HTTP error: {e}", file=sys.stderr)
        return {}
    if resp.status_code >= 400:
        print(f"[firecrawl] {endpoint} returned {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


# ── Public: scrape any listing URL ───────────────────────────────────

async def scrape_listing_url(url: str, expect_locality: str = "") -> Optional[dict]:
    """Scrape a property listing page and extract structured data.

    Works with any real estate site — Zillow, Redfin, realtor.ca, VRBO,
    Twiddy, Homes.com, etc. Uses Firecrawl's JSON extraction with an
    LLM prompt to pull property details regardless of site structure.

    Returns a normalized property dict or None on failure.
    """
    print(f"[Search] Scraping listing: {url[:80]}...")

    body = {
        "url": url,
        "formats": ["json"],
        "jsonOptions": {
            "prompt": (
                "Extract the property listing details from this real estate page. "
                "Find the street address, number of bedrooms, bathrooms, square footage, "
                "property type, description, the main/hero property photo URL, "
                "the city/town name, and the listing price if shown. "
                "For the hero image, find the largest/main property photo URL (not a logo or icon)."
            ),
            "schema": PROPERTY_SCHEMA,
        },
        "waitFor": 5000,
    }

    data = await _firecrawl_post("/scrape", body)
    json_data = (data.get("data") or {}).get("json")
    if not json_data or not isinstance(json_data, dict):
        print(f"[Search] No structured data extracted from {url[:60]}", file=sys.stderr)
        return None

    result = _parse_firecrawl_result(json_data, url)

    # Require at least a hero image or bedrooms to consider this a real result
    if not result["hero_image_url"] and not result["bedrooms"]:
        print(f"[Search] Extraction returned no usable data from {url[:60]}", file=sys.stderr)
        return None

    # When we got here from an address search, the page must be in the town
    # that was asked for. A rich extraction for the wrong city is worse than
    # no result: the whole report gets built for the wrong property.
    if expect_locality and not locality_agrees(expect_locality, result):
        print(f"[Search] Rejected {url[:60]} — extracted "
              f"{result['raw_address'] or result['market']!r}, expected "
              f"{expect_locality!r}", file=sys.stderr)
        return None

    print(f"[Search] Extracted: {result.get('title', '')[:50]} / "
          f"{result['bedrooms']}BR / {result['bathrooms']}BA / "
          f"Hero: {'yes' if result['hero_image_url'] else 'no'}")
    return result


# ── Public: search for a property by address ─────────────────────────

async def search_for_property(address: str) -> Optional[dict]:
    """Search the web for a property listing matching the address.

    Searches across real estate sites (Zillow, Redfin, realtor.ca, Homes.com,
    VRBO, etc.), finds the best listing URL, and scrapes it for property data.

    Returns a normalized property dict or None if no listing found.
    """
    print(f"[Search] Searching for: {address}")

    search_body = {
        "query": f"{address} property listing",
        "limit": 8,
        "scrapeOptions": {
            "formats": ["json"],
            "jsonOptions": {
                "prompt": (
                    "Extract the property listing details from this page. "
                    "Find the street address, bedrooms, bathrooms, square footage, "
                    "property type, description, the main property photo URL, "
                    "the city/town name, and price."
                ),
                "schema": PROPERTY_SCHEMA,
            },
        },
    }

    data = await _firecrawl_post("/search", search_body)
    results = data.get("data") or []

    if not results:
        print(f"[Search] No search results for: {address}", file=sys.stderr)
        return None

    # The town the caller actually asked about. Everything below treats this
    # as a first-class ranking term — extraction richness alone will happily
    # pick a better-documented property in the next town over.
    want_city = query_locality(address)
    if want_city:
        print(f"[Search] Requiring locality: {want_city}")

    # Score results: prefer ones that extracted property data (bedrooms, hero image)
    best: Optional[dict] = None
    best_score = -1

    for r in results:
        json_data = r.get("json")
        r_url = r.get("url") or ""
        if not json_data or not isinstance(json_data, dict):
            continue

        score = 0
        if json_data.get("bedrooms"):
            score += 3
        if json_data.get("hero_image_url"):
            score += 3
        if json_data.get("bathrooms"):
            score += 2
        if json_data.get("sqft"):
            score += 1
        if json_data.get("description"):
            score += 1

        # Boost real estate domains
        real_estate_domains = [
            "zillow.com", "redfin.com", "realtor.ca", "realtor.com",
            "homes.com", "vrbo.com", "twiddy.com", "trulia.com",
            "coldwellbanker.com", "century21.com", "remax.com",
        ]
        if any(d in r_url.lower() for d in real_estate_domains):
            score += 2

        # Locality agreement. Weighted above the whole extraction-richness
        # budget so a correct-town result outranks a richer wrong-town one.
        score += _locality_score(
            want_city,
            json_data.get("address") or "",
            json_data.get("market") or "",
            json_data.get("title") or "",
        )

        if score > best_score:
            best_score = score
            best = {"json": json_data, "url": r_url}

    if best is not None and want_city:
        # Hard reject: never return a property from a town nobody asked about.
        candidate = _parse_firecrawl_result(best["json"], best["url"])
        if not locality_agrees(want_city, candidate):
            print(f"[Search] Best extraction is in "
                  f"{candidate['market'] or candidate['raw_address']!r}, not "
                  f"{want_city!r} — discarding.", file=sys.stderr)
            best = None
            best_score = -1

    if not best or best_score < 3:
        print(f"[Search] No quality listing found for: {address}", file=sys.stderr)

        # Fallback: scrape the top search result that is not already known to
        # be in the wrong town, and re-check the locality after scraping.
        # Capped — each attempt is a full Firecrawl scrape (up to 180s).
        attempts = 0
        for r in results:
            if attempts >= _MAX_FALLBACK_SCRAPES:
                break
            top_url = r.get("url") or ""
            if not top_url:
                continue
            jd = r.get("json") if isinstance(r.get("json"), dict) else {}
            if want_city and jd and not locality_agrees(
                want_city, _parse_firecrawl_result(jd, top_url)
            ):
                continue
            attempts += 1
            print(f"[Search] Trying top result as fallback: {top_url[:60]}")
            scraped = await scrape_listing_url(top_url, expect_locality=want_city)
            if scraped:
                return scraped
        return None

    result = _parse_firecrawl_result(best["json"], best["url"])

    print(f"[Search] Best match: {best['url'][:60]}")
    print(f"         {result.get('title', '')[:50]} / "
          f"{result['bedrooms']}BR / {result['bathrooms']}BA / "
          f"Hero: {'yes' if result['hero_image_url'] else 'no'}")
    return result


# ── Public: search for hero image only ───────────────────────────────

async def search_hero_image(address: str) -> Optional[str]:
    """Last-resort search for just a property photo when no listing was found.

    Searches for the address + 'property photo' and returns the first
    relevant image URL found.
    """
    print(f"[Search] Searching for hero image: {address}")

    body = {
        "query": f"{address} property photo exterior",
        "limit": 5,
        "scrapeOptions": {
            "formats": ["json"],
            "jsonOptions": {
                "prompt": "Find the main property/house photo URL from this page. Return the largest exterior photo of the property.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "hero_image_url": {"type": "string", "description": "URL of the main property photo"},
                    },
                },
            },
        },
    }

    data = await _firecrawl_post("/search", body)
    for r in (data.get("data") or []):
        json_data = r.get("json") or {}
        img = json_data.get("hero_image_url")
        if img and img.startswith("http"):
            print(f"[Search] Found hero image: {img[:80]}")
            return img

    print(f"[Search] No hero image found for: {address}", file=sys.stderr)
    return None


# ── CLI smoke test ───────────────────────────────────────────────────

async def _smoke_test():
    """Manual smoke test: `python -m scrapers.property_search`."""

    print("[smoke] Search for property by address...")
    result = await search_for_property("102 Duck Landing Ln, Duck NC 27949")
    if result:
        for k, v in result.items():
            if isinstance(v, str) and len(v) > 80:
                v = v[:80] + "..."
            print(f"  {k}: {v}")
    else:
        print("  No result found.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
