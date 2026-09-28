"""Branding: from the student's website to every colour on the report. Hermetic.

The report once shipped with a broken "Your Company Logo" image, an empty
website button and Solnest greens hard-coded into its gradients, shadows and
chart, so a student's own colours reached about a quarter of it (2026-09-28).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import brand_from_website as bw  # noqa: E402
import check_setup as cs  # noqa: E402
import config  # noqa: E402
from report import template_engine as te  # noqa: E402

SOLNEST = json.loads((ROOT / "tests" / "fixtures" / "branding_solnestai.json").read_text(encoding="utf-8"))
TEMPLATE = (ROOT / "templates" / "report.html.j2").read_text(encoding="utf-8")


# ── from Firecrawl's read to branding.json ──────────────────────────────

def test_a_dark_site_gets_readable_colours_not_its_cream_text():
    """solnestai.com's "primary" is #F0EBE1, its cream text: invisible on the report."""
    primary, accent = bw.pick_colours(SOLNEST)
    assert primary == "#12110f" and accent == "#c0522b"
    assert bw.contrast(primary, bw.REPORT_BG) >= 7
    assert bw.contrast(accent, bw.REPORT_BG) >= 3


def test_a_light_site_keeps_its_own_brand_colour():
    brand = {"colorScheme": "light", "colors": {"primary": "#1E40AF", "accent": "#F59E0B",
                                                "background": "#FFFFFF", "textPrimary": "#111827"}}
    primary, accent = bw.pick_colours(brand)
    assert primary == "#1e40af", "a dark enough brand blue beats near-black text"
    assert bw.contrast(accent, bw.REPORT_BG) >= 3 and bw.saturation(accent) >= 0.25


def test_a_pale_only_palette_is_darkened_not_dropped():
    primary, accent = bw.pick_colours({"colors": {"primary": "#FFD1DC", "accent": "#B5EAD7"}})
    assert bw.contrast(primary, bw.REPORT_BG) >= 7
    assert bw.contrast(accent, bw.REPORT_BG) >= 3


def test_no_colours_falls_back_to_the_defaults():
    assert bw.pick_colours({}) == (config._BRANDING_DEFAULTS["primary_color"],
                                   config._BRANDING_DEFAULTS["accent_color"])


def test_next_image_proxy_resolves_to_the_direct_logo_file():
    assert bw.logo_url(SOLNEST, "https://solnestai.com") == "https://solnestai.com/solnest-logo.png"
    assert bw.logo_url({"logo": "/img/logo.svg"}, "https://x.com/about") == "https://x.com/img/logo.svg"
    assert bw.logo_url({"logo": "data:image/png;base64,AAA"}, "https://x.com") == ""
    assert bw.logo_url({}, "https://x.com") == ""


def test_a_dark_sites_logo_goes_on_its_own_background():
    assert bw.logo_plate(SOLNEST) == "#0d0d0b"
    assert bw.logo_plate({"colorScheme": "light", "colors": {"background": "#ffffff"}}) == ""


def test_main_writes_branding_json_and_never_needs_the_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw, "CACHE", tmp_path / ".cache")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(bw, "read_brand", lambda site: SOLNEST)
    monkeypatch.setattr(bw, "fetch_logo", lambda url: tmp_path / "logo.png")
    assert bw.main(["solnestai.com", "--tagline", "AI for STR operators"]) == 0
    out = json.loads((tmp_path / "branding.json").read_text(encoding="utf-8"))
    assert out == {"company_name": "Solnest AI", "tagline": "AI for STR operators",
                   "logo_url": "https://solnestai.com/solnest-logo.png",
                   "website_url": "https://solnestai.com", "primary_color": "#12110f",
                   "accent_color": "#c0522b", "logo_background": "#0d0d0b"}
    assert "fc-test" not in capsys.readouterr().out


def test_a_logo_that_does_not_load_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw, "CACHE", tmp_path / ".cache")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(bw, "read_brand", lambda site: SOLNEST)
    monkeypatch.setattr(bw, "fetch_logo", lambda url: None)
    assert bw.main(["https://solnestai.com"]) == 0
    out = json.loads((tmp_path / "branding.json").read_text(encoding="utf-8"))
    assert out["logo_url"] == "" and out["logo_background"] == ""


def test_a_site_with_no_brand_exits_3_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(bw, "read_brand", lambda site: {})
    assert bw.main(["https://example.com"]) == 3
    assert not (tmp_path / "branding.json").exists()


