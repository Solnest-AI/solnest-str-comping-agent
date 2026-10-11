"""Print / Save PDF must print the report, not the page's buttons.

Found 2026-10-10 while testing co-branded PDFs (S2-027) with headless
Chrome's --print-to-pdf: every page-1 PDF carried the "Print / Save PDF" and
website buttons. The print stylesheet said `.no-print { display: none; }`, but
the same elements carry Tailwind's `flex` / `inline-flex` utilities, which the
CDN injects later and which win at equal specificity. With `!important` the
buttons are gone (checked with pdftotext on the same report).

The same run printed every section faded: the fade-in animations start each
section at opacity 0. Whether a real browser's Print dialog does the same is
not verified; forcing full opacity in print is harmless either way, and the
PDF is the document students hand to clients.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "report.html.j2"


def _print_css() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"@media print\s*\{(.*?)\n    \}", text, re.DOTALL)
    assert m, "the report lost its print stylesheet"
    return m.group(1)


def _rule(css: str, selector_part: str) -> str:
    m = re.search(r"([^{}]*" + re.escape(selector_part) + r"[^{}]*)\{([^}]*)\}", css)
    assert m, f"no print rule for {selector_part}"
    return m.group(2)


def test_no_print_beats_tailwind_display_utilities():
    body = _rule(_print_css(), ".no-print")
    assert re.search(r"display:\s*none\s*!important", body)


def test_print_never_catches_a_section_mid_fade():
    css = _print_css()
    for cls in (".fade-in-up", ".delay-1", ".delay-2", ".delay-3"):
        assert cls in css, f"{cls} sections can print faded"
    body = _rule(css, ".fade-in-up")
    assert re.search(r"opacity:\s*1\s*!important", body)
    assert re.search(r"animation:\s*none\s*!important", body)


def test_every_no_print_element_is_still_marked():
    """The buttons the PDF must not carry are the ones tagged no-print."""
    text = TEMPLATE.read_text(encoding="utf-8")
    header = text[text.index("<!-- 1) Branded Header -->"):text.index("<!-- 2) Subject Property Details -->")]
    assert "Print / Save PDF" in header and "no-print" in header
