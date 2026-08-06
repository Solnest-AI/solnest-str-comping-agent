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


# ── Apify Zillow scraper ────────────────────────────────────────────

def _is_zillow_url(url: str) -> bool:
    return "zillow.com/homedetails/" in (url or "").lower()


async def _scrape_zillow_with_apify(url: str) -> Optional[dict]:
    """Scrape a Zillow listing URL via Apify's Zillow Detail Scraper.

    Uses the sync endpoint to get results in a single HTTP call (up to 120s).
    Returns a normalized property dict or None on failure.
    """
    if not config.APIFY_TOKEN:
        return None

    actor_id = config.APIFY_ZILLOW_ACTOR_ID
    api_url = (
        f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
        f"?token={config.APIFY_TOKEN}"
    )

    try:
        print("[Apify] Scraping Zillow listing...")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(api_url, json={"startUrls": [{"url": url}]})

        if resp.status_code >= 400:
            print(f"[Apify] HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return None

        items = resp.json()
        if not isinstance(items, list) or not items:
            print("[Apify] No results returned", file=sys.stderr)
            return None

        result = _parse_apify_zillow_item(items[0], url)
        if result:
            print(f"[Apify] Found: {result['raw_address']}")
            print(f"         {result['bedrooms']}BR / {result['bathrooms']}BA / "
                  f"{result['sqft'] or '?'} sqft / {result['property_type']}")
        return result

    except httpx.ReadTimeout:
        print("[Apify] Timed out after 120s", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[Apify] Error: {e}", file=sys.stderr)
        return None


async def _google_search_zillow_url(address: str) -> Optional[str]:
    """Search Google for the Zillow listing URL of a property address.

    Uses a simple httpx GET to Google's search page, parses the results
    for a zillow.com/homedetails/ URL. Returns the URL or None.
    """
    import re
    try:
        query = f"site:zillow.com/homedetails {address}"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.google.com/search",
                params={"q": query, "num": 3},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
        if resp.status_code != 200:
            return None
        # Extract zillow homedetails URLs from Google results
        matches = re.findall(
            r'https?://www\.zillow\.com/homedetails/[^"&\s]+_zpid/',
            resp.text,
        )
        if matches:
            print(f"[Search] Found Zillow URL via Google: {matches[0][:60]}")
            return matches[0]
        return None
    except Exception as e:
        print(f"[Search] Google Zillow search failed: {e}", file=sys.stderr)
        return None


async def _search_zillow_via_apify(address: str) -> Optional[dict]:
    """Search for a property on Zillow by address using Apify directly.

    Constructs a Zillow search URL from the address and runs the Apify
    Zillow Detail Scraper against it. If the actor finds a listing, returns
    the normalized property dict. Returns None on failure.
    """
    if not config.APIFY_TOKEN:
        return None

    # Construct a Zillow address search URL
    # Format: zillow.com/homes/<address-slugified>_rb/
    slug = address.replace(",", "").replace(".", "").replace(" ", "-")
    search_url = f"https://www.zillow.com/homes/{slug}_rb/"

    actor_id = config.APIFY_ZILLOW_ACTOR_ID
    api_url = (
        f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
        f"?token={config.APIFY_TOKEN}"
    )

    try:
        print(f"[Apify] Searching Zillow for: {address}")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(api_url, json={"startUrls": [{"url": search_url}]})

        if resp.status_code >= 400:
            # Search URL didn't work — the detail actor needs a /homedetails/ URL.
            # Try constructing one from address parts instead.
            return None

        items = resp.json()
        if not isinstance(items, list) or not items:
            return None

        # Parse with the same logic as _scrape_zillow_with_apify
        return _parse_apify_zillow_item(items[0], search_url)

    except Exception as e:
        print(f"[Apify] Zillow search failed: {e}", file=sys.stderr)
        return None


def _parse_apify_zillow_item(item: dict, source_url: str) -> Optional[dict]:
    """Parse an Apify Zillow actor result item into the standard property dict."""
    raw_addr = item.get("address") or ""
    if isinstance(raw_addr, dict):
        parts = [
            raw_addr.get("streetAddress", ""),
            raw_addr.get("city", ""),
            raw_addr.get("state", ""),
            raw_addr.get("zipcode", ""),
        ]
        raw_addr = ", ".join(p for p in parts if p)

    hero = (
        item.get("hiResImageLink")
        or item.get("imgSrc")
        or ""
    )
    if not hero:
        photos = item.get("photos") or []
        if photos and isinstance(photos, list):
            first = photos[0]
            hero = first.get("url") or first if isinstance(first, str) else ""

    market = ""
    if isinstance(item.get("address"), dict):
        market = item["address"].get("city") or ""

    return {
        "hero_image_url": hero or "",
        "bedrooms": _coerce_int(item.get("bedrooms")),
        "bathrooms": _coerce_float(item.get("bathrooms")),
        "sqft": _coerce_int(item.get("livingArea") or item.get("livingAreaValue")),
        "max_guests": None,
        "property_type": (item.get("homeType") or "Property").replace("_", " ").title(),
        "listing_url": source_url,
        "raw_address": raw_addr,
        "title": item.get("streetAddress") or raw_addr.split(",")[0] if raw_addr else "",
        "description": item.get("description") or "",
        "market": market,
        "price": _coerce_float(item.get("price")),
    }


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

async def scrape_listing_url(url: str) -> Optional[dict]:
    """Scrape a property listing page and extract structured data.

    For Zillow URLs: uses Apify (reliable, no hallucination).
    For everything else: uses Firecrawl's LLM extraction.

    Returns a normalized property dict or None on failure.
    """
    # Zillow URLs → Apify first (reliable structured data, no LLM hallucination)
    if _is_zillow_url(url):
        apify_result = await _scrape_zillow_with_apify(url)
        if apify_result and (apify_result.get("bedrooms") or apify_result.get("hero_image_url")):
            return apify_result
        print("[Apify] Zillow scrape failed or empty — falling back to Firecrawl")

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
        print(f"[Search] No search results from Firecrawl for: {address}", file=sys.stderr)
        # Fallback: search Google for the Zillow listing URL, then Apify it
        if config.APIFY_TOKEN:
            zillow_url = await _google_search_zillow_url(address)
            if zillow_url:
                apify_result = await _scrape_zillow_with_apify(zillow_url)
                if apify_result and apify_result.get("bedrooms"):
                    return apify_result
        return None

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

        if score > best_score:
            best_score = score
            best = {"json": json_data, "url": r_url}

    if not best or best_score < 3:
        print(f"[Search] No quality listing found for: {address}", file=sys.stderr)

        # Fallback: try scraping the top search result directly
        if results and results[0].get("url"):
            top_url = results[0]["url"]
            print(f"[Search] Trying top result as fallback: {top_url[:60]}")
            return await scrape_listing_url(top_url)
        return None

    best_url = best["url"]

    # If the best result is a Zillow URL, use Apify for reliable extraction
    # instead of trusting Firecrawl's LLM-extracted JSON (which hallucinates).
    if _is_zillow_url(best_url) and config.APIFY_TOKEN:
        print(f"[Search] Best match is Zillow — routing to Apify: {best_url[:60]}")
        apify_result = await _scrape_zillow_with_apify(best_url)
        if apify_result and (apify_result.get("bedrooms") or apify_result.get("hero_image_url")):
            return apify_result
        print("[Search] Apify failed — falling back to Firecrawl extraction")

    result = _parse_firecrawl_result(best["json"], best_url)

    print(f"[Search] Best match: {best_url[:60]}")
    print(f"         {result.get('title', '')[:50]} / "
          f"{result['bedrooms']}BR / {result['bathrooms']}BA / "
          f"Hero: {'yes' if result['hero_image_url'] else 'no'}")
    return result


# ── Public: search for hero image only ───────────────────────────────

def get_street_view_url(address: str, size: str = "800x600") -> Optional[str]:
    """Build a Google Street View Static API URL for the given address.

    Returns a deterministic URL that renders the actual street-level photo
    of the property. Requires GOOGLE_MAPS_API_KEY in config.
    Returns None if no API key is configured.
    """
    if not config.GOOGLE_MAPS_API_KEY:
        return None
    # URL-encode the address for the API
    import urllib.parse
    encoded = urllib.parse.quote(address)
    url = (
        f"https://maps.googleapis.com/maps/api/streetview"
        f"?size={size}&location={encoded}&key={config.GOOGLE_MAPS_API_KEY}"
    )
    return url


_MAP_URL_PATTERNS = (
    "maps.googleapis.com", "api.mapbox.com", "staticmap",
    "streetview", "placeholder", "example.com",
)


def _is_real_photo_url(url: str) -> bool:
    """Check if a URL looks like an actual property photo, not a map/placeholder."""
    if not url or not url.startswith("http"):
        return False
    return not any(p in url.lower() for p in _MAP_URL_PATTERNS)


async def search_hero_image(address: str) -> Optional[str]:
    """Find a hero image for a property by address.

    Priority:
      1. Firecrawl web search — finds real listing/property photos
      2. Google Street View — last resort, deterministic but not pretty
    """
    print(f"[Search] Searching for hero image: {address}")

    # Priority 1: Web search for actual property photos
    body = {
        "query": f"{address} property photo exterior",
        "limit": 5,
        "scrapeOptions": {
            "formats": ["json"],
            "jsonOptions": {
                "prompt": "Find the main property/house photo URL from this page. Return the largest exterior photo of the property. Do NOT return map images, street view images, or placeholder images.",
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
        if _is_real_photo_url(img):
            print(f"[Search] Found property photo: {img[:80]}")
            return img

    # Priority 2: Google Street View (last resort)
    street_view_url = get_street_view_url(address)
    if street_view_url:
        try:
            import urllib.parse
            encoded = urllib.parse.quote(address)
            meta_url = (
                f"https://maps.googleapis.com/maps/api/streetview/metadata"
                f"?location={encoded}&key={config.GOOGLE_MAPS_API_KEY}"
            )
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(meta_url)
            if resp.status_code == 200:
                meta = resp.json()
                if meta.get("status") == "OK":
                    print("[Search] Using Google Street View as last resort")
                    return street_view_url
                else:
                    print("[Search] No Street View coverage for this address")
        except Exception as e:
            print(f"[Search] Street View check failed: {e}", file=sys.stderr)

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
