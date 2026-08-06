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

# ── Apify (reliable Zillow scraping — primary for property search) ──
APIFY_TOKEN: str = _get("APIFY_TOKEN")
APIFY_ZILLOW_ACTOR_ID: str = _get("APIFY_ZILLOW_ACTOR_ID", "ENK9p4RZHg0iVso52")

# ── Google Maps (Street View hero images for off-market properties) ──
GOOGLE_MAPS_API_KEY: str = _get("GOOGLE_MAPS_API_KEY", _get("GOOGLE_MAPS_GEOCODING_API_KEY"))


# ── Gmail SMTP (optional) ──
GMAIL_ADDRESS: str = _get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD: str = _get("GMAIL_APP_PASSWORD")

# ── Output ──
OUTPUT_DIR: Path = Path(_get("OUTPUT_DIR", "./output"))

# ── HTTP ──
HTTP_TIMEOUT: int = 30

# ── Branding ──
_branding_path = Path(__file__).parent / "branding.json"
_branding_example = Path(__file__).parent / "branding.example.json"


def _load_branding() -> dict:
    """Load branding config. Falls back to example if branding.json doesn't exist."""
    path = _branding_path if _branding_path.exists() else _branding_example
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "company_name": "STR Income Analysis",
        "tagline": "Short-Term Rental Management",
        "logo_url": "",
        "website_url": "",
        "primary_color": "#1f3c34",
        "accent_color": "#4b7c6b",
    }


BRANDING: dict = _load_branding()


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
