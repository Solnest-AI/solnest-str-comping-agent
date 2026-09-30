#!/usr/bin/env python3
"""Brand the report from the student's own website: name, logo and colours.

    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/brand_from_website.py https://theirsite.com

The report is white-label and reads branding.json. Students should not have to
find a logo URL or hex codes, so this asks Firecrawl (the key the connections
kit already holds) to read the site's brand, then maps it onto the report:

  company_name   the brand name Firecrawl reads off the site
  logo_url       the header logo, checked to load as an image
  website_url    the site itself
  primary_color  headings, buttons, the header gradient: a brand colour dark
                 enough to read on the report's cream background (WCAG 7:1),
                 because a dark site's "primary" is often its cream text
  accent_color   labels and the gradient's far end, readable at 3:1

It writes branding.json and saves the logo to .cache/brand_logo.* so Claude can
LOOK at it and confirm with the student before the first report. Nothing here
prints a key.

Exit codes:
  0  branding.json written
  2  Firecrawl rejected the key or is out of credit (run scripts/check_setup.py)
  3  the site could not be read; ask the student for another page or fill
     branding.json by hand from branding.example.json
"""

from __future__ import annotations

import argparse
import colorsys
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import kit  # noqa: E402

BRANDING = ROOT / "branding.json"
EXAMPLE = ROOT / "branding.example.json"
CACHE = ROOT / ".cache"
REPORT_BG = "#f7f3ee"          # the report's cream background (--sol-cream)
FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v2/scrape"


# ── colour maths (pure, tested) ─────────────────────────────────────────

def _hex(value) -> str | None:
    s = str(value or "").strip()
    if len(s) == 4 and s.startswith("#"):           # #abc -> #aabbcc
        s = "#" + "".join(c * 2 for c in s[1:])
    if len(s) == 7 and s.startswith("#"):
        try:
            int(s[1:], 16)
            return s.lower()
        except ValueError:
            return None
    return None


def _rgb(h: str) -> tuple[float, float, float]:
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two #rrggbb colours."""
    def lum(h):
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in _rgb(h)]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def saturation(h: str) -> float:
    return colorsys.rgb_to_hls(*_rgb(h))[2]


def darken_to(h: str, ratio: float, bg: str = REPORT_BG) -> str:
    """The same hue, darkened until it reaches `ratio` against bg."""
    hue, light, sat = colorsys.rgb_to_hls(*_rgb(h))
    while light > 0 and contrast(h, bg) < ratio:
        light = max(0.0, light - 0.02)
        h = "#" + "".join(f"{round(c * 255):02x}" for c in colorsys.hls_to_rgb(hue, light, sat))
    return h


def pick_colours(branding: dict) -> tuple[str, str]:
    """(primary_color, accent_color) for a light report, from Firecrawl's read."""
    colours = branding.get("colors") or {}
    button = ((branding.get("components") or {}).get("buttonPrimary") or {}).get("background")
    order = [colours.get("primary"), button, colours.get("accent"), colours.get("link"),
             colours.get("secondary"), colours.get("textPrimary"), colours.get("background")]
    cands = [h for h in dict.fromkeys(_hex(c) for c in order) if h]
    if not cands:
        return config._BRANDING_DEFAULTS["primary_color"], config._BRANDING_DEFAULTS["accent_color"]

    # Primary: prefer a real colour over near-black/grey when one is dark enough.
    dark = [h for h in cands if contrast(h, REPORT_BG) >= 7]
    colourful = [h for h in dark if saturation(h) >= 0.25]
    if colourful:
        primary = colourful[0]
    elif dark:
        primary = dark[0]
    else:
        primary = darken_to(max(cands, key=saturation), 7)

    # Accent: a different, readable, preferably colourful brand colour.
    accent_order = [colours.get("accent"), colours.get("link"), colours.get("secondary"),
                    colours.get("primary"), button]
    acc = [h for h in dict.fromkeys(_hex(c) for c in accent_order) if h and h != primary]
    good = [h for h in acc if contrast(h, REPORT_BG) >= 3 and saturation(h) >= 0.25]
    fair = [h for h in acc if saturation(h) >= 0.25]
    if good:
        accent = good[0]
    elif fair:
        accent = darken_to(fair[0], 3)
    else:
        accent = darken_to(_lighten(primary, 0.35), 3)
    return primary, accent


def _lighten(h: str, amount: float) -> str:
    return "#" + "".join(f"{round((c + (1 - c) * amount) * 255):02x}" for c in _rgb(h))


def logo_plate(branding: dict) -> str:
    """A dark site's logo is drawn light (cream text on black), so on the
    report's cream header it would vanish. Put it back on the site's own
    background. "" for a light site: the logo sits on the header as is."""
    if str(branding.get("colorScheme", "")).lower() != "dark":
        return ""
    bg = _hex((branding.get("colors") or {}).get("background"))
    return bg if bg and contrast(bg, REPORT_BG) >= 3 else "#111111"


