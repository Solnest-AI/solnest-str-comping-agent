"""
AirROI API client — primary STR data provider.

Exposes:
  - get_comparables()       — 25 relevance-ranked comps for a location+size
  - get_estimate()          — revenue projections with percentiles + monthly distributions
  - get_listing_metrics()   — per-listing monthly time-series (occupancy, ADR, revenue)
  - run_airroi_pipeline()   — high-level wrapper returning (estimate, comps)

Auth:
  AIRROI_API_KEY in .env (header: X-API-KEY)

Docs:
  OpenAPI spec at https://api.airroi.com (v2.1.1)
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

import httpx

import config
from schema import PropertyBasics


# ── Exceptions ────────────────────────────────────────────────────────

class AirROIError(RuntimeError):
    """Raised when the AirROI API returns a non-2xx response."""

    def __init__(self, status: int, message: str, body: Optional[dict] = None):
        super().__init__(f"AirROI API {status}: {message}")
        self.status = status
        self.message = message
        self.body = body or {}


# ── HTTP helper ───────────────────────────────────────────────────────

async def _get(
    endpoint: str,
    params: dict,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """GET from AirROI with auth header. Raises AirROIError on failure."""
    config.ensure_airroi_configured()

    headers = {
        "X-API-KEY": config.AIRROI_API_KEY,
    }
    url = config.AIRROI_BASE_URL.rstrip("/") + endpoint

    # Strip None params
    params = {k: v for k, v in params.items() if v is not None}

    if client is None:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as c:
            resp = await c.get(url, params=params, headers=headers)
    else:
        resp = await client.get(url, params=params, headers=headers)

    try:
        data = resp.json()
    except Exception as e:
        raise AirROIError(resp.status_code, f"non-JSON response: {e}")

    if resp.status_code >= 400:
        msg = data.get("message") or f"HTTP {resp.status_code}"
        raise AirROIError(resp.status_code, str(msg), data)

    return data


# ── Public: single listing details ───────────────────────────────────

async def get_listing(
    listing_id: int,
    *,
    currency: str = "usd",
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """Fetch full details for a single Airbnb listing by ID.

    Returns the full ListingDetailsResponse with: listing_info, host_info,
    location_info, property_details, booking_settings, pricing_info,
    ratings, performance_metrics.
    """
    return await _get("/listings", {"id": listing_id, "currency": currency}, client=client)


# ── Public: comparable listings ──────────────────────────────────────

async def get_comparables(
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    address: Optional[str] = None,
    bedrooms: int,
    baths: float,
    guests: int,
    currency: str = "usd",
    radius: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Fetch up to 25 comparable listings ranked by relevance.

    Location via coords OR address (not both).
    Optional radius in miles — widens the search area for thin markets.
    Returns the ``listings`` array from the response.
    """
    params: dict = {
        "bedrooms": bedrooms,
        "baths": baths,
        "guests": guests,
        "currency": currency,
    }
    if radius is not None:
        params["radius"] = radius
    if address and not (latitude is not None and longitude is not None):
        params["address"] = address
    else:
        params["latitude"] = latitude
        params["longitude"] = longitude

    data = await _get("/listings/comparables", params, client=client)
    return data.get("listings") or []


# ── Public: revenue estimate ─────────────────────────────────────────

