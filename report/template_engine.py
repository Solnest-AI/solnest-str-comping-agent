"""Jinja2 template rendering for the HTML report."""

import statistics
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
        # Listing names, descriptions and LLM narrative text land in this
        # template verbatim. Escaping is on by default; the only values
        # allowed to carry markup are the methodology lists, which build
        # their own <strong> tags and escape their interpolations at
        # source, and are marked |safe individually in the template.
        autoescape=True,
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

    # Say where the headline's occupancy came from. The three bases are not
    # equally strong and a report that presents them identically is hiding
    # the difference: the comp-set median sits near the market's 81st
    # percentile and over-projected by +72% across 125 backtested listings.
    sp = getattr(data.property, "subject_performance", None)
    if calc.occ_basis == "subject" and sp is not None:
        occ_basis_text = (
            f"Occupancy is this property's own measured result over the last "
            f"12 months ({sp.occupancy_pct:.0f}% of {sp.nights_listed} open "
            f"nights), not an estimate from the comparables."
        )
    elif calc.occ_basis == "market_strong" and sp is not None:
        months = sp.months_with_data
        covered = f"the {months} months" if months else "the months"
        occ_basis_text = (
            f"This listing has not been on the market a full year, so its "
            f"trailing-12-month occupancy is measured partly over a period it "
            f"was not listed for and understates it. The scenario above is "
            f"built on this market's UPPER-QUARTILE occupancy instead, because "
            f"across {covered} the property has actually operated it ran above "
            f"the market median every month."
        )
    elif calc.occ_basis == "market_typical":
        occ_basis_text = (
            "This listing has not been on the market a full year, so its "
            "trailing-12-month occupancy covers a period it was not listed "
            "for. The scenario above uses this market's median occupancy "
            "instead of that figure. Its own measured result is shown below."
        )
    elif calc.occ_basis == "market_pool":
        occ_basis_text = (
            "Occupancy is the median across every comparable listing in this "
            "market, not the median of the six shown below. Those six are "
            "selected for quality and run well above the market median."
        )
    else:
        occ_basis_text = (
            "Occupancy is the median of the six comparables shown below. "
            "Those six are selected for quality, so treat this as a "
            "well-run-operator figure rather than a market average."
        )

    return template.render(
        comp_summary=_comp_summary(data),
        subject_performance=sp,
        occ_basis_text=occ_basis_text,
        branding=config.BRANDING,
        property=data.property,
        rentalizer=data.rentalizer,
        comps=data.comps,
        calculator=data.calculator,
        narratives=data.narratives,
        methodology=data.methodology,
        seasonal_data=data.seasonal_data,
        seasonal_p25=data.seasonal_p25,
        seasonal_p75=data.seasonal_p75,
        subject_monthly=data.subject_monthly,
        report_date=data.report_date,
        initial_revenue=initial_revenue,
        initial_occ_nights=initial_occ_nights,
        initial_revpar=initial_revpar,
    )


def _comp_summary(data: ReportData) -> dict | None:
    """Facts for the comp-section blurb, computed rather than asserted.

    The blurb used to say the subject's "caliber is above typical inventory"
    on every report, including subjects below the comp median.
    """
    comps = [c for c in data.comps if c.annual_revenue > 0]
    if not comps:
        return None
    cur = data.property.currency
    revs = sorted(c.annual_revenue for c in comps)
    beds = sorted({c.bedrooms for c in comps})
    sleeps = sorted({c.sleeps for c in comps})

    def span(vals, unit):
        return (f"{vals[0]} {unit}" if len(vals) == 1
                else f"{vals[0]}-{vals[-1]} {unit}")

    return {
        "count": len(comps),
        "bedrooms": span(beds, "bedrooms"),
        "sleeps": span(sleeps, "guests"),
        "median_revenue": format_currency(round(statistics.median(revs)), cur),
        "low_revenue": format_currency(revs[0], cur),
        "high_revenue": format_currency(revs[-1], cur),
    }


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