def test_a_firecrawl_key_failure_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")

    def rejected(site):
        bw.kit.check_key_status("Firecrawl", 401)

    monkeypatch.setattr(bw, "read_brand", rejected)
    assert bw.main(["https://solnestai.com"]) == 2


# ── the setup gate ─────────────────────────────────────────────────────

def test_setup_asks_for_the_website_before_the_first_report(tmp_path, monkeypatch, capsys):
    kit_dir = tmp_path / "kit"
    kit_dir.mkdir()
    for marker in ("CONNECTIONS.md", "fan-out-env.sh"):
        (kit_dir / marker).write_text("", encoding="utf-8")
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs, "PROBES", {n: (lambda key: "ok") for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    monkeypatch.setattr(cs, "other_copy", lambda name: ("", ""))
    monkeypatch.setattr(cs, "BRANDING", tmp_path / "branding.json")
    assert cs.main(["--no-open"]) == 4
    assert "brand_from_website.py" in capsys.readouterr().out
    (tmp_path / "branding.json").write_text("{}", encoding="utf-8")
    assert cs.main(["--no-open"]) == 0


# ── config: scraped values land in CSS and JS, so they are checked ─────────

def _load(tmp_path, monkeypatch, data):
    path = tmp_path / "branding.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "_BRANDING_PATH", path)
    return config._load_branding()


def test_bad_colours_and_links_fall_back(tmp_path, monkeypatch):
    b = _load(tmp_path, monkeypatch, {"primary_color": "red;}</style><script>", "accent_color": "#abc",
                                      "logo_url": "javascript:alert(1)", "website_url": "ftp://x",
                                      "logo_background": "url(x)"})
    assert b["primary_color"] == config._BRANDING_DEFAULTS["primary_color"]
    assert b["accent_color"] == config._BRANDING_DEFAULTS["accent_color"]
    assert b["logo_url"] == "" and b["website_url"] == "" and b["logo_background"] == ""


# ── the template: every brand colour comes from branding.json ────────────

def test_no_solnest_colour_is_hard_coded_in_the_template():
    for literal in (r"#1f3c34", r"#4b7c6b", r"#6b9b88", r"rgba\(\s*31\s*,\s*60\s*,\s*52", r"rgba\(\s*75\s*,\s*124\s*,\s*107"):
        assert not re.search(literal, TEMPLATE, re.I), literal


def test_filters():
    assert te.rgb("#1f3c34") == "31, 60, 52"
    assert te.lighten("#000000", 0.5) == "#808080"


def _render_header(branding: dict) -> str:
    import jinja2
    env = jinja2.Environment(autoescape=True)
    env.filters.update(rgb=te.rgb, lighten=te.lighten, format_bath=te.format_bath)
    start = TEMPLATE.index("<!-- 1) Branded Header -->")
    end = TEMPLATE.index("</header>", start) + len("</header>")
    return env.from_string(TEMPLATE[start:end]).render(
        branding=branding, property={"short_address": "X", "bedrooms": 1, "bathrooms": 1, "property_type": ""})


def test_no_logo_means_no_broken_image_and_no_website_means_no_empty_button():
    html = _render_header({**config._BRANDING_DEFAULTS, "company_name": "Acme"})
    assert "<img" not in html and "🌐" not in html


def test_a_dark_sites_logo_renders_on_its_plate():
    html = _render_header({**config._BRANDING_DEFAULTS, "company_name": "Solnest AI",
                           "logo_url": "https://solnestai.com/solnest-logo.png",
                           "website_url": "https://solnestai.com", "logo_background": "#0d0d0b"})
    assert 'src="https://solnestai.com/solnest-logo.png"' in html
    assert "background:#0d0d0b" in html and "solnestai.com" in html


@pytest.mark.parametrize("brand", [("#12110f", "#c0522b"), ("#1e40af", "#f59e0b")])
def test_the_css_and_chart_follow_the_brand(brand):
    import jinja2
    env = jinja2.Environment(autoescape=True)
    env.filters.update(rgb=te.rgb, lighten=te.lighten)
    head = TEMPLATE[:TEMPLATE.index("</style>")]
    css = env.from_string(head).render(branding={"primary_color": brand[0], "accent_color": brand[1],
                                                 "company_name": "", "logo_url": ""},
                                       property={"short_address": ""})
    assert f"--sol-forest:{brand[0]}" in css and f"--brand-rgb:{te.rgb(brand[0])}" in css
    assert "31, 60, 52" not in css
