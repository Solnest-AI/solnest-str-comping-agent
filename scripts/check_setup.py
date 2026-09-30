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
  2  a key is not working. Blank or rejected: the kit's .env is opened for the
     student to paste into (never into the chat). Out of credit: top up at the
     vendor, the key is fine. Rate limited, vendor error or unreachable: not a
     key problem, wait and run the check again; no editor is opened
  3  no connections kit on this computer, or it was never run
  4  keys ready, but no usable branding.json yet (missing, not valid JSON,
     or still a placeholder name): ask the student for their website
     and run scripts/brand_from_website.py, so the first report carries their
     name, logo and colours instead of a placeholder
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
BRANDING = ROOT / "branding.json"
STAMP = kit.SETUP_STAMP   # agent.py forgets it when a vendor rejects the key mid-run
FRESH_FOR = 24 * 3600


def probe_airroi(key: str) -> str:
    """One $0.01 call, the same one the kit's checker makes (lib/probes.sh)."""
    return _probe("https://api.airroi.com/markets/search", {"X-API-KEY": key}, {"query": "miami"})


def probe_firecrawl(key: str) -> str:
    """Reads the credit balance, free; the kit's own probe. A 200 only proves
    the key: a zero balance would pass here and then fail on the student's
    first branding or address lookup, so the balance is read too."""
    return _probe("https://api.firecrawl.dev/v2/team/credit-usage", {"Authorization": f"Bearer {key}"},
                  judge=_firecrawl_balance)


def _firecrawl_balance(r: httpx.Response) -> str:
    """ok for a positive balance. Anything the vendor did not clearly say is
    unverified, never ok, so it is not remembered as a 24-hour pass."""
    try:
        body = r.json()
    except ValueError:
        return "unverified (unreadable reply)"
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("success") is False or not isinstance(data, dict):
        return "unverified (unexpected reply)"
    credits = data.get("remainingCredits", data.get("remaining_credits"))
    if isinstance(credits, bool) or not isinstance(credits, (int, float)):
        return "unverified (no balance in reply)"
    return "ok" if credits > 0 else "no credit"


def _probe(url: str, headers: dict, params: dict | None = None, judge=None) -> str:
    try:
        r = httpx.get(url, headers=headers, params=params, timeout=20)
    except httpx.HTTPError:
        return "unreachable"
    if r.status_code == 200:
        return judge(r) if judge else "ok"
    if r.status_code in (401, 403):
        return "rejected"
    if r.status_code == 402:
        return "no credit"
    if r.status_code == 429:
        return "rate limited"
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
    # A corrupt stamp is a cache miss, never a crash: recheck for real.
    if not isinstance(s, dict) or not isinstance(s.get("at"), (int, float)):
        return False
    return s.get("fingerprint") == _fingerprint(values) and 0 <= time.time() - s["at"] < FRESH_FOR


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
    others = [d for d in kit.find_kits() if d != found and kit.keys_filled(d)]
    if others and not os.environ.get("STR_SECRETS_KIT"):
        print("[setup] Other copies of the kit with keys in them (values not shown):")
        for d in others:
            print(f"        {d}")
        print("      Using the one above. If the student set up a different one, run this check")
        print("      again with STR_SECRETS_KIT=\"<that folder>\" in front; it is remembered after.")
    values = {n: kit.kit_value(n, found) for n in REQUIRED}
    for n in REQUIRED:
        if not values[n]:
            value, where = other_copy(n)
            if not value:
                continue
            try:
                kit.set_value(env, n, value)
            except (ValueError, OSError):
                print(f"[setup] {n}: found in {where} but could not copy it into the kit's .env.")
                continue
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
        kit.remember_kit(found)   # later runs keep using this kit, whatever else gets downloaded
        print("[setup] Keys: AirROI and Firecrawl both work.")
        from config import BRANDING_ASK, branding_problems   # lazy: config walks for the kit on import
        problems = branding_problems(BRANDING)
        if problems:
            if BRANDING.exists():
                print("[setup] Branding: branding.json is not usable yet: " + "; ".join(problems) + ".")
                print("      Fix those fields with the student (a name alone is fine), or rebuild it:")
            else:
                print("[setup] Branding: not set yet. The report would say 'Your Company' with no logo.")
            print("\n".join(BRANDING_ASK))
            print("      Look at the logo it saves and confirm the name, logo and colours with them.")
            print("      No website? Copy branding.example.json to branding.json and fill it in with them.")
            return 4
        print("[setup] READY: keys work and the report is branded.")
        return 0

    STAMP.unlink(missing_ok=True)   # a failed live check must not leave an old READY standing
    # Only a blank or rejected key is fixed by pasting one. Out of credit, rate
    # limiting and vendor errors say nothing against the key: opening Notepad
    # for those sends the student to replace a working key.
    needs_key = [n for n in bad if results[n] in ("blank", "rejected")]
    if needs_key:
        try:
            kit.add_blank_lines(env, needs_key)   # atomic: the kit's .env is the master copy
        except (ValueError, OSError) as e:
            print(f"[setup] Could not add the blank key line(s) to the kit's .env: {e}")
        print(f"[setup] Opening {env} for the student.")
    for n in bad:
        if n in needs_key:
            print(f"  {n}: paste the key straight after the = sign on the {n}= line. Get it at {WHERE[n]}")
        elif results[n] == "no credit":
            print(f"  {n}: the key is fine but the vendor account is out of credit. "
                  f"Top up at {WHERE[n]}; no new key needed.")
        else:
            print(f"  {n}: the vendor answered '{results[n]}', which is not a key problem. "
                  f"Wait a minute, then run this check again; the key stays as it is.")
    if needs_key:
        print("NEXT: tell the student exactly that, one key at a time, and to save the file and say 'saved'.")
        print("      Keys go in the file, never in the chat. Then run this check again.")
    if any(results[n] == "no credit" for n in bad):
        print("NEXT: tell the student which account is out of credit and to top it up at the vendor; "
              "the key stays. Then run this check again.")
    if any(results[n] in ("rate limited", "unreachable") or results[n].startswith(("error", "unverified"))
           for n in bad):
        print("NEXT: no key change needed. Check the internet connection, wait a minute, run this check again.")
    if needs_key and not no_open:
        open_for_paste(env)
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(sys.argv[1:]))
