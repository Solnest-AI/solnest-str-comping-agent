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
