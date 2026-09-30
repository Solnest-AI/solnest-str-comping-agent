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
import re
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


# The browser's own unvisited / visited link colours. A site with an unstyled
# link reports them as brand colours (solneststays.com: "secondary" #0000EE),
# and they are nobody's brand.
BROWSER_DEFAULTS = {"#0000ee", "#551a8b"}


def chroma(h: str) -> float:
    """HSV saturation: how coloured a colour is, 0 for greys, black and white.

    Not HLS saturation: HLS calls cream (#F0EBE1) 0.43 "saturated" and a muted
    sage (#8A8C6D) only 0.12, which is backwards for picking a brand colour.
    """
    r, g, b = _rgb(h)
    hi = max(r, g, b)
    return 0.0 if hi == 0 else (hi - min(r, g, b)) / hi


def is_brand_hue(h: str) -> bool:
    """A real colour, not a grey, not the browser's link blue. Sage, tan and
    greige-with-a-tint count: muted palettes are normal for STR brands."""
    return h not in BROWSER_DEFAULTS and chroma(h) >= 0.18


def _distance(a: str, b: str) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5 * 255


def pick_colours(branding: dict) -> tuple[str, str]:
    """(primary_color, accent_color) for a light report, from Firecrawl's read.

    The site's declared primary (or its main button) IS the brand. When it is
    too light to read on cream it is darkened, same hue, until it is, instead
    of being swapped for whatever else on the page happens to be dark: that
    used to turn a sage brand navy (its link colour) and a tan brand black
    (its body text), measured on 5 of 13 real STR sites on 2026-09-29.
    """
    colours = branding.get("colors") or {}
    button = ((branding.get("components") or {}).get("buttonPrimary") or {}).get("background")
    order = [colours.get("primary"), button, colours.get("accent"), colours.get("link"),
             colours.get("secondary"), colours.get("textPrimary"), colours.get("background")]
    cands = [h for h in dict.fromkeys(_hex(c) for c in order) if h and h not in BROWSER_DEFAULTS]
    if not cands:
        return config._BRANDING_DEFAULTS["primary_color"], config._BRANDING_DEFAULTS["accent_color"]

    primary = None
    for h in (_hex(colours.get("primary")), _hex(button)):
        if not h or h in BROWSER_DEFAULTS:
            continue
        if is_brand_hue(h):
            primary = darken_to(h, 7)          # unchanged when already dark enough
            break
        if contrast(h, REPORT_BG) >= 7:        # a near-black brand (dark site's buttons)
            primary = h
            break
        # a grey, cream or white "primary" is a dark site's text: keep looking
    if primary is None:
        dark = [h for h in cands if contrast(h, REPORT_BG) >= 7]
        colourful = [h for h in dark if is_brand_hue(h)]
        if colourful:
            primary = colourful[0]
        elif dark:
            primary = dark[0]
        else:
            primary = darken_to(max(cands, key=chroma), 7)

    # Accent: a brand colour visibly different from the primary. The site's
    # own accent first, then the primary as the site shows it (so a darkened
    # sage gets the real sage beside it), darkened only as far as 3:1 needs.
    accent_order = [colours.get("accent"), colours.get("primary"), button,
                    colours.get("link"), colours.get("secondary")]
    acc = [h for h in dict.fromkeys(_hex(c) for c in accent_order)
           if h and is_brand_hue(h) and _distance(h, primary) >= 48]
    good = [h for h in acc if contrast(h, REPORT_BG) >= 3]
    if good:
        accent = good[0]
    elif acc:
        # The one that needs the least darkening: a yellow taken to 3:1 is
        # mustard (houst.com), a light blue taken to 3:1 is still that blue.
        accent = darken_to(max(acc, key=lambda h: contrast(h, REPORT_BG)), 3)
    else:
        accent = darken_to(_lighten(primary, 0.35), 3)
    return primary, accent


def clean_name(name: str) -> str:
    """'Air Concierge | Short Term Rental Management' -> 'Air Concierge'.
    Firecrawl sometimes returns the page title; the brand is the first part."""
    for sep in (" | ", " – ", " — "):
        name = name.split(sep)[0]
    return name.strip()


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


_SVG_COLOUR = re.compile(r"""(?:fill|stroke|stop-color)\s*[:=]\s*["']?\s*(#[0-9a-fA-F]{3,6}\b|white\b|rgb\([^)]*\))""",
                         re.IGNORECASE)


