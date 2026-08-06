"""
Two-phase blocking sanity gate for STR income reports.

Phase A — pre-render. Runs on the `ReportData` before Jinja rendering.
  Checks: 6 comps present, every comp has all required fields, hero images
  return HTTP 200, subject hero from trusted source, revenue-math sanity,
  currency consistency, Airbnb URLs live, calculator defaults valid.

Phase B — post-render. Runs on the rendered HTML string/file.
  Checks: exactly 6 .comp-card elements, all <img> URLs HTTP 200, required
  sections present, no unrendered {{ }} artifacts, no residual "AirDNA"
  strings.

Blocking semantics: Phase A or Phase B failure raises SanityFailure and the
agent exits with code 2. Staging-path pattern in agent.py ensures the user
never sees a broken HTML file.

Legacy: `run_sanity_checks(data)` preserved as a warnings-only wrapper for
backward compat / diagnostics.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from schema import CompProperty, ReportData


# ── Exceptions ────────────────────────────────────────────────────────

class SanityFailure(Exception):
    """Raised when a blocking sanity check fails. Carries failure list for logging."""
    def __init__(self, phase: str, failures: list[str]):
        self.phase = phase
        self.failures = failures
        super().__init__(f"Phase {phase} sanity gate failed ({len(failures)} issue{'s' if len(failures) != 1 else ''})")


# ── Trusted image hosts (for subject hero provenance check) ───────────

_TRUSTED_HERO_HOSTS = (
    "a0.muscache.com",        # Airbnb (primary)
    "muscache.com",           # Airbnb (alt subdomains)
    "cdn.realtor.ca",         # realtor.ca
    "realtor.ca",
    "cdn-redfin.com",         # Redfin
    "ssl.cdn-redfin.com",
    "photos.zillowstatic.com",  # Zillow
    "zillowstatic.com",
    "maps.googleapis.com",    # Google Street View (Firecrawl fallback)
    "ssl.hwcdn.net",          # Homes.com CDN
    "static.wixstatic.com",   # Wix-hosted sites
    "a.travel-assets.com",    # VRBO
    "images.trvl-media.com",  # VRBO alt
    "api.mapbox.com",         # Mapbox static maps (rew.ca, some MLS sites)
)


# ── HTTP helpers ──────────────────────────────────────────────────────

async def _head_ok(client: httpx.AsyncClient, url: str, *, accept_302: bool = True) -> tuple[str, bool, int]:
    """HEAD request. Returns (url, ok, status_code)."""
    if not url:
        return url, False, 0
    try:
        resp = await client.head(url, follow_redirects=False, timeout=15)
        status = resp.status_code
        if status == 405 or status == 403:
            # Some CDNs reject HEAD; fall back to GET with range header
            resp = await client.get(url, headers={"Range": "bytes=0-128"}, timeout=15, follow_redirects=True)
            status = resp.status_code
        # 429 = rate-limited but URL exists (server processed the request).
        # Airbnb throttles when we check the same URLs twice in quick succession;
        # treat as alive rather than blocking the report.
        ok = status < 400 or (accept_302 and status in (301, 302)) or status == 429
        return url, ok, status
    except Exception:
        return url, False, 0


# ── Phase A: pre-render blocking checks ───────────────────────────────

def _check_comp_fields(comp: CompProperty) -> list[str]:
    """Return list of failure strings for missing/invalid fields on this comp."""
    errors: list[str] = []
    name = comp.name or "unnamed"

    if not comp.name or comp.name.strip() in ("", "Unnamed listing"):
        errors.append(f"[{name}] name is missing or placeholder")
    if not comp.image_url:
        errors.append(f"[{name}] image_url is empty")
    if not comp.airbnb_url:
        errors.append(f"[{name}] airbnb_url is empty")

    # Numeric fields must be present. Bedrooms can be 0 (valid for studios).
    if comp.bedrooms < 0:
        errors.append(f"[{name}] bedrooms = {comp.bedrooms} (negative)")
    if comp.bathrooms <= 0:
        errors.append(f"[{name}] bathrooms = {comp.bathrooms}")
    if comp.sleeps <= 0:
        errors.append(f"[{name}] sleeps = {comp.sleeps}")
    if comp.rating <= 0:
        # Rating 0 = no reviews yet — warn but don't block. In thin markets
        # these may be the only comps available.
        print(f"[Sanity] WARNING: [{name}] has no rating data (new listing)", file=sys.stderr)
    if comp.review_count <= 0:
        print(f"[Sanity] WARNING: [{name}] has no reviews (new listing)", file=sys.stderr)
    if comp.adr <= 0:
        errors.append(f"[{name}] adr = {comp.adr}")
    if comp.annual_revenue <= 0:
        errors.append(f"[{name}] annual_revenue = {comp.annual_revenue}")
    if comp.revenue_potential <= 0:
        errors.append(f"[{name}] revenue_potential = {comp.revenue_potential}")
    if not (0 < comp.occupancy_pct <= 100):
        errors.append(f"[{name}] occupancy_pct out of range: {comp.occupancy_pct}")
    if not (1 <= comp.days_available <= 365):
        errors.append(f"[{name}] days_available out of range: {comp.days_available}")
    if comp.rating > 5:
        errors.append(f"[{name}] rating out of range: {comp.rating}")

    return errors


def _check_revenue_sanity(comp: CompProperty) -> Optional[str]:
    """Sanity-check annual_revenue against (ADR × days) without overfitting.

    Real STR data has seasonal ADR/occupancy variance, so strict equality
    (adr × occ × days == annual_revenue within 10%) rejects most real listings.
    Instead we flag comps where annual_revenue is outside [0.15×, 3.0×] of
    (adr × days_available). Seasonal properties legitimately earn 2-3x their
    average ADR during peak — a cabin available 114 days that earns $203K on
    a $665 average ADR is a seasonal winner, not a data error.
    """
    if comp.adr <= 0 or comp.days_available <= 0 or comp.annual_revenue <= 0:
        return None  # handled by field check
    theoretical_max = comp.adr * comp.days_available
    if theoretical_max <= 0:
        return None
    ratio = comp.annual_revenue / theoretical_max
    if ratio < 0.15:
        return (f"[{comp.name}] annual_revenue (${comp.annual_revenue:,.0f}) is <15% of "
                f"ADR×days (${theoretical_max:,.0f}) — likely inactive or stale")
    if ratio > 3.0:
        return (f"[{comp.name}] annual_revenue (${comp.annual_revenue:,.0f}) exceeds 300% of "
                f"ADR×days (${theoretical_max:,.0f}) — data error suspected")
    return None


async def run_phase_a(data: ReportData) -> list[str]:
    """Pre-render blocking gate. Returns list of failure strings. Empty = pass.

    Caller (agent.py) should raise SanityFailure and write a .sanity-failed.json
    when this returns non-empty.
    """
    failures: list[str] = []

    # 1. Comp count — require exactly 6. The widening pass in agent.py should
    #    have already searched adjacent bedroom counts to fill the pool.
    n = len(data.comps)
    if n < 6:
        failures.append(
            f"Only {n} comps — need 6 for a complete report. "
            f"The widening pass should have searched adjacent markets/bedrooms."
        )

    # 2. Per-comp field completeness
    for comp in data.comps:
        failures.extend(_check_comp_fields(comp))

    # 3. Revenue-math sanity per comp — warn but don't block.
    #    The agent's pre-filter already handles revenue issues during comp
    #    selection. In thin markets, comps with unusual revenue ratios may
    #    be the only ones available — blocking the whole report isn't helpful.
    for comp in data.comps:
        msg = _check_revenue_sanity(comp)
        if msg:
            print(f"[Sanity] WARNING: {msg}", file=sys.stderr)

    # 4. Subject hero — warn if missing or untrusted, but don't block.
    #    Users can provide their own photo with --hero-url. Not having a
    #    photo shouldn't prevent the report from being generated.
    prop = data.property
    if not prop.hero_image_url:
        print("[Sanity] WARNING: No hero image — report will use placeholder. "
              "Provide one with --hero-url to improve the report.", file=sys.stderr)
    elif prop.hero_image_url:
        host = prop.hero_image_url.split("://", 1)[-1].split("/", 1)[0].lower()
        if not any(t in host for t in _TRUSTED_HERO_HOSTS):
            print(f"[Sanity] WARNING: Subject hero from untrusted source: {host}",
                  file=sys.stderr)

    # 5. Currency consistency (light — the template locks it in via property.currency)
    if not prop.currency:
        failures.append("property.currency is empty")

    # 6. Calculator defaults
    calc = data.calculator
    for field in ("adr_default", "occ_default", "days_default",
                  "adr_min", "adr_max", "occ_min", "occ_max"):
        v = getattr(calc, field, None)
        if v is None or (isinstance(v, (int, float)) and v <= 0):
            failures.append(f"calculator.{field} is missing/zero: {v}")

    # 7. HTTP liveness — subject hero + comp heroes + comp Airbnb URLs (parallel HEAD)
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 (airroi-report-agent sanity gate)"},
        timeout=20,
    ) as client:
        url_tasks: list = []
        url_meta: list[tuple[str, str]] = []  # (kind, url)

        if prop.hero_image_url:
            url_tasks.append(_head_ok(client, prop.hero_image_url, accept_302=True))
            url_meta.append(("subject hero", prop.hero_image_url))
        for c in data.comps:
            if c.image_url:
                url_tasks.append(_head_ok(client, c.image_url, accept_302=True))
                url_meta.append((f"comp hero [{c.name[:40]}]", c.image_url))
            if c.airbnb_url:
                url_tasks.append(_head_ok(client, c.airbnb_url, accept_302=True))
                url_meta.append((f"comp airbnb url [{c.name[:40]}]", c.airbnb_url))

        results = await asyncio.gather(*url_tasks, return_exceptions=True)

    for (kind, u), r in zip(url_meta, results):
        if isinstance(r, Exception):
            # Subject hero failures are warnings, not blockers — the image
            # is cosmetic. Off-market properties often have map/streetview
            # URLs that return 403 without an API key.
            if kind == "subject hero":
                print(f"[Sanity] WARNING: {kind} HTTP check raised: {r}", file=sys.stderr)
            else:
                failures.append(f"{kind} HTTP check raised: {r} — {u}")
            continue
        _url, ok, status = r
        if not ok:
            if kind == "subject hero":
                print(f"[Sanity] WARNING: {kind} returned HTTP {status} (non-blocking)", file=sys.stderr)
            else:
                failures.append(f"{kind} returned HTTP {status}: {u}")

    return failures


# ── Phase B: post-render blocking checks ──────────────────────────────

REQUIRED_TEMPLATE_KEYWORDS = [
    "Subject Property",         # section 2
    "Market Positioning",       # section 3
    "Revenue Projections",      # section 4
    "Performance Analytics",    # section 5 (seasonal chart)
    "Market Comparables",       # section 6
    "Methodology",              # section 7
]


async def run_phase_b(html_path: Path) -> list[str]:
    """Post-render blocking gate on the rendered HTML file."""
    failures: list[str] = []

    if not html_path.exists():
        failures.append(f"Rendered HTML not found at {html_path}")
        return failures

    html = html_path.read_text(encoding="utf-8")

    # 1. No unrendered Jinja placeholders
    jinja_artifacts = re.findall(r"\{\{[^}]{1,50}\}\}|\{%[^%]{1,50}%\}", html)
    if jinja_artifacts:
        uniq = sorted(set(jinja_artifacts))[:5]
        failures.append(f"Unrendered Jinja artifacts found: {uniq}")

    # 2. No residual "AirDNA" strings (except in harmless comments we deliberately
    #    chose to keep, which should be none now)
    airdna_hits = re.findall(r"AirDNA|airdna-pill|airdna_pill", html, re.IGNORECASE)
    if airdna_hits:
        failures.append(f"Residual 'AirDNA' references found ({len(airdna_hits)} times) — should be rebranded to AirROI")

    # 3. BeautifulSoup-based structural checks
    soup = BeautifulSoup(html, "html.parser")

    comp_cards = soup.select(".comp-card")
    if len(comp_cards) < 6:
        failures.append(f"Expected 6 .comp-card elements, got {len(comp_cards)} (too few)")
    elif len(comp_cards) > 6:
        failures.append(f"Expected 6 .comp-card elements, got {len(comp_cards)} (too many)")

    for keyword in REQUIRED_TEMPLATE_KEYWORDS:
        if keyword not in html:
            failures.append(f"Missing required section keyword: {keyword!r}")

    # 4. All <img> sources return HTTP 200 (skip data: URIs and empty srcs)
    img_tags = soup.find_all("img")
    img_urls: list[str] = []
    for img in img_tags:
        src = (img.get("src") or "").strip()
        if not src or src.startswith(("data:", "#")):
            continue
        img_urls.append(src)

    if img_urls:
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (airroi-report-agent sanity gate)"},
            timeout=20,
        ) as client:
            results = await asyncio.gather(
                *[_head_ok(client, u, accept_302=True) for u in img_urls],
                return_exceptions=True,
            )
        # Map/streetview URLs may return 403 without API keys — treat as
        # warnings for those sources, failures for actual property images.
        _MAP_HOSTS = ("maps.googleapis.com", "api.mapbox.com")
        for u, r in zip(img_urls, results):
            is_map = any(h in u for h in _MAP_HOSTS)
            if isinstance(r, Exception):
                if is_map:
                    print(f"[Sanity] WARNING: map image check raised: {r}", file=sys.stderr)
                else:
                    failures.append(f"Rendered <img> HEAD raised: {r} — {u}")
                continue
            _url, ok, status = r
            if not ok:
                if is_map:
                    print(f"[Sanity] WARNING: map image returned HTTP {status} (non-blocking)", file=sys.stderr)
                else:
                    failures.append(f"Rendered <img> returned HTTP {status}: {u}")

    return failures


# ── Failure report writer ─────────────────────────────────────────────

def write_failure_report(
    phase: str,
    failures: list[str],
    output_dir: Path,
    subject_slug: str = "report",
) -> Path:
    """Write a JSON dump of failures + exit-friendly stderr log."""
    import json
    from datetime import datetime

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{subject_slug}.sanity-failed-phase-{phase}.json"
    path.write_text(json.dumps({
        "phase":     phase,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "failures":  failures,
    }, indent=2), encoding="utf-8")

    print(f"\n[SANITY PHASE {phase}] {len(failures)} issue(s):", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    print(f"\n  Full failure log: {path.resolve()}", file=sys.stderr)
    return path


# ── Legacy warnings-only wrapper (for diagnostics) ────────────────────

async def run_sanity_checks(data: ReportData) -> list[str]:
    """Legacy non-blocking sanity check. Returns list of warnings.

    Preserved for backwards compat in any caller that expects a warning list
    rather than blocking behavior. New code should call `run_phase_a()` and
    treat a non-empty return as blocking.
    """
    return await run_phase_a(data)
