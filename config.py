"""Configuration loader — reads .env and branding.json, exposes typed settings."""

import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv

import kit

# Load .env from project root. override=True so the .env wins over any empty
# values already in the shell environment (e.g., ANTHROPIC_API_KEY="" set globally).
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path, override=True)


def _get(key: str, default: str = "") -> str:
    """A setting, or the default when it is unset or blank (`NARRATIVE_MODEL=`)."""
    return (os.getenv(key) or "").strip() or default


def key_from_connections_kit(name: str, claude_json: Path | None = None) -> str:
    """A key the STR Secrets connections kit already collected, or "".

    The kit (github.com/Solnest-AI/str-secrets-connections, summit pre-work)
    registers the AirROI and Firecrawl MCP servers in the top-level
    "mcpServers" of ~/.claude.json with the key in a header
    (lib/mcp_register.py): airroi-official -> X-API-KEY, firecrawl ->
    "Authorization: Bearer <key>". Attendees ran it before this folder
    existed, so its .env fan-out never reached us and they would otherwise be
    asked for keys they already gave. Read-only; the value is never printed.
    """
    path = claude_json or (Path.home() / ".claude.json")
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    if not isinstance(servers, dict):
        return ""
    host, header, prefix = {
        "AIRROI_API_KEY": ("airroi.com", "X-API-KEY", ""),
        "FIRECRAWL_API_KEY": ("firecrawl.dev", "Authorization", "bearer "),
    }.get(name, ("", "", ""))
    if not host:
        return ""
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        value = ""
        url, headers, env = server.get("url"), server.get("headers"), server.get("env")
        if isinstance(url, str) and host in url:
            value = str((headers if isinstance(headers, dict) else {}).get(header) or "").strip()
            if prefix and value.lower().startswith(prefix):
                value = value[len(prefix):].strip()
            elif prefix:
                value = ""
        value = value or str((env if isinstance(env, dict) else {}).get(name) or "").strip()
        value = value or _key_beside_stdio_server(server, name)
        # A literal variable name or ${...} is a template, not a key.
        if value and value != name and not value.startswith("$"):
            return value
    return ""


def _key_beside_stdio_server(server: dict, name: str) -> str:
    """The key from the .env next to a bundled stdio server's script.

    The kit's AirROI "Path A" registers its bundled server (airroi -> python
    .../mcp-servers/airroi/server.py) and fan-out-env.sh puts the key in that
    folder's .env, not in ~/.claude.json. Attendees who skipped "Path B"
    (airroi-official, key in a header) have only this copy. Found 2026-09-27
    on a Windows laptop with a working key that the agent reported missing.
    """
    args = server.get("args")
    for arg in args if isinstance(args, list) else []:
        if not isinstance(arg, str) or not arg.endswith(".py"):
            continue
        value = kit.read_env(Path(arg).parent / ".env").get(name, "")
        if value:
            return value
    return ""


KIT_DIR = kit.find_kit()


def _kit_key(name: str) -> str:
    return kit.kit_value(name, KIT_DIR)


def _key(name: str) -> tuple[str, str]:
    """(value, where it came from). The connections kit's own .env is the master
    copy (Ryan, 2026-09-28: one place to paste, one place to fix), then this
    folder's .env / environment, then what the kit registered in ~/.claude.json."""
    value = _kit_key(name)
    if value:
        return value, "connections kit (.env)"
    value = _get(name)
    if value:
        return value, ".env"
    value = key_from_connections_kit(name)
    return (value, "connections kit (~/.claude.json)") if value else ("", "")


# ── AirROI (primary STR data provider — listings, comps, calculator, markets) ──
AIRROI_API_KEY, AIRROI_KEY_SOURCE = _key("AIRROI_API_KEY")
AIRROI_BASE_URL: str = _get("AIRROI_BASE_URL", "https://api.airroi.com")

# ── Firecrawl (universal web scraping for property search) ──
FIRECRAWL_API_KEY, FIRECRAWL_KEY_SOURCE = _key("FIRECRAWL_API_KEY")
FIRECRAWL_BASE_URL: str = _get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v1")

# ── Anthropic (narrative generation) ──
ANTHROPIC_API_KEY: str = _get("ANTHROPIC_API_KEY")
# The API path is opt-in. Claude Code writes the copy (the narrative handoff);
# a key the student happens to have in their environment must not silently
# switch that off. Set NARRATIVE_MODE=api for headless platform runs.
NARRATIVES_VIA_API: bool = _get("NARRATIVE_MODE").lower() == "api"
# Default is the current-generation Sonnet. Measured on a real report:
#   claude-sonnet-4-6          35.7s   $0.025/report
#   claude-sonnet-5            18.5s   $0.025/report   <- same cost, 1.9x faster
#   claude-haiku-4-5-20251001  15.4s   $0.008/report   <- 3x cheaper, lighter prose
# Set NARRATIVE_MODEL in .env to override (e.g. Haiku for high-volume runs).
NARRATIVE_MODEL: str = _get("NARRATIVE_MODEL", "claude-sonnet-5")