def logo_url(branding: dict, site: str) -> str:
    """The logo as a direct absolute link. Next.js serves images through
    /_next/image?url=/logo.png&w=384; the direct file is steadier."""
    images = branding.get("images") or {}
    raw = branding.get("logo") or images.get("logo") or ""
    if not raw or str(raw).startswith("data:"):
        return ""
    url = urljoin(site, str(raw))
    parsed = urlparse(url)
    if parsed.path.endswith("/_next/image"):
        inner = parse_qs(parsed.query).get("url", [""])[0]
        if inner:
            url = urljoin(site, inner)
    return url if url.startswith(("https://", "http://")) else ""


def site_origin(site: str) -> str:
    """scheme://host[:port] of a page URL, "" when it has no usable host.

    The report links to the student's site, not to whichever page the brand
    was read from, and a userinfo or query string must never be copied into a
    public link.
    """
    try:
        parsed = urlparse(site)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return ""
    if not host or parsed.scheme not in ("http", "https"):
        return ""
    host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


# ── I/O ─────────────────────────────────────────────────────────────────

def read_brand(site: str) -> dict:
    """Firecrawl's brand read of the site. Raises kit.KeyFailure on a key problem."""
    r = httpx.post(FIRECRAWL_SCRAPE, headers={"Authorization": f"Bearer {config.FIRECRAWL_API_KEY}"},
                   json={"url": site, "formats": ["branding"]}, timeout=120)
    kit.check_key_status("Firecrawl", r.status_code)
    r.raise_for_status()
    return ((r.json() or {}).get("data") or {}).get("branding") or {}


def fetch_logo(url: str) -> Path | None:
    """Download the logo so it can be looked at; None if it is not an image."""
    try:
        r = httpx.get(url, follow_redirects=True, timeout=30,
                      headers={"User-Agent": "Mozilla/5.0 (brand check)"})
    except httpx.HTTPError:
        return None
    kind = r.headers.get("content-type", "").split(";")[0].strip()
    if r.status_code != 200 or not kind.startswith("image/"):
        return None
    ext = {"image/svg+xml": ".svg", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(kind, ".png")
    CACHE.mkdir(exist_ok=True)
    for old in CACHE.glob("brand_logo.*"):
        old.unlink()
    path = CACHE / f"brand_logo{ext}"
    path.write_bytes(r.content)
    return path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("website")
    ap.add_argument("--tagline", help="footer line, e.g. 'Short-Term Rental Management'")
    a = ap.parse_args(argv)
    site = a.website.strip()
    if not site.lower().startswith(("http://", "https://")):
        site = "https://" + site
    origin = site_origin(site)
    if not origin:
        print(f"[brand] '{a.website}' is not a website address.")
        print("NEXT: ask the student for their website address (like theirsite.com) and run this again.")
        return 3
    config.ensure_firecrawl_configured()

    try:
        brand = read_brand(site)
    except kit.KeyFailure as e:
        kit.forget_setup_pass()
        print(e)
        return 2
    except (httpx.HTTPError, ValueError) as e:
        print(f"[brand] Could not read {site}: {type(e).__name__}")
        brand = {}
    if not brand:
        print(f"[brand] Firecrawl found no brand on {site}.")
        print("NEXT: ask the student for their home page (or another page with their logo) and run this")
        print("      again, or copy branding.example.json to branding.json and fill it in with them.")
        return 3

    CACHE.mkdir(exist_ok=True)
    (CACHE / "brand_raw.json").write_text(json.dumps(brand, indent=1), encoding="utf-8")

    primary, accent = pick_colours(brand)
    logo = logo_url(brand, site)
    logo_file = fetch_logo(logo) if logo else None
    if logo and not logo_file:
        logo = ""
    try:
        previous = json.loads(BRANDING.read_text(encoding="utf-8-sig")) if BRANDING.exists() else {}
    except ValueError:
        previous = {}
    example = json.loads(EXAMPLE.read_text(encoding="utf-8-sig"))
    name = str(brand.get("brandName") or "").strip() or urlparse(origin).hostname.removeprefix("www.")
    out = {
        "company_name": name,
        "tagline": a.tagline or previous.get("tagline") or example.get("tagline", ""),
        "logo_url": logo,
        "website_url": origin,
        "primary_color": primary,
        "accent_color": accent,
        "logo_background": logo_plate(brand) if logo else "",
    }
    BRANDING.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(f"[brand] Wrote {BRANDING.name} from {site}")
    print(f"  company_name   {out['company_name']}")
    print(f"  tagline        {out['tagline']}")
    print(f"  logo_url       {logo or '(none found: the header shows the name only)'}")
    if out["logo_background"]:
        print(f"  logo_background {out['logo_background']}   (dark site: the logo sits on this plate)")
    print(f"  primary_color  {primary}   (contrast {contrast(primary, REPORT_BG):.1f}:1 on the report)")
    print(f"  accent_color   {accent}   (contrast {contrast(accent, REPORT_BG):.1f}:1)")
    site_colours = {k: v for k, v in (brand.get("colors") or {}).items() if _hex(v)}
    print(f"  site colours   {site_colours}")
    if logo_file:
        print(f"  logo saved     {logo_file}")
    print("NEXT: look at the saved logo, then show the student the name, logo, tagline and colours")
    print("      and ask if that is their brand. Change anything they say in branding.json; colours")
    print("      must stay #rrggbb and dark enough to read on cream.")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
