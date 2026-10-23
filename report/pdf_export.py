"""Render a saved HTML report to a client-ready PDF with headless Chromium.

    PY="$(bash scripts/ensure_env.sh)" && "$PY" -m report.pdf_export "output/<report>.html"

(or pass --pdf to agent.py). scale=0.6 lays the page out at a ~1200px virtual
width, so Tailwind's md/lg breakpoints apply and the PDF keeps the desktop
multi-column layout. The template's @media print rules handle the rest
(animations, shadows, buttons).

Optional: needs Playwright, which requirements.txt does not install:
    uv pip install --python .venv playwright && .venv/Scripts/python -m playwright install chromium
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def free_path(path: Path) -> Path:
    """`path`, or `path` with -2, -3 ... before the suffix when it exists."""
    path, n = Path(path), 2
    candidate = path
    while candidate.exists():
        candidate, n = path.with_name(f"{path.stem}-{n}{path.suffix}"), n + 1
    return candidate


def export_pdf(html_path: Path, pdf_path: Path | None = None, scale: float = 0.6) -> Path:
    """Write the PDF next to the HTML (or to `pdf_path`) and return its path.
    An existing PDF is never overwritten; a -2/-3 suffix is used instead."""
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path).resolve()
    pdf_path = free_path(Path(pdf_path) if pdf_path else html_path.with_suffix(".pdf"))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Airbnb's CDN serves Chromium AVIF, which Chrome re-embeds in the PDF as a
        # lossless ~600 KB bitmap per photo. Asking for JPEG gets ~50 KB files that
        # Chrome embeds as-is (a ~3 MB PDF instead of ~15 MB).
        page.route(re.compile(r"^https://[^/]*muscache\.com/"), lambda route: route.continue_(
            headers={**route.request.headers, "accept": "image/jpeg"},
        ))
        # Tailwind's CDN build and Chart.js both run in the page, so let the
        # network settle and the chart finish its draw-in animation.
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.pdf(
            path=str(pdf_path),
            format="Letter",
            scale=scale,
            print_background=True,
            margin={"top": "0.4in", "bottom": "0.4in", "left": "0.4in", "right": "0.4in"},
        )
        browser.close()
    return pdf_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an STR report HTML to PDF.")
    parser.add_argument("html", help="Path to the rendered report .html")
    parser.add_argument("pdf", nargs="?", default=None,
                        help="Output .pdf (default: next to the HTML)")
    parser.add_argument("--scale", type=float, default=0.6)
    args = parser.parse_args()
    out = export_pdf(Path(args.html), Path(args.pdf) if args.pdf else None, args.scale)
    print(f"[PDF] Saved to: {out}")


if __name__ == "__main__":
    main()