# ── Gmail SMTP (optional) ──
GMAIL_ADDRESS: str = _get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD: str = _get("GMAIL_APP_PASSWORD")

# ── Output ──
OUTPUT_DIR: Path = Path(_get("OUTPUT_DIR", "./output"))

# ── HTTP ──
HTTP_TIMEOUT: int = 30


def ensure_firecrawl_configured() -> None:
    """Raise if Firecrawl API key is missing — required for property search."""
    if not FIRECRAWL_API_KEY:
        raise RuntimeError(
            "FIRECRAWL_API_KEY is not set, here or in the STR Secrets connections kit. "
            "Run scripts/check_setup.py: it opens the kit's .env to paste the key into. "
            "Get one at https://www.firecrawl.dev/app/api-keys"
        )


def ensure_airroi_configured() -> None:
    """Raise if AirROI API key is missing — required for the main pipeline."""
    if not AIRROI_API_KEY:
        raise RuntimeError(
            "AIRROI_API_KEY is not set, here or in the STR Secrets connections kit. "
            "Run scripts/check_setup.py: it opens the kit's .env to paste the key into. "
            "Get one at https://www.airroi.com/api/developer"
        )


# ── Branding ──────────────────────────────────────────────────────────
# The report is white-label. Copy branding.example.json to branding.json and
# edit it; branding.json is gitignored so your identity never ships with the
# code, and the example provides neutral defaults for anyone who skips it.
_BRANDING_PATH = Path(__file__).parent / "branding.json"
_BRANDING_EXAMPLE = Path(__file__).parent / "branding.example.json"

_BRANDING_DEFAULTS = {
    "company_name": "STR Income Analysis",
    "tagline": "Short-Term Rental Analysis",
    "logo_url": "",
    "website_url": "",
    "primary_color": "#1f3c34",
    "accent_color": "#4b7c6b",
    "logo_background": "",
}


def _load_branding() -> dict:
    """Load branding.json, falling back to the example, then to defaults.

    Never raises: a malformed branding file degrades to defaults rather than
    taking down a report run.
    """
    merged = dict(_BRANDING_DEFAULTS)
    path = _BRANDING_PATH if _BRANDING_PATH.exists() else _BRANDING_EXAMPLE
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if v not in (None, "")})
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] Could not read {path.name} ({e}); using default branding.")
    # Branding is scraped from a student's website (scripts/brand_from_website.py)
    # and lands inside the report's CSS and chart JS: a colour must be #rrggbb and
    # a link must be http(s), or that field falls back to the default.
    for key in ("primary_color", "accent_color"):
        if not _HEX.fullmatch(str(merged.get(key, ""))):
            print(f"[config] branding {key} {merged.get(key)!r} is not #rrggbb; using the default.")
            merged[key] = _BRANDING_DEFAULTS[key]
    if merged.get("logo_background") and not _HEX.fullmatch(str(merged["logo_background"])):
        merged["logo_background"] = ""
    for key in ("logo_url", "website_url"):
        if merged.get(key) and not str(merged[key]).lower().startswith(("https://", "http://")):
            print(f"[config] branding {key} is not an http(s) link; leaving it out.")
            merged[key] = ""
    return merged


_HEX = re.compile(r"#[0-9a-fA-F]{6}")


# Names that mean nobody branded the report: the example's and the defaults'.
_PLACEHOLDER_NAMES = {"your company", _BRANDING_DEFAULTS["company_name"].lower()}


def branding_problems(path: Path | None = None) -> list[str]:
    """What stops branding.json from being a real brand, one line per field.
    Empty means ready. Shared by scripts/check_setup.py and the run's own gate,
    so a file that setup calls READY is never rendered as a default identity.
    A name alone is a complete brand: logo, website and colours are optional."""
    path = path or _BRANDING_PATH
    if not path.exists():
        return ["branding.json does not exist yet"]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return [f"branding.json is not valid JSON ({e})"]
    if not isinstance(data, dict):
        return ["branding.json must be one JSON object: { \"company_name\": \"...\", ... }"]
    problems = []
    name = data.get("company_name")
    if not isinstance(name, str) or not name.strip():
        problems.append("company_name is missing or blank")
    elif name.strip().lower() in _PLACEHOLDER_NAMES:
        problems.append(f"company_name is still the placeholder {name.strip()!r}")
    for key in ("tagline", "logo_url", "website_url", "primary_color", "accent_color", "logo_background"):
        value = data.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            problems.append(f"{key} must be text in quotes")
        elif key.endswith(("_color", "_background")) and not _HEX.fullmatch(value):
            problems.append(f"{key} {value!r} must be a colour like #1f3c34")
        elif key.endswith("_url") and not value.lower().startswith(("https://", "http://")):
            problems.append(f"{key} must start with https://")
    return problems


def branding_is_placeholder() -> bool:
    """True until branding.json holds a real brand: the report would carry a
    placeholder name. The skill asks for the student's website before the
    first report (scripts/brand_from_website.py)."""
    return bool(branding_problems())


BRANDING: dict = _load_branding()
