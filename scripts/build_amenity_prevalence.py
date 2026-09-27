#!/usr/bin/env python3
"""Build data/amenity_prevalence.json: how common each AirROI amenity string is.

Usage:
    python scripts/build_amenity_prevalence.py

Reads every comparable-listing pool on disk (tests/fixtures/comps_*.json plus
the local AirROI response cache in .cache/vendor/), de-duplicates listings by
id, and records the share of listings carrying each amenity. The scorer uses
it to weight amenity overlap by rarity: an amenity nearly every listing has
(Wifi, Smoke alarm) says nothing about whether two listings are alike, a rare
one (Boat slip, Sauna) says a lot.

Why rarity and not revenue lift: measured 2026-09-26 on 375 listings in 15
distinct pools, within-pool revenue lift ranked "Hangers" and "Shampoo" level
with "Hot tub" and put "Pool" at zero. That is hosts who fill in every
checkbox also running better listings, not guests paying for hangers; the
sample cannot separate the two. Rarity is a property of the data that can be
measured honestly. The premium features that drive price are handled
separately in comp_similarity.PREMIUM_FEATURES.

The output is aggregate counts only, no listing data, so it is safe to commit.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "amenity_prevalence.json"


def _listings(payload) -> list:
    if isinstance(payload, dict):
        for key in ("listings", "comparable_listings"):
            if isinstance(payload.get(key), list):
                return payload[key]
        if isinstance(payload.get("data"), dict):
            return _listings(payload["data"])
    return []


def main() -> None:
    sources = sorted(ROOT.glob("tests/fixtures/comps_*.json")) + sorted(
        (ROOT / ".cache" / "vendor").glob("*.json"))
    by_id: dict = {}
    for path in sources:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for comp in _listings(payload):
            lid = (comp.get("listing_info") or {}).get("listing_id")
            amenities = (comp.get("property_details") or {}).get("amenities")
            if lid and amenities:
                by_id[lid] = set(amenities)

    n = len(by_id)
    if n < 100:
        raise SystemExit(f"Only {n} listings found; refusing to write a table this thin.")
    counts = Counter(a for amenities in by_id.values() for a in amenities)
    table = {
        "_note": ("Share of AirROI comparable listings carrying each amenity. "
                  "Built by scripts/build_amenity_prevalence.py."),
        "generated": _dt.date.today().isoformat(),
        "n_listings": n,
        "prevalence": {a: round(c / n, 4) for a, c in sorted(counts.items())},
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(table, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(counts)} amenities across {n} listings")


if __name__ == "__main__":
    main()
