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
import random
import sys
from typing import Any, Optional

import httpx

import config
from schema import PropertyBasics
from . import _cache


# ── Retry policy ──────────────────────────────────────────────────────
# Transport-level retries (connect/read failures before a response exists) are
# handled by httpx itself. Status-level retries (429 + 5xx, where a response
# DID arrive) are handled by the loop in _request_with_retries.

TRANSPORT_RETRIES = 3          # httpx.AsyncHTTPTransport(retries=...)
STATUS_RETRY_ATTEMPTS = 3      # extra attempts after the first, on 429/5xx
RETRY_BACKOFF_BASE = 0.5       # seconds; doubles each attempt
RETRY_BACKOFF_MAX = 8.0        # cap on a single computed backoff sleep
RETRY_AFTER_MAX = 10.0         # cap on an honoured Retry-After header; a comp
                               # fetch runs 6-wide, so an unbounded server-
                               # supplied sleep would stall the whole CLI
RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Sentinel status for errors that never got an HTTP status code
# (transport failure, empty comp pool, missing location argument).
NO_HTTP_STATUS = 0


# ── Exceptions ────────────────────────────────────────────────────────

class AirROIError(RuntimeError):
    """Raised when an AirROI call fails.

    Covers non-2xx responses AND transport failures (timeout, connect error,
    read error) so that a caller's ``except AirROIError`` actually catches
    everything this module can throw. ``status`` is 0 when no HTTP response
    was ever received.
    """

    def __init__(self, status: int, message: str, body: Optional[dict] = None):
        super().__init__(f"AirROI API {status}: {message}")
        self.status = status
        self.message = message
        self.body = body or {}


# ── HTTP helper ───────────────────────────────────────────────────────

def _new_client(timeout: Optional[float] = None) -> httpx.AsyncClient:
    """AsyncClient with transport-level retries enabled.

    Any client that talks to AirROI should come from here — a bare
    httpx.AsyncClient() is single-attempt and dies on the first connection
    reset.
    """
    transport = httpx.AsyncHTTPTransport(retries=TRANSPORT_RETRIES)
    return httpx.AsyncClient(
        timeout=timeout if timeout is not None else config.HTTP_TIMEOUT,
        transport=transport,
    )


def _extract_error(data: Any, status: int) -> tuple[str, dict]:
    """Pull a human-readable message out of an AirROI error body.

    AirROI uses two envelopes (both verified live 2026-08-29):
      {"code": 400, "message": "Parameters 'bedrooms', ... are required."}
      {"errors": ["query param currency Allowed currency values are ..."]}
    A list- or string-shaped body is also handled — the old code called
    ``.get`` on it and blew up with an uncaught AttributeError.
    """
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error") or data.get("detail")
        if not msg:
            errors = data.get("errors") or []
            if isinstance(errors, (list, tuple)):
                msg = "; ".join(str(x) for x in errors)
            else:
                msg = str(errors)
        return (str(msg).strip() or f"HTTP {status}"), data

    if isinstance(data, (list, tuple)):
        msg = "; ".join(str(x) for x in data)
        return (msg[:300] or f"HTTP {status}"), {"raw": list(data)}

    return (str(data)[:300] or f"HTTP {status}"), {"raw": data}


def _retry_delay(resp: Optional[httpx.Response], attempt: int) -> float:
    """Seconds to wait before the next attempt. Honours Retry-After."""
    if resp is not None:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                # Retry-After may also be an HTTP-date, which float() rejects —
                # fall through to computed backoff in that case.
                return min(float(raw), RETRY_AFTER_MAX)
            except (TypeError, ValueError):
                pass
    # Exponential backoff with jitter so parallel comp fetches don't
    # re-synchronise into the same 429 wall.
    delay = RETRY_BACKOFF_BASE * (2 ** attempt)
    return min(delay, RETRY_BACKOFF_MAX) * (0.5 + random.random() / 2)