def _svg_colour(raw: str) -> str | None:
    raw = raw.strip().lower()
    if raw == "white":
        return "#ffffff"
    if raw.startswith("rgb"):
        nums = re.findall(r"\d+", raw)[:3]
        return "#" + "".join(f"{min(int(n), 255):02x}" for n in nums) if len(nums) == 3 else None
    return _hex(raw)


def logo_is_light(path: Path | None) -> bool | None:
    """True when the logo is drawn light (white text on a transparent
    background) and would vanish on the report's cream header, False when it
    reads on cream, None when it cannot be told (no file, unknown format).

    legacyrnr.com's header logo is LegacyRnR_Logo_white.png on a light site:
    the colour-scheme rule alone left it invisible.
    """
    if path is None or not path.is_file():
        return None
    try:
        if path.suffix == ".svg":
            text = path.read_text(encoding="utf-8", errors="replace")
            found = [c for c in (_svg_colour(m) for m in _SVG_COLOUR.findall(text)) if c]
            if not found:
                return False                # unfilled SVG paths draw black
            light = sum(contrast(c, REPORT_BG) < 1.8 for c in found)
            return light / len(found) >= 0.6
        from PIL import Image               # Pillow: in requirements.txt
        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((160, 160))
            raw = im.tobytes()                # RGBA, 4 bytes a pixel, on every Pillow version
    except Exception:                        # a logo we cannot read is not worth a crash
        return None
    total = len(raw) // 4
    opaque = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 4) if raw[i + 3] >= 128]
    if not opaque or len(opaque) >= 0.97 * total:
        # Nothing drawn, or a solid rectangle (a JPEG, a logo on its own
        # background): a box always shows on cream, whatever is in it.
        return None if not opaque else False
    light = sum(contrast("#%02x%02x%02x" % p, REPORT_BG) < 1.8 for p in opaque)
    return light / len(opaque) >= 0.6


def plate_for(branding: dict, logo_file: Path | None, primary: str) -> str:
    """The logo's backing colour on the report header, "" for none."""
    light = logo_is_light(logo_file)
    if light is None:
        return logo_plate(branding)          # cannot see it: go by the site's scheme
    if not light:
        return ""                            # a dark logo reads on cream as is
    return logo_plate(branding) or primary   # a light logo needs a dark plate


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
    ap.add_argument("--logo", help="the logo's image address, when the site's own was not found "
                                   "or is the wrong one (right-click the logo, Copy image address)")
    a = ap.parse_args(argv)
    site = a.website.strip()
    if not site.lower().startswith(("http://", "https://")):
        site = "https://" + site
    origin = site_origin(site)
    if not origin:
        print(f"[brand] '{a.website}' is not a website address.")
        print("NEXT: ask the student for their website address (like theirsite.com), wait for their answer, then run this again.")
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
    logo = logo_url({"logo": a.logo.strip()}, site) if a.logo else logo_url(brand, site)
    logo_file = fetch_logo(logo) if logo else None
    if a.logo and not logo_file:
        print(f"[brand] The logo link given did not load as an image: {a.logo.strip()}")
    if logo and not logo_file:
        logo = ""
    try:
        previous = json.loads(BRANDING.read_text(encoding="utf-8-sig")) if BRANDING.exists() else {}
    except ValueError:
        previous = {}
    example = json.loads(EXAMPLE.read_text(encoding="utf-8-sig"))
    name = clean_name(str(brand.get("brandName") or "")) or urlparse(origin).hostname.removeprefix("www.")
    out = {
        "company_name": name,
        "tagline": a.tagline or previous.get("tagline") or example.get("tagline", ""),
        "logo_url": logo,
        "website_url": origin,
        "primary_color": primary,
        "accent_color": accent,
        "logo_background": plate_for(brand, logo_file, primary) if logo else "",
    }
    BRANDING.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(f"[brand] Wrote {BRANDING.name} from {site}")
    print(f"  company_name   {out['company_name']}")
    print(f"  tagline        {out['tagline']}")
    if logo:
        print(f"  logo_url       {logo}")
    else:
        print("  logo_url       (none found: the header shows the name only. Ask the student to")
        print('                 right-click their logo, Copy image address, and re-run with --logo "<that>")')
    if out["logo_background"]:
        print(f"  logo_background {out['logo_background']}   (a light logo: it sits on this plate)")
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
