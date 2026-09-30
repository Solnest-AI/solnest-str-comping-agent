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


# ── real STR sites, read by Firecrawl 2026-09-29 (colours as returned) ────
# Before this fix 5 of 13 lost their brand colour: the picker kept only a
# colour that was ALREADY dark, so black body text or a link colour won.

def test_a_muted_brand_colour_is_kept_not_swapped_for_the_link_colour():
    """solneststays.com (Wix): sage everywhere, navy links, browser-blue 'secondary'."""
    brand = {"colorScheme": "light", "colors": {
        "primary": "#8A8C6D", "secondary": "#0000EE", "accent": "#8A8C6D", "background": "#EEEEEE",
        "textPrimary": "#000000", "link": "#2B5672"},
        "components": {"buttonPrimary": {"background": "#8A8C6D"}}}
    primary, accent = bw.pick_colours(brand)
    assert accent == "#8a8c6d", "the site's own sage, as the site shows it"
    assert bw.contrast(primary, bw.REPORT_BG) >= 7
    assert _hue(primary) == pytest.approx(_hue("#8A8C6D"), abs=0.01), "a darker sage, not navy"
    assert "#0000ee" not in (primary, accent) and "#2b5672" not in (primary, accent)


def test_the_browser_default_link_colour_is_nobodys_brand():
    for default in ("#0000EE", "#551A8B"):
        primary, accent = bw.pick_colours({"colors": {"primary": default, "accent": default,
                                                      "textPrimary": "#222222"}})
        assert default.lower() not in (primary, accent)


def test_a_tan_brand_on_black_text_stays_tan():
    """theapresarcade.com / sunburstchalet.ca: primary tan, text black. Was black."""
    brand = {"colors": {"primary": "#C19B76", "accent": "#C19B76", "background": "#F5F5F5",
                        "textPrimary": "#000000", "link": "#9CA3AF"}}
    primary, accent = bw.pick_colours(brand)
    assert primary != "#000000" and _hue(primary) == pytest.approx(_hue("#C19B76"), abs=0.01)
    assert bw.contrast(primary, bw.REPORT_BG) >= 7 and bw.contrast(accent, bw.REPORT_BG) >= 3


def test_an_accent_is_darkened_as_little_as_possible():
    """houst.com: yellow accent, light-blue primary. Darkening the yellow gave mustard."""
    brand = {"colors": {"primary": "#3898EC", "secondary": "#0050BD", "accent": "#FFE403",
                        "background": "#FFFFFF", "textPrimary": "#000000", "link": "#FFE403"}}
    primary, accent = bw.pick_colours(brand)
    assert _hue(accent) == pytest.approx(_hue("#3898EC"), abs=0.01)
    assert bw.contrast(accent, bw.REPORT_BG) >= 3


def test_an_already_dark_brand_colour_is_left_alone():
    """legacyrnr.com, evolve.com, cozycohost.com: dark primaries, used as is."""
    for colour in ("#2B2F1B", "#093F46", "#283B62"):
        assert bw.pick_colours({"colors": {"primary": colour, "accent": "#C8AD7D"}})[0] == colour.lower()


def test_a_page_title_is_cut_back_to_the_brand_name():
    assert bw.clean_name("Air Concierge | Short Term Rental Management") == "Air Concierge"
    assert bw.clean_name("Legacy RnR") == "Legacy RnR"
    assert bw.clean_name("Stay-With-Somos") == "Stay-With-Somos"


def _hue(h: str) -> float:
    import colorsys
    return colorsys.rgb_to_hls(*bw._rgb(bw._hex(h)))[0]


def _png(path, fg, alpha_bg=True, box=False):
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (120, 60), (255, 255, 255, 255) if box else (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((10, 15, 110, 45), fill=fg)
    im.save(path)
    return path


def test_a_white_logo_on_a_light_site_gets_a_dark_plate(tmp_path):
    """legacyrnr.com's logo is LegacyRnR_Logo_white.png: invisible on cream."""
    logo = _png(tmp_path / "logo.png", (255, 255, 255, 255))
    assert bw.logo_is_light(logo) is True
    assert bw.plate_for({"colorScheme": "light"}, logo, "#2b2f1b") == "#2b2f1b"


def test_a_dark_logo_needs_no_plate_even_on_a_dark_site(tmp_path):
    logo = _png(tmp_path / "logo.png", (20, 20, 20, 255))
    assert bw.logo_is_light(logo) is False
    assert bw.plate_for(SOLNEST, logo, "#12110f") == ""


def test_a_logo_on_its_own_box_always_shows(tmp_path):
    logo = _png(tmp_path / "logo.png", (255, 255, 255, 255), box=True)
    assert bw.logo_is_light(logo) is False


def test_svg_logos_are_judged_by_their_fills(tmp_path):
    white = tmp_path / "w.svg"
    white.write_text('<svg><path fill="#FFF" d="M0"/><text style="fill: white">x</text></svg>', encoding="utf-8")
    plain = tmp_path / "p.svg"
    plain.write_text('<svg><path d="M0"/></svg>', encoding="utf-8")
    coloured = tmp_path / "c.svg"
    coloured.write_text('<svg><path fill="rgb(13, 82, 104)" d="M0"/></svg>', encoding="utf-8")
    assert bw.logo_is_light(white) is True
    assert bw.logo_is_light(plain) is False
    assert bw.logo_is_light(coloured) is False


def test_an_unreadable_logo_falls_back_to_the_site_scheme(tmp_path):
    junk = tmp_path / "logo.png"
    junk.write_bytes(b"not an image")
    assert bw.logo_is_light(junk) is None and bw.logo_is_light(None) is None
    assert bw.plate_for(SOLNEST, junk, "#12110f") == "#0d0d0b"
    assert bw.plate_for({"colorScheme": "light"}, junk, "#12110f") == ""


def test_the_student_can_supply_the_logo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw, "CACHE", tmp_path / ".cache")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(bw, "read_brand", lambda site: {"brandName": "Evolve", "colors": {"primary": "#093F46"},
                                                        "logo": "data:image/svg+xml;utf8,<svg/>"})
    fetched = []
    monkeypatch.setattr(bw, "fetch_logo", lambda url: fetched.append(url) or _png(tmp_path / "l.png", (9, 63, 70, 255)))
    assert bw.main(["evolve.com", "--logo", "https://cdn.example.com/evolve-logo.png"]) == 0
    out = json.loads((tmp_path / "branding.json").read_text(encoding="utf-8"))
    assert fetched == ["https://cdn.example.com/evolve-logo.png"]
    assert out["logo_url"] == "https://cdn.example.com/evolve-logo.png" and out["logo_background"] == ""

    monkeypatch.setattr(bw, "fetch_logo", lambda url: None)
    assert bw.main(["evolve.com", "--logo", "https://cdn.example.com/broken.png"]) == 0
    assert "did not load as an image" in capsys.readouterr().out
    assert json.loads((tmp_path / "branding.json").read_text(encoding="utf-8"))["logo_url"] == ""


