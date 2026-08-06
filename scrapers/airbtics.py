"""
Airbtics API client.

Airbtics provides market-level STR data (occupancy, ADR, revenue, percentiles)
for ~15M+ listings across 54K+ markets globally. Used here as an OPTIONAL
overlay that supplements AirROI's property-level data with market context.

Important: **Airbtics does not cover every market.** Smaller/niche markets
(e.g., Sun Peaks BC) return empty from `search_markets`. In that case all
functions here return `None` and the pipeline continues without overlay.
This is intentional — AirROI handles everything when Airbtics has no data.

Auth:
  AIRBTICS_API_KEY in .env (header: x-api-key)

Base URL:
  https://crap0y5bx5.execute-api.us-east-2.amazonaws.com/prod
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Optional
from urllib.parse import quote

import httpx

import config


# ── Exceptions ────────────────────────────────────────────────────────

class AirbticsError(RuntimeError):
    """Raised when the Airbtics API returns a non-2xx response (other than 404)."""

    def __init__(self, status: int, message: str, body: Optional[dict] = None):
        super().__init__(f"Airbtics API {status}: {message}")
        self.status = status
        self.message = message
        self.body = body or {}


# ── HTTP helper ───────────────────────────────────────────────────────

async def _request(
    path: str,
    *,
    method: str = "GET",
    body: Optional[dict] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Any:
    """Call Airbtics with auth header.

    Returns parsed JSON (unwrapped from the `{"message": ...}` envelope when
    present). Raises AirbticsError on non-404 failures. Returns None on 404
    since "market not found" is a valid, non-exceptional outcome.
    """
    if not config.AIRBTICS_API_KEY:
        raise AirbticsError(0, "AIRBTICS_API_KEY is not set in .env")

    url = config.AIRBTICS_BASE_URL.rstrip("/") + path
    headers = {"x-api-key": config.AIRBTICS_API_KEY, "Content-Type": "application/json"}

    if client is None:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as c:
            resp = await c.request(method, url, headers=headers, json=body)
    else:
        resp = await client.request(method, url, headers=headers, json=body)

    # 404 = "Market not found" — return None, don't raise
    if resp.status_code == 404:
        return None

    try:
        data = resp.json()
    except Exception as e:
        raise AirbticsError(resp.status_code, f"non-JSON response: {e}", {"text": resp.text[:500]})

    if resp.status_code >= 400:
        msg = data.get("message") if isinstance(data, dict) else str(data)
        raise AirbticsError(resp.status_code, str(msg), data if isinstance(data, dict) else {})

    # Airbtics wraps successful responses as {"message": <payload>, "metadata": {...}}.
    # Unwrap "message" and attach "metadata" to the payload if the payload is a dict.
    if isinstance(data, dict) and "message" in data:
        payload = data["message"]
        metadata = data.get("metadata")
        if metadata is not None and isinstance(payload, dict):
            payload["_metadata"] = metadata
        return payload
    return data


# ── Public API ─────────────────────────────────────────────────────────

async def search_markets(
    query: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[list[dict]]:
    """Search Airbtics markets by name (e.g. 'Sun Peaks', 'Tampa').

    Returns a list of market dicts with keys: id, name, region, country,
    country_code, verified. Returns an empty list (not None) when no markets
    match — callers should check `if not markets` rather than `is None`.
    """
    result = await _request(
        f"/markets/search?query={quote(query)}",
        method="GET",
        client=client,
    )
    if not isinstance(result, list):
        return []
    return result


async def lookup_market(
    latitude: float,
    longitude: float,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict]:
    """Find the market covering a lat/lng pair.

    Returns a market dict or None if no tracked market covers those coords.
    (A 404 from Airbtics is mapped to None — not an error.)
    """
    return await _request(
        f"/markets/lookup?latitude={latitude}&longitude={longitude}",
        method="GET",
        client=client,
    )


async def market_summary(
    market_id: str | int,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict]:
    """Pull KPIs for a market: occupancy, average_daily_rate, revenue,
    revenue_percentiles (p25/p50/p75/p90), active_listings_count, regulations.

    Market IDs come from `search_markets` or `lookup_market`.
    """
    return await _request(
        "/markets/summary",
        method="POST",
        body={"market_id": str(market_id)},
        client=client,
    )


async def market_metrics(
    market_id: str | int,
    months: int = 12,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[list[dict]]:
    """Historical monthly metrics for a market (up to 36 months).

    Returns a list of monthly dicts: {month, occupancy, average_daily_rate,
    revenue, ...}. Powers the seasonal chart when available.
    """
    result = await _request(
        "/markets/metrics/all",
        method="POST",
        body={"market_id": str(market_id), "months": int(months)},
        client=client,
    )
    if not isinstance(result, list):
        return []
    return result


async def search_listings(
    market_id: str | int,
    *,
    bedrooms: Optional[int] = None,
    min_bedrooms: Optional[int] = None,
    max_bedrooms: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[list[dict]]:
    """Search comp listings within a market, optionally filtered by bedroom count.

    Mostly redundant with AirROI comps — provided for completeness. Returns
    an empty list if the market has no listings or isn't tracked.
    """
    body: dict = {"market_id": str(market_id)}
    if bedrooms is not None:
        body["bedrooms"] = int(bedrooms)
    if min_bedrooms is not None:
        body["min_bedrooms"] = int(min_bedrooms)
    if max_bedrooms is not None:
        body["max_bedrooms"] = int(max_bedrooms)

    result = await _request(
        "/listings/search/market",
        method="POST",
        body=body,
        client=client,
    )

    # Airbtics occasionally double-wraps listings as a stringified JSON
    if isinstance(result, dict) and "listings" in result:
        listings = result["listings"]
        if isinstance(listings, str):
            try:
                import json
                listings = json.loads(listings)
            except (ValueError, TypeError):
                return []
        if isinstance(listings, dict) and "message" in listings:
            listings = listings["message"]
        return listings if isinstance(listings, list) else []

    return result if isinstance(result, list) else []


async def listing_metrics(
    listing_id: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[dict]:
    """Detailed per-listing metrics by Airbtics listing ID."""
    return await _request(
        f"/listings/metrics/all?listing_id={listing_id}",
        method="GET",
        client=client,
    )


# ── High-level convenience ─────────────────────────────────────────────

async def get_market_overlay(
    market_query: str,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    months: int = 12,
) -> Optional[dict]:
    """Best-effort market overlay for a subject location. Returns None silently
    if Airbtics doesn't track the market (e.g., Sun Peaks BC).

    Strategy:
      1. Try lookup_market by lat/lng (most precise)
      2. Fall back to search_markets by name
      3. If still nothing, return None — the pipeline proceeds without overlay

    Returns a dict: {market, summary, metrics} when successful.
      - market:  {id, name, region, country, ...}
      - summary: {occupancy, average_daily_rate, revenue, ...}
      - metrics: list of up-to-`months` monthly snapshots
    """
    if not config.AIRBTICS_API_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            # Strategy 1: coord lookup
            market = None
            if latitude is not None and longitude is not None:
                market = await lookup_market(latitude, longitude, client=client)

            # Strategy 2: name search
            if not market:
                matches = await search_markets(market_query, client=client)
                if matches:
                    # Prefer exact-name match; otherwise take the first result
                    q_lower = (market_query or "").lower().strip()
                    market = next(
                        (m for m in matches if q_lower in (m.get("name", "") or "").lower()),
                        matches[0],
                    )

            if not market or not market.get("id"):
                return None

            # Strategy 3: pull summary + metrics in parallel
            summary_task = market_summary(market["id"], client=client)
            metrics_task = market_metrics(market["id"], months=months, client=client)
            summary, metrics = await asyncio.gather(summary_task, metrics_task)

            return {
                "market":  market,
                "summary": summary or {},
                "metrics": metrics or [],
            }

    except AirbticsError as e:
        # Surface the error but don't break the pipeline
        print(f"[airbtics] overlay failed: {e}", file=sys.stderr)
        return None


# ── CLI smoke test ────────────────────────────────────────────────────

async def _smoke_test():
    """Manual smoke test: `python -m scrapers.airbtics`."""

    print("[smoke] Airbtics search — 'Sun Peaks' (expect: empty, untracked market)...")
    result = await search_markets("Sun Peaks")
    print(f"  Got {len(result or [])} markets. Expected 0.")

    print("\n[smoke] Airbtics search — 'Tampa' (expect: results)...")
    result = await search_markets("Tampa")
    print(f"  Got {len(result or [])} markets")
    if result:
        for m in result[:3]:
            print(f"    [{m.get('id')}] {m.get('name')} — {m.get('region')}, {m.get('country')}")

    print("\n[smoke] High-level overlay — Sun Peaks (expect: None)...")
    overlay = await get_market_overlay("Sun Peaks")
    print(f"  Overlay: {overlay}")

    print("\n[smoke] High-level overlay — Tampa (expect: populated)...")
    overlay = await get_market_overlay("Tampa")
    if overlay:
        s = overlay.get("summary") or {}
        print(f"  Market: {overlay.get('market', {}).get('name')}")
        print(f"  Active listings: {s.get('active_listings_count')}")
        print(f"  Avg occupancy:   {s.get('occupancy')}%")
        print(f"  Avg ADR:         ${s.get('average_daily_rate')}")
        print(f"  Monthly metrics: {len(overlay.get('metrics') or [])} rows")
    else:
        print("  overlay: None (market untracked)")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
