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


# ── AirROI (primary STR data provider — listings, comps, calculator, markets) ──
AIRROI_API_KEY: str = _get("AIRROI_API_KEY")
AIRROI_BASE_URL: str = _get("AIRROI_BASE_URL", "https://api.airroi.com")

# ── Firecrawl (universal web scraping for property search) ──
FIRECRAWL_API_KEY: str = _get("FIRECRAWL_API_KEY")
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
            "FIRECRAWL_API_KEY is not set. Copy .env.example to .env and fill in the key. "
            "Get one at https://www.firecrawl.dev"
        )


def ensure_airroi_configured() -> None:
    """Raise if AirROI API key is missing — required for the main pipeline."""
    if not AIRROI_API_KEY:
        raise RuntimeError(
            "AIRROI_API_KEY is not set. Copy .env.example to .env and fill in the key. "
            "Get one at https://www.airroi.com/api/developer/activate"
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
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if v not in (None, "")})
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] Could not read {path.name} ({e}); using default branding.")
    return merged


BRANDING: dict = _load_branding()
