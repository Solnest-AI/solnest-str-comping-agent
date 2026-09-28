"""Configuration loader — reads .env and branding.json, exposes typed settings."""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root. override=True so the .env wins over any empty
# values already in the shell environment (e.g., ANTHROPIC_API_KEY="" set globally).
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path, override=True)


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


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
        if host in (server.get("url") or ""):
            value = str((server.get("headers") or {}).get(header) or "").strip()
            if prefix and value.lower().startswith(prefix):
                value = value[len(prefix):].strip()
            elif prefix:
                value = ""
        value = value or str((server.get("env") or {}).get(name) or "").strip()
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
    for arg in server.get("args") or []:
        if not isinstance(arg, str) or not arg.endswith(".py"):
            continue
        env = Path(arg).parent / ".env"
        try:
            lines = env.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, ValueError):
            continue
        for line in lines:
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == name:
                v = v.strip().strip('"').strip("'")
                if v:
                    return v
    return ""


def _key(name: str) -> tuple[str, str]:
    """(value, where it came from): this folder's .env / environment first,
    then the connections kit's ~/.claude.json entry."""
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

# ── Airbtics (optional market-level overlay) ──
AIRBTICS_API_KEY: str = _get("AIRBTICS_API_KEY")
AIRBTICS_BASE_URL: str = _get(
    "AIRBTICS_BASE_URL",
    "https://crap0y5bx5.execute-api.us-east-2.amazonaws.com/prod",
)

# ── Anthropic (narrative generation) ──
ANTHROPIC_API_KEY: str = _get("ANTHROPIC_API_KEY")
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

# ── Ski resort seasonal template (Jan-Dec occupancy %) ──
# Used when AirROI / Airbtics monthly data is unavailable
SKI_RESORT_SEASONAL_TEMPLATE: list[float] = [
    82, 85, 78, 45, 38, 42, 48, 52, 40, 35, 50, 75
]


def ensure_firecrawl_configured() -> None:
    """Raise if Firecrawl API key is missing — required for property search."""
    if not FIRECRAWL_API_KEY:
        raise RuntimeError(
            "FIRECRAWL_API_KEY is not set, here or in the STR Secrets connections kit. "
            "Run the kit's Firecrawl row, or copy .env.example to .env and fill in the "
            "key. Get one at https://www.firecrawl.dev"
        )


def ensure_airroi_configured() -> None:
    """Raise if AirROI API key is missing — required for the main pipeline."""
    if not AIRROI_API_KEY:
        raise RuntimeError(
            "AIRROI_API_KEY is not set, here or in the STR Secrets connections kit. "
            "Run the kit's AirROI row, or copy .env.example to .env and fill in the "
            "key. Get one at https://www.airroi.com/api/developer/activate"
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
    return merged


BRANDING: dict = _load_branding()