async def _request_with_retries(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    headers: dict,
) -> dict:
    """GET with backoff on 429/5xx and httpx.HTTPError wrapped into AirROIError."""
    last_status: Optional[int] = None
    last_msg = ""
    last_body: dict = {}

    for attempt in range(STATUS_RETRY_ATTEMPTS + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            # Transport failed (timeout / connect reset / read error). httpx
            # already retried the connection TRANSPORT_RETRIES times.
            last_status, last_msg = NO_HTTP_STATUS, f"{type(e).__name__}: {e}"
            last_body = {"transport_error": type(e).__name__}
            if attempt < STATUS_RETRY_ATTEMPTS:
                await asyncio.sleep(_retry_delay(None, attempt))
                continue
            raise AirROIError(NO_HTTP_STATUS, last_msg, last_body) from e

        if resp.status_code in RETRY_STATUS_CODES and attempt < STATUS_RETRY_ATTEMPTS:
            await asyncio.sleep(_retry_delay(resp, attempt))
            continue

        try:
            data = resp.json()
        except Exception as e:
            raise AirROIError(resp.status_code, f"non-JSON response: {e}",
                              {"raw_text": resp.text[:300]})

        if resp.status_code >= 400:
            msg, body = _extract_error(data, resp.status_code)
            raise AirROIError(resp.status_code, msg, body)

        return data

    # Unreachable in practice — the loop either returns or raises — but keep a
    # deterministic failure rather than falling off the end with None.
    raise AirROIError(last_status if last_status is not None else NO_HTTP_STATUS,
                      last_msg or "retries exhausted", last_body)


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

    # Every call below this line costs money. Serve a same-day repeat from disk.
    cached = _cache.get("airroi", endpoint, params)
    if cached is not None:
        return cached

    if client is None:
        async with _new_client() as c:
            data = await _request_with_retries(c, url, params, headers)
    else:
        data = await _request_with_retries(client, url, params, headers)

    _cache.put("airroi", endpoint, params, data)
    return data


def _location_params(
    *,
    latitude: Optional[float],
    longitude: Optional[float],
    address: Optional[str],
    lat_key: str,
    lng_key: str,
) -> dict:
    """Build the location half of a request.

    Coordinates win when both are supplied; otherwise the address is used.
    Uses ``is not None`` — a truthiness check drops latitude/longitude 0.0
    (Greenwich meridian, the equator) and silently falls back to the address.
    Raises locally when neither location form is usable, instead of sending a
    request that AirROI answers with a confusing 400.
    """
    has_coords = latitude is not None and longitude is not None
    if has_coords:
        return {lat_key: latitude, lng_key: longitude}
    if address:
        return {"address": address}
    raise AirROIError(
        NO_HTTP_STATUS,
        "no location supplied: pass both latitude/longitude, or an address",
    )


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
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Fetch up to 25 comparable listings ranked by relevance.

    Location via coords OR address. Coordinates win when both latitude and
    longitude are supplied (0.0 counts — see _location_params); the address is
    the fallback. Raises AirROIError if neither is usable.
    Returns the ``listings`` array from the response.
    """
    params: dict = {
        "bedrooms": bedrooms,
        "baths": baths,
        "guests": guests,
        "currency": currency,
    }
    params.update(_location_params(
        latitude=latitude, longitude=longitude, address=address,
        lat_key="latitude", lng_key="longitude",
    ))

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

    Location via coords (lat/lng) OR address. Coordinates win when both are
    supplied (0.0 counts); the address is the fallback. Raises AirROIError if
    neither is usable.
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
    params.update(_location_params(
        latitude=lat, longitude=lng, address=address,
        lat_key="lat", lng_key="lng",
    ))

    return await _get("/calculator/estimate", params, client=client)


# ── Public: per-listing monthly metrics ──────────────────────────────

def _normalize_metrics(results: list) -> list[dict]:
    """Normalize /listings/metrics/all rows to a single documented shape.

    The live endpoint returns BARE FLOATS, not percentile dicts, and names
    the RevPAR key ``rev_par`` (verified against fixtures/metrics.json, 12/12
    rows). Callers were written against a percentile-dict contract, so 100% of
    this data was being silently dropped by their ``isinstance(v, dict)`` guard.

    Normalizing here — rather than at each call site — means no caller can get
    it wrong. Every metric key is guaranteed to be a dict with at least "avg".
    If AirROI ever starts returning real percentile dicts, they pass through
    untouched.
    """
    out: list[dict] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        for k in ("occupancy", "average_daily_rate", "revenue"):
            if not isinstance(r.get(k), dict):
                r[k] = {"avg": r.get(k)}
        if not isinstance(r.get("revpar"), dict):
            # live key is rev_par; fall back to revpar if the shape ever changes
            r["revpar"] = {"avg": r.get("rev_par", r.get("revpar"))}
        out.append(r)
    return out


async def get_listing_metrics(
    listing_id: int,
    num_months: int = 12,
    currency: str = "usd",
    client: Optional[httpx.AsyncClient] = None,
) -> list[dict]:
    """Fetch monthly time-series for a single listing.

    Returns rows NORMALIZED by _normalize_metrics(). Each entry has:
      date (str "YYYY-MM"), min_nights,
      occupancy.avg, average_daily_rate.avg, revenue.avg, revpar.avg
    plus the raw live keys (occupancy is also a bare float on the wire, and
    revpar arrives as ``rev_par``) preserved untouched alongside.

    occupancy.avg is a FRACTION (0-1), not a percent.
    """
    params = {
        "id": listing_id,
        "num_months": num_months,
        "currency": currency,
    }
    data = await _get("/listings/metrics/all", params, client=client)
    return _normalize_metrics(data.get("results") or [])


# ── Public: high-level pipeline wrapper ──────────────────────────────

# The estimate's comparable_listings block is the primary comp source. Only pay
# for a separate /listings/comparables call when it comes back below this. The
# report's sanity gate needs 6 comps after heavy filtering, so this is set well
# above that rather than at "non-empty".
MIN_COMPS_FROM_ESTIMATE = 15


async def run_airroi_pipeline(
    prop: PropertyBasics,
    *,
    currency: str = "usd",
) -> tuple[dict, list[dict]]:
    """Fetch estimate + comparables and merge into a single comp pool.

    Returns (estimate_data, comps_list) where:
      - estimate_data is the /calculator/estimate response
      - comps_list is a deduped list of listing dicts (comparables endpoint,
        with the estimate's comparable_listings as failover)

    Raises AirROIError if the estimate call fails (including transport
    failures, which are wrapped) or if the merged comp pool is empty.
    """
    # Build location kwargs — prefer coords when available
    has_coords = (
        getattr(prop, "latitude", None) is not None
        and getattr(prop, "longitude", None) is not None
    )

    async with _new_client() as client:
        # /calculator/estimate ($0.20) ALREADY returns comparable_listings, and for
        # the same params that block is byte-identical to what /listings/comparables
        # ($0.10) returns — verified by sorting both payloads and diffing them. So
        # buy the estimate first and only pay for the comparables call when the
        # estimate's block comes back thin. Saves $0.10 on every healthy report and
        # keeps the failover for the thin case.
        if has_coords:
            estimate_data = await get_estimate(
                lat=float(prop.latitude), lng=float(prop.longitude),
                bedrooms=prop.bedrooms, baths=prop.bathrooms,
                guests=prop.max_guests, currency=currency, client=client,
            )
        else:
            estimate_data = await get_estimate(
                address=prop.address,
                bedrooms=prop.bedrooms, baths=prop.bathrooms,
                guests=prop.max_guests, currency=currency, client=client,
            )

        est_comps = estimate_data.get("comparable_listings") or []
        if len(est_comps) >= MIN_COMPS_FROM_ESTIMATE:
            comps_list: list = []       # estimate block is enough
        else:
            print(
                f"[airroi] estimate returned {len(est_comps)} comps "
                f"(< {MIN_COMPS_FROM_ESTIMATE}) — paying for /listings/comparables",
                file=sys.stderr,
            )
            loc_kw = (
                {"latitude": float(prop.latitude), "longitude": float(prop.longitude)}
                if has_coords else {"address": prop.address}
            )
            try:
                comps_list = await get_comparables(
                    bedrooms=prop.bedrooms, baths=prop.bathrooms,
                    guests=prop.max_guests, currency=currency, client=client,
                    **loc_kw,
                )
            except (AirROIError, httpx.HTTPError, asyncio.TimeoutError) as e:
                print(f"[airroi] warning: comparables failover failed: {e}", file=sys.stderr)
                comps_list = []

    # PRIORITY INVERTED 2026-09-20. The estimate's comparable_listings block is
    # now the PRIMARY source because the estimate is already paid for, and
    # /listings/comparables is the failover for a thin estimate. Both endpoints
    # return the same relevance-ranked set (proven by byte-diff), so this costs
    # nothing in fidelity and saves $0.10 per report. comps_list is empty on the
    # healthy path; the loop below is a no-op then.
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

    # Empty-market guard. /calculator/estimate happily returns a full revenue
    # payload for coordinates in the middle of the Pacific (rev $36,667, 0 comps).
    # With no comps there is nothing behind that number, so refuse to hand it
    # back as fact rather than letting the caller print it.
    if not merged:
        loc = estimate_data.get("location") or {}
        where = loc if isinstance(loc, str) else (
            getattr(prop, "address", None) or getattr(prop, "short_address", None) or "this location"
        )
        raise AirROIError(
            NO_HTTP_STATUS,
            f"no comparable listings found for {where} — AirROI returned an estimate "
            f"with an empty comp pool, so the revenue figure is unsupported. "
            f"Check the coordinates/address and the bedroom/bath/guest filters.",
            {"estimate_returned": True, "comps": 0},
        )

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
