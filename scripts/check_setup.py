#!/usr/bin/env python3
"""Verify everything the comping agent needs, before any paid run.

    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/check_setup.py

ensure_env.sh has already proven uv, Python 3.13 and the libraries (this runs
inside them). This checks the STR Secrets connections kit and the two keys the
comping agent cannot run without: AirROI (the comp data) and Firecrawl (street
addresses). Both are required (Ryan, 2026-09-28). Each key is tested with one
real request; a pass is remembered for 24 hours so a class of runs does not pay
for a check every time. Nothing here ever prints a key.

Exit codes, each with a line saying what Claude does next:
  0  ready
  2  the kit is set up but a key is missing or rejected: the kit's .env is
     opened for the student to paste into (never into the chat)
  3  no connections kit on this computer, or it was never run
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import kit  # noqa: E402

REQUIRED = ("AIRROI_API_KEY", "FIRECRAWL_API_KEY")
STAMP = kit.SETUP_STAMP   # agent.py forgets it when a vendor rejects the key mid-run
FRESH_FOR = 24 * 3600


def probe_airroi(key: str) -> str:
    """One $0.01 call, the same one the kit's checker makes (lib/probes.sh)."""
    return _probe("https://api.airroi.com/markets/search", {"X-API-KEY": key}, {"query": "miami"})


def probe_firecrawl(key: str) -> str:
    """Reads the credit balance, free; the kit's own probe."""
    return _probe("https://api.firecrawl.dev/v2/team/credit-usage", {"Authorization": f"Bearer {key}"})


def _probe(url: str, headers: dict, params: dict | None = None) -> str:
    try:
        r = httpx.get(url, headers=headers, params=params, timeout=20)
    except httpx.HTTPError:
        return "unreachable"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (401, 403):
        return "rejected"
    if r.status_code == 402:
        return "no credit"
    return f"error {r.status_code}"


PROBES = {"AIRROI_API_KEY": probe_airroi, "FIRECRAWL_API_KEY": probe_firecrawl}
WHERE = {
    "AIRROI_API_KEY": "https://www.airroi.com/api/developer (copy the API key; needs the $10 credit deposit)",
    "FIRECRAWL_API_KEY": "https://www.firecrawl.dev/app/api-keys (copy the API key)",
}


def _fingerprint(values: dict[str, str]) -> str:
    return hashlib.sha256("|".join(values[n] for n in REQUIRED).encode()).hexdigest()


def _recently_ok(values: dict[str, str]) -> bool:
    try:
        s = json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return s.get("fingerprint") == _fingerprint(values) and time.time() - s.get("at", 0) < FRESH_FOR


def other_copy(name: str) -> tuple[str, str]:
    """(value, where) for a key the kit's .env lacks but this computer has
    elsewhere: this folder's .env, the environment, or what the kit registered
    in ~/.claude.json. Adopting it into the kit's .env keeps one master copy
    without asking a student to paste a key they already gave."""
    value = kit.read_env(ROOT / ".env").get(name, "")
    if value:
        return value, "this folder's .env"
    value = os.environ.get(name, "").strip()
    if value:
        return value, "the environment"
    from config import key_from_connections_kit   # lazy: config loads .env and walks for the kit
    value = key_from_connections_kit(name)
    return (value, "~/.claude.json") if value else ("", "")


def open_for_paste(path: Path) -> None:
    """Open the file in the plain text editor; the student pastes and saves."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad.exe", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-e", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        print(f"  Could not open an editor. The file is: {path}")


def ensure_lines(env: Path, names) -> None:
    """Add an empty NAME= line for any required name the kit's .env lacks, so
    the student has a line to paste after. Never writes a value."""
    text = env.read_text(encoding="utf-8-sig") if env.exists() else ""
    have = kit.read_env(env)
    missing = [n for n in names if n not in have]
    if missing:
        sep = "" if (not text or text.endswith("\n")) else "\n"
        env.write_text(text + sep + "".join(f"{n}=\n" for n in missing), encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    no_open = "--no-open" in argv
    force = "--force" in argv
    found = kit.find_kit()
    if found is None or not (found / ".env").exists():
        where = f"found at {found} but never run" if found else "not found on this computer"
        print(f"[setup] Connections kit: {where}.")
        print("NEXT: set up the STR Secrets connections kit for them, then run this check again.")
        print(f"      Clone {kit.KIT_URL} next to this folder (skip if it is already there),")
        print("      read its CONNECTIONS.md and follow it: Phase 0, Phase 1, then the AirROI")
        print("      and Firecrawl rows (connectors/market-airroi.md, connectors/web-firecrawl.md).")
        print("      Nothing else in the kit is needed for comps.")
        return 3

    env = found / ".env"
    print(f"[setup] Connections kit: {found}")
    values = {n: kit.kit_value(n, found) for n in REQUIRED}
    for n in REQUIRED:
        if not values[n]:
            value, where = other_copy(n)
            if value:
                kit.set_value(env, n, value)
                values[n] = value
                print(f"[setup] {n}: blank in the kit's .env; copied in from {where} (value not shown).")
    blank = [n for n in REQUIRED if not values[n]]
    results: dict[str, str] = {}
    if not blank and not force and _recently_ok(values):
        results = {n: "ok" for n in REQUIRED}
        print("[setup] Keys: both passed a live check in the last 24 hours.")
    else:
        for n in REQUIRED:
            results[n] = "blank" if not values[n] else PROBES[n](values[n])

    for n in REQUIRED:
        mark = "OK  " if results[n] == "ok" else "!!  "
        print(f"  {mark}{n}: {results[n]}")

    bad = [n for n in REQUIRED if results[n] != "ok"]
    if not bad:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(json.dumps({"fingerprint": _fingerprint(values), "at": time.time()}), encoding="utf-8")
        print("[setup] READY: AirROI and Firecrawl both work.")
        return 0

    ensure_lines(env, bad)
    print(f"[setup] Opening {env} for the student.")
    for n in bad:
        print(f"  {n}: paste the key straight after the = sign on the {n}= line. Get it at {WHERE[n]}")
    print("NEXT: tell the student exactly that, one key at a time, and to save the file and say 'saved'.")
    print("      Keys go in the file, never in the chat. Then run this check again.")
    if any(results[n] == "unreachable" for n in bad):
        print("      A vendor was unreachable: check the internet connection before blaming the key.")
    if not no_open:
        open_for_paste(env)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