async def get_estimate(
    *,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    address: Optional[str] = None,
    bedrooms: int,
    baths: float,
    guests: int,
    currency: str = "usd",
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """Get revenue projections with percentiles + monthly distributions.

    Location via coords (lat/lng) OR address (not both).
    Returns the full response dict with: revenue, average_daily_rate,
    occupancy, percentiles, currency, monthly_revenue_distributions,
    comparable_listings.
    """
    params: dict = {
        "bedrooms": bedrooms,
        "baths": baths,
        "guests": guests,
        "currency": currency,
    }
    if address and not (lat is not None and lng is not None):
        params["address"] = address
    else:
        params["lat"] = lat
        params["lng"] = lng

    return await _get("/calculator/estimate", params, client=client)


# ── Public: per-listing monthly metrics ──────────────────────────────

async def get_listing_metrics(
    listing_id: int,
    num_months: int = 12,
    currency: str = "usd",
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Fetch monthly time-series for a single listing.

    Each entry in the returned list has: date, occupancy.{avg,p25,p50,p75,p90},
    average_daily_rate.{...}, revenue.{...}, revpar.{...}.
    """
    params = {
        "id": listing_id,
        "num_months": num_months,
        "currency": currency,
    }
    data = await _get("/listings/metrics/all", params, client=client)
    return data.get("results") or []


# ── Public: high-level pipeline wrapper ──────────────────────────────

async def run_airroi_pipeline(
    prop: PropertyBasics,
    *,
    currency: str = "usd",
    radius_override: Optional[int] = None,
) -> tuple[dict, list[dict]]:
    """Fetch estimate + comparables and merge into a single comp pool.

    Returns (estimate_data, comps_list) where:
      - estimate_data is the /calculator/estimate response
      - comps_list is a deduped list of listing dicts from both endpoints
    """
    # Build location kwargs — prefer coords when available
    has_coords = (
        getattr(prop, "latitude", None) is not None
        and getattr(prop, "longitude", None) is not None
    )

    # Widen search radius for large properties (5+ BR) — thin comp pools
    radius = radius_override if radius_override is not None else (5 if prop.bedrooms >= 5 else None)
    if radius:
        print(f"[AirROI] Using {radius}-mile radius for {prop.bedrooms}BR property")

    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        # Fire estimate + comparables concurrently.
        # When coords are available, search BOTH by coords AND address —
        # AirROI returns different comp pools for each, and merging gives
        # the widest possible candidate set.
        coros = []
        if has_coords:
            coros.append(get_estimate(
                lat=float(prop.latitude), lng=float(prop.longitude),
                bedrooms=prop.bedrooms, baths=prop.bathrooms,
                guests=prop.max_guests, currency=currency, client=client,
            ))
            coros.append(get_comparables(
                latitude=float(prop.latitude), longitude=float(prop.longitude),
                bedrooms=prop.bedrooms, baths=prop.bathrooms,
                guests=prop.max_guests, currency=currency, radius=radius,
                client=client,
            ))
            # Also search by address for comps the coord search misses
            if prop.address:
                coros.append(get_comparables(
                    address=prop.address,
                    bedrooms=prop.bedrooms, baths=prop.bathrooms,
                    guests=prop.max_guests, currency=currency, radius=radius,
                    client=client,
                ))
        else:
            coros.append(get_estimate(
                address=prop.address,
                bedrooms=prop.bedrooms, baths=prop.bathrooms,
                guests=prop.max_guests, currency=currency, client=client,
            ))
            coros.append(get_comparables(
                address=prop.address,
                bedrooms=prop.bedrooms, baths=prop.bathrooms,
                guests=prop.max_guests, currency=currency, radius=radius,
                client=client,
            ))

        results = await asyncio.gather(*coros, return_exceptions=True)

    # Unpack: first result is always the estimate, rest are comp lists
    estimate_data = results[0]
    comp_results = results[1:]

    # Handle errors
    if isinstance(estimate_data, Exception):
        raise estimate_data
    comps_list = []
    for r in comp_results:
        if isinstance(r, Exception):
            print(f"[airroi] warning: comparables fetch failed: {r}", file=sys.stderr)
        elif isinstance(r, list):
            comps_list.extend(r)

    # Merge comps from estimate's comparable_listings + dedicated comps endpoint
    merged: dict[int, dict] = {}
    for listing in comps_list:
        li = listing.get("listing_info") or {}
        lid = li.get("listing_id")
        if lid:
            merged[lid] = listing

    for listing in estimate_data.get("comparable_listings") or []:
        li = listing.get("listing_info") or {}
        lid = li.get("listing_id")
        if lid and lid not in merged:
            merged[lid] = listing

    return estimate_data, list(merged.values())


# ── CLI smoke test ────────────────────────────────────────────────────

async def _smoke_test():
    """Manual smoke test: `python -m scrapers.airroi`."""
    print("[smoke] Testing AirROI — Malibu estimate...")
    est = await get_estimate(
        address="21506 Pacific Coast Hwy, Malibu, CA",
        bedrooms=3, baths=2, guests=6,
    )
    print(f"  Revenue: ${est.get('revenue', 0):,.0f}")
    print(f"  ADR: ${est.get('average_daily_rate', 0):.0f}")
    print(f"  Occ: {est.get('occupancy', 0):.0%}")
    print(f"  Comparable listings: {len(est.get('comparable_listings', []))}")

    print("\n[smoke] Comparable listings...")
    comps = await get_comparables(
        address="21506 Pacific Coast Hwy, Malibu, CA",
        bedrooms=3, baths=2, guests=6,
    )
    print(f"  Comps returned: {len(comps)}")
    if comps:
        top = sorted(
            comps,
            key=lambda c: (c.get("performance_metrics") or {}).get("ttm_revenue") or 0,
            reverse=True,
        )[:3]
        print("  Top 3 by TTM revenue:")
        for i, c in enumerate(top, 1):
            li = c.get("listing_info") or {}
            pm = c.get("performance_metrics") or {}
            pd = c.get("property_details") or {}
            print(f"    #{i} {pd.get('bedrooms')}BR — ${pm.get('ttm_revenue', 0):,.0f} — {li.get('listing_name', '')[:50]}")


if __name__ == "__main__":
    asyncio.run(_smoke_test())