def test_no_logo_found_tells_claude_how_to_get_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bw, "BRANDING", tmp_path / "branding.json")
    monkeypatch.setattr(bw, "CACHE", tmp_path / ".cache")
    monkeypatch.setattr(bw.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(bw, "read_brand", lambda site: {"brandName": "Ski", "colors": {"primary": "#C19B76"}})
    assert bw.main(["theapresarcade.com"]) == 0
    assert "--logo" in capsys.readouterr().out


# ── the setup gate ─────────────────────────────────────────────────────

def test_setup_asks_for_the_website_before_the_first_report(tmp_path, monkeypatch, capsys):
    kit_dir = tmp_path / "kit"
    kit_dir.mkdir()
    for marker in ("CONNECTIONS.md", "fan-out-env.sh"):
        (kit_dir / marker).write_text("", encoding="utf-8")
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs.kit, "find_kits", lambda: [kit_dir])
    monkeypatch.setattr(cs, "PROBES", {n: (lambda key: "ok") for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    monkeypatch.setattr(cs, "other_copy", lambda name: ("", ""))
    monkeypatch.setattr(cs, "BRANDING", tmp_path / "branding.json")
    assert cs.main(["--no-open"]) == 4
    assert "brand_from_website.py" in capsys.readouterr().out
    (tmp_path / "branding.json").write_text('{"company_name": "Acme Stays"}', encoding="utf-8")
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
    env.filters.update(rgb=te.rgb, lighten=te.lighten, format_bath=te.format_bath,
                       safe_url=te.safe_url)
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


def test_no_report_runs_before_the_student_is_branded(tmp_path, monkeypatch, capsys):
    """Neither the paid first pass nor the free re-render may ship 'Your Company'."""
    import asyncio

    import agent

    monkeypatch.setattr(config, "_BRANDING_PATH", tmp_path / "branding.json")
    for argv in (["agent.py", "--input", "https://www.airbnb.com/rooms/1"],
                 ["agent.py", "--render", "output/x.report-data.json"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as e:
            asyncio.run(agent.main())
        assert e.value.code == 2
    out = capsys.readouterr().out
    assert "brand_from_website.py" in out and "Nothing has been spent" in out

    (tmp_path / "branding.json").write_text('{"company_name": "Acme Stays"}', encoding="utf-8")
    assert agent._require_branding()


# ── one validator for setup and the run: READY means a real brand ─────────

@pytest.mark.parametrize("text", [
    "{}", "{", "[]", '"Acme"', '{"company_name": "Your Company"}', '{"company_name": "  "}',
    '{"company_name": 42}', '{"company_name": "Acme", "primary_color": "red"}',
    '{"company_name": "Acme", "logo_url": "javascript:alert(1)"}',
    (ROOT / "branding.example.json").read_text(encoding="utf-8"),
])
def test_an_unusable_branding_file_is_not_ready_anywhere(tmp_path, monkeypatch, capsys, text):
    path = tmp_path / "branding.json"
    path.write_text(text, encoding="utf-8")
    assert config.branding_problems(path)
    monkeypatch.setattr(config, "_BRANDING_PATH", path)
    assert config.branding_is_placeholder()
    import agent
    assert not agent._require_branding()
    assert "not usable yet" in capsys.readouterr().out


def test_a_name_only_brand_is_complete(tmp_path):
    path = tmp_path / "branding.json"
    path.write_text('{"company_name": "Acme Stays", "logo_url": "", "website_url": ""}', encoding="utf-8")
    assert config.branding_problems(path) == []


def test_setup_is_not_ready_on_a_placeholder_brand(tmp_path, monkeypatch, capsys):
    kit_dir = tmp_path / "kit"
    kit_dir.mkdir()
    for m in ("CONNECTIONS.md", "fan-out-env.sh"):
        (kit_dir / m).write_text("", encoding="utf-8")
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs.kit, "find_kits", lambda: [kit_dir])
    monkeypatch.setattr(cs, "PROBES", {n: (lambda key: "ok") for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    monkeypatch.setattr(cs, "other_copy", lambda name: ("", ""))
    monkeypatch.setattr(cs, "BRANDING", tmp_path / "branding.json")
    (tmp_path / "branding.json").write_text('{"company_name": "Your Company"}', encoding="utf-8")
    assert cs.main(["--no-open"]) == 4
    assert "placeholder" in capsys.readouterr().out
