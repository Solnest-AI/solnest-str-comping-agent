"""Real estate agent profiles for co-branded reports (S2-027).

A student who sends reports to agents' buyers wants the agent's brand on the
report next to their own. Each agent is one file, partners/<agent>.json,
written by scripts/add_partner.py from what the student says and picked per
report with `agent.py --partner <agent>`.

The files hold a real person's phone and email, so partners/ is gitignored and
never shipped (scripts/package.py). Every field lands in the report's HTML:
links must be http(s), the logo plate a #rrggbb colour, and unknown keys are
refused so a typo ("brokrage") is caught instead of silently dropped.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from generators.narrative_brief import slugify
from schema import PartnerBrand

PARTNERS_DIR = Path(__file__).parent / "partners"

FIELDS = ("name", "brokerage", "phone", "email", "website_url", "logo_url", "logo_background")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_EMAIL = re.compile(r"[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+")
_PHONE = re.compile(r"[0-9+()\-.\s xX]+")
MAX_LEN = 120


class PartnerError(Exception):
    """A partner profile is missing, unreadable or invalid. The message is
    written for Claude to act on and names every problem."""


def partner_slug(name: str) -> str:
    return slugify(name).lower()


def list_partners(partners_dir: Path | None = None) -> list[str]:
    d = Path(partners_dir or PARTNERS_DIR)
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def partner_problems(data) -> list[str]:
    """Everything wrong with one profile, one line each. Empty means usable."""
    if not isinstance(data, dict):
        return ['a profile must be one JSON object: { "name": "..." }']
    problems = [f"unknown field {k!r} (allowed: {', '.join(FIELDS)})"
                for k in data if k not in FIELDS]
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("name is missing or blank: the agent's name is required")
    for key in FIELDS:
        value = data.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            problems.append(f"{key} must be text in quotes")
            continue
        value = value.strip()
        if len(value) > MAX_LEN:
            problems.append(f"{key} is longer than {MAX_LEN} characters")
        elif key.endswith("_url") and not value.lower().startswith(("https://", "http://")):
            problems.append(f"{key} must start with https://")
        elif key == "logo_background" and not _HEX.fullmatch(value):
            problems.append(f"logo_background {value!r} must be a colour like #1f3c34")
        elif key == "email" and not _EMAIL.fullmatch(value):
            problems.append(f"email {value!r} is not an email address")
        elif key == "phone" and not _PHONE.fullmatch(value):
            problems.append(f"phone {value!r} may hold only digits, spaces and + ( ) - .")
    return problems


def _resolve(ref: str, partners_dir: Path) -> Path:
    ref = ref.strip()
    if ref.lower().endswith(".json") or "/" in ref or "\\" in ref:
        return Path(ref).expanduser()
    return partners_dir / f"{partner_slug(ref)}.json"


def load_partner(ref: str, partners_dir: Path | None = None) -> PartnerBrand:
    """The agent named `ref` (a saved slug, the agent's name, or a path)."""
    d = Path(partners_dir or PARTNERS_DIR)
    path = _resolve(ref, d)
    if not path.is_file():
        saved = list_partners(d)
        have = (f"Saved agents: {', '.join(saved)}." if saved
                else "No agents are saved yet.")
        raise PartnerError(
            f"No agent profile called {ref!r} ({path}). {have} Add one with "
            f'scripts/add_partner.py --name "<agent name>" (ask the student for the details).'
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise PartnerError(f"{path.name} is not valid JSON ({e})") from e
    problems = partner_problems(data)
    if problems:
        raise PartnerError(f"{path.name} cannot be used: " + "; ".join(problems))
    return PartnerBrand(**{k: str(data.get(k) or "").strip() for k in FIELDS})


def save_partner(data: dict, partners_dir: Path | None = None) -> Path:
    """Write a validated profile and return its path. Caller validates first."""
    d = Path(partners_dir or PARTNERS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{partner_slug(data['name'])}.json"
    clean = {k: str(data.get(k) or "").strip() for k in FIELDS}
    path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return path
