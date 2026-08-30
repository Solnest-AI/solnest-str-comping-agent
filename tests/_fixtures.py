"""Shared test helpers — live AirROI fixture loading.

Imported as `from _fixtures import ...` (tests/conftest.py puts tests/ on sys.path).

`tests/fixtures/` holds real AirROI responses captured 2026-08-29:
  comps_<market>.json  — 6 markets x 25 listings = 150 live records
  listing_detail.json  — GET /listings?id=
  metrics.json         — GET /listings/metrics/all
  amenity_vocab.json   — the 136 distinct amenity display strings in the corpus

These are checked in on purpose. Before this, zero tests parsed a real API
response and the suite stayed green while the tool blocked reports in 5 of 6
markets. Synthetic fixtures can only encode what we already believe.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Markets in the captured corpus, in filename order.
MARKETS = ["coords", "destin", "gatlinburg", "nashville", "scottsdale", "sunpeaks"]


def fixture_path(name: str) -> Path:
    p = FIXTURE_DIR / name
    if not p.exists():
        raise FileNotFoundError(f"missing test fixture: {p}")
    return p


@functools.lru_cache(maxsize=None)
def load_json(name: str):
    with fixture_path(name).open(encoding="utf-8") as fh:
        return json.load(fh)


def comps_response(market: str) -> dict:
    """Raw `GET /listings/comparables` response for one market."""
    return load_json(f"comps_{market}.json")


def market_listings(market: str) -> list[dict]:
    """The 25 raw AirROI listing dicts for one market."""
    return list(comps_response(market)["listings"])


def all_live_listings() -> list[dict]:
    """All 150 raw AirROI listing dicts across the 6 captured markets."""
    out: list[dict] = []
    for m in MARKETS:
        out.extend(market_listings(m))
    return out


def amenity_vocabulary() -> set[str]:
    """The 136 amenity display strings that actually occur in AirROI data."""
    return set(load_json("amenity_vocab.json"))


def pearson(xs, ys) -> float:
    """Pearson correlation. Raises on degenerate input rather than returning 0."""
    n = len(xs)
    if n != len(ys) or n < 2:
        raise ValueError("pearson needs two equal-length series of length >= 2")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    if den == 0:
        raise ValueError("pearson: zero variance in one series")
    return num / den
