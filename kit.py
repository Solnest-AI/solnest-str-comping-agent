"""Find the STR Secrets connections kit and read keys from its .env.

The kit (github.com/Solnest-AI/str-secrets-connections) is where an attendee's
keys live, and its .env is the master copy: the comping agent reads it directly
instead of keeping a second copy that can drift. Search order and markers match
the Listing Optimizer's scripts/kit_link.py, so every summit skill finds the
same kit. Nothing here ever prints a value.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KIT_URL = "https://github.com/Solnest-AI/str-secrets-connections"
KIT_MARKERS = ("CONNECTIONS.md", "fan-out-env.sh")
# Spaces around the = are tolerated because the kit's env_load tolerates them:
# a key the kit reads as set must not read as blank here, or check_setup would
# append an empty duplicate line that blanks the key for the kit too.
_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_SKIP = {".git", ".venv", "node_modules", "__pycache__", "output", "Library", "AppData"}


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, quotes and whitespace stripped, like the kit's lib/env.sh."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        m = _LINE.match(line.strip())
        if not m:
            continue
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[m.group(1)] = val
    return values


def is_kit(d: Path) -> bool:
    return all((d / m).is_file() for m in KIT_MARKERS)


def _walk(root: Path, depth: int):
    if depth < 0 or not root.is_dir():
        return
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for c in children:
        try:
            if not c.is_dir() or c.name.startswith(".") or c.name in _SKIP:
                continue
        except OSError:
            continue
        if is_kit(c):
            yield c
        else:
            yield from _walk(c, depth - 1)


def find_kit(home: Path | None = None, near: Path = ROOT) -> Path | None:
    """The kit folder, or None. $STR_SECRETS_KIT wins, then next to this folder,
    Desktop, Documents, Downloads (and their OneDrive copies), home; 2 levels deep.
    A kit that has been run (has a .env) beats an unused download."""
    env = os.environ.get("STR_SECRETS_KIT")
    if env and is_kit(Path(env).expanduser()):
        return Path(env).expanduser().resolve()
    home = home or Path.home()
    roots = [near.parent, home / "Desktop", home / "Documents", home / "Downloads",
             home / "OneDrive" / "Desktop", home / "OneDrive" / "Documents", home]
    found: list[Path] = []
    for r in roots:
        for d in ([r] if is_kit(r) else []) + list(_walk(r, 2)):
            d = d.resolve()
            if d not in found:
                found.append(d)
    if not found:
        return None
    found.sort(key=lambda d: ((d / ".env").exists(),
                              (d / ".env").stat().st_mtime if (d / ".env").exists() else 0),
               reverse=True)
    return found[0]


def kit_value(name: str, kit: Path | None) -> str:
    return read_env(kit / ".env").get(name, "") if kit else ""


def set_value(env: Path, name: str, value: str) -> None:
    """Set NAME=value in the kit's .env: on NAME's LAST line (the one the kit's
    env_load and read_env use), else appended. Touches no other line and never
    prints the value. Written to a temp file and swapped in, so a crash or a
    full disk cannot leave the master copy half-written."""
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{name}: refusing to write an empty or multi-line value")
    raw = env.read_bytes() if env.exists() else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    eol = "\r\n" if b"\r\n" in raw else "\n"   # keep the file's own line endings
    lines = raw.decode("utf-8-sig").splitlines()
    hits = [i for i, line in enumerate(lines)
            if (m := _LINE.match(line.strip())) and m.group(1) == name]
    if hits:
        lines[hits[-1]] = f"{name}={value}"
    else:
        lines.append(f"{name}={value}")
    tmp = env.with_name(env.name + ".tmp")
    tmp.write_text(eol.join(lines) + eol, encoding="utf-8-sig" if bom else "utf-8", newline="")
    os.replace(tmp, env)


# ── The 24-hour "keys passed" stamp written by scripts/check_setup.py ──
SETUP_STAMP = ROOT / ".cache" / "setup_ok.json"
# What a vendor means by these during a run: the key, not the property.
KEY_FAILURES = {401: "rejected the key", 402: "is out of credit", 403: "rejected the key"}


class KeyFailure(BaseException):
    """A vendor rejected the key or is out of credit. Not the property's fault,
    so no fallback may absorb it: it derives from BaseException, which every
    `except Exception` / `except AirROIError` degrade path lets through, and
    (unlike SystemExit) asyncio.gather propagates it like any error. agent.py
    catches it once, at the top, and stops the run. The message never holds
    the key or the vendor's response body."""

    def __init__(self, vendor: str, status: int):
        super().__init__(f"[setup] {vendor} {KEY_FAILURES[status]} (HTTP {status}). Not a problem "
                         "with the property. Run: PY=\"$(bash scripts/ensure_env.sh)\" && "
                         "\"$PY\" scripts/check_setup.py")
        self.vendor = vendor
        self.status = status


def check_key_status(vendor: str, status: int) -> None:
    """Call on every vendor HTTP response: raises KeyFailure for 401/402/403."""
    if status in KEY_FAILURES:
        raise KeyFailure(vendor, status)


def forget_setup_pass() -> None:
    """Drop the 24-hour pass so the next check_setup probes for real."""
    try:
        SETUP_STAMP.unlink()
    except OSError:
        pass
