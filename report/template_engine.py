"""Jinja2 template rendering for the HTML report."""

from pathlib import Path
from datetime import date

import jinja2

from schema import ReportData
import config


TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


# ── Custom Jinja2 filters ────────────────────────────────────────────────

def format_currency(value: float, prefix: str = "CA$") -> str:
    """CA$1,100"""
    return f"{prefix}{int(value):,}"


def format_currency_k(value: float, prefix: str = "CA$") -> str:
    """CA$142.3K"""
    return f"{prefix}{value / 1000:.1f}K"


def format_bath(value: float) -> str:
    """3.5 → '3.5', 4.0 → '4'"""
    return str(int(value)) if value == int(value) else str(value)


# ── Rendering ─────────────────────────────────────────────────────────────

def render_report(data: ReportData) -> str:
    """Render the Jinja2 template with all report data. Returns HTML string."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # HTML template manages its own escaping
    )
    env.filters["format_currency"] = format_currency
    env.filters["format_currency_k"] = format_currency_k
    env.filters["format_bath"] = format_bath

    template = env.get_template("report.html.j2")

    # Pre-compute initial calculator display values
    calc = data.calculator
    # Must match report.html.j2's updateCalculator() exactly (Math.round), or
    # the pre-JS HTML that email_sender.py attaches disagrees with the live page.
    initial_occ_nights = round(calc.days_default * calc.occ_default / 100)
    initial_revenue = initial_occ_nights * calc.adr_default
    initial_revpar = round(initial_revenue / calc.days_default) if calc.days_default else 0

    return template.render(
        branding=config.BRANDING,
        property=data.property,
        rentalizer=data.rentalizer,
        comps=data.comps,
        calculator=data.calculator,
        narratives=data.narratives,
        methodology=data.methodology,
        seasonal_data=data.seasonal_data,
        report_date=data.report_date,
        initial_revenue=initial_revenue,
        initial_occ_nights=initial_occ_nights,
        initial_revpar=initial_revpar,
    )


def _slugify(text: str, max_len: int = 60) -> str:
    """Filename-safe slug: alphanumerics + hyphens, length-capped, no doubles."""
    import re
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "report"


def save_report(data: ReportData, output_dir: Path) -> Path:
    """Render and save the report HTML. Returns the output file path.

    Filename priority: subject's short_address (the listing name for Airbnb-URL
    inputs, the address for address inputs) > market name > generic "report".
    Falls back gracefully if any source is empty.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Prefer short_address (which is the Airbnb listing name for URL inputs,
    # the street address for address inputs). Drop "Unknown Market" sentinel.
    identity = (
        data.property.short_address
        or (data.property.market if data.property.market != "Unknown Market" else "")
        or "report"
    )
    slug = _slugify(identity)
    today = date.today().isoformat()
    brand_slug = _slugify(config.BRANDING.get("company_name", "STR"), max_len=20) or "STR"
    filename = f"{brand_slug}-Report-{slug}-{today}.html"
    output_path = output_dir / filename

    html = render_report(data)
    output_path.write_text(html, encoding="utf-8")
    return output_path
