"""On-disk TTL cache for paid vendor calls.

Every AirROI call costs money. Running the same property twice in a day — which
is what happens constantly while iterating on a report, debugging a thin comp
set, or testing a code change — paid full price every time. One session on a
single Bardstown property spent roughly $1.20 across three byte-identical runs
and produced no report at all.

Scope is deliberately narrow:
  * Only GET-shaped, side-effect-free vendor reads are cached.
  * Keyed on the full (endpoint, params) pair, so a different radius, bedroom
    count or currency is a different entry and can never be served a stale hit.
  * TTL defaults to 24h. STR performance data moves on a daily-to-weekly
    cadence, so a same-day repeat is the same answer.
  * Disabled with AIRROI_CACHE=0 or --no-cache, and any corrupt/unreadable
    entry is treated as a miss rather than an error. A cache must never be able
    to break a run that would otherwise work.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(os.getenv("AIRROI_CACHE_DIR", Path(__file__).parent.parent / ".cache" / "vendor"))
TTL_SECONDS = int(os.getenv("AIRROI_CACHE_TTL", str(24 * 60 * 60)))

_STATS = {"hit": 0, "miss": 0, "write": 0}


def enabled() -> bool:
    return os.getenv("AIRROI_CACHE", "1") not in {"0", "false", "no"}


def stats() -> dict:
    return dict(_STATS)


def _key(vendor: str, endpoint: str, params: dict | None) -> str:
    blob = json.dumps(
        {"v": vendor, "e": endpoint, "p": params or {}},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def get(vendor: str, endpoint: str, params: dict | None):
    """Return the cached payload, or None on miss/expiry/corruption."""
    if not enabled():
        return None
    f = CACHE_DIR / f"{_key(vendor, endpoint, params)}.json"
    try:
        rec = json.loads(f.read_text())
        if time.time() - rec["at"] > TTL_SECONDS:
            _STATS["miss"] += 1
            return None
        _STATS["hit"] += 1
        return rec["data"]
    except (FileNotFoundError, KeyError, ValueError, OSError):
        _STATS["miss"] += 1
        return None


def put(vendor: str, endpoint: str, params: dict | None, data) -> None:
    """Best-effort write. A cache failure must never fail the run."""
    if not enabled():
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = CACHE_DIR / f"{_key(vendor, endpoint, params)}.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "endpoint": endpoint, "data": data}))
        tmp.replace(f)          # atomic, so a killed run cannot leave a half file
        _STATS["write"] += 1
    except (OSError, TypeError, ValueError):
        pass
