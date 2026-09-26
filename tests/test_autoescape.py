"""Regression tests for HTML escaping in the rendered report.

The template used to run with autoescape=False under a comment claiming the
template managed its own escaping. It did not: there were zero escape filters
in 1,191 lines, so an Airbnb listing name or an LLM narrative containing markup
went into a client-facing, emailable HTML file verbatim.
"""
import jinja2
import pytest

from generators.methodology import build_methodology
from report.template_engine import TEMPLATES_DIR


def _env_from_source():
    """Rebuild the env the way render_report does, to assert on its config."""
    import inspect
    import report.template_engine as te
    src = inspect.getsource(te.render_report)
    assert "autoescape=True" in src, "render_report must enable autoescape"
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )


def test_render_report_enables_autoescape():
    _env_from_source()


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    "Cabin & Co </div><img src=x onerror=alert(1)>",
    '"><svg onload=alert(1)>',
])
def test_untrusted_text_is_escaped(hostile):
    env = _env_from_source()
    out = env.from_string("<h1>{{ name }}</h1>").render(name=hostile)
    body = out[len("<h1>"):-len("</h1>")]
    # No raw angle bracket may survive: that is what turns text into markup.
    assert "<" not in body and ">" not in body, body
    assert "&lt;" in body or "&amp;" in body


def test_methodology_strong_tags_still_render(monkeypatch):
    """The |safe lists must keep their markup, or the report loses its bolding."""
    env = _env_from_source()
    tpl = env.from_string("{% for i in items %}<li>{{ i|safe }}</li>{% endfor %}")
    out = tpl.render(items=["<strong>AirROI:</strong> data"])
    assert "<strong>AirROI:</strong>" in out


def test_methodology_escapes_its_own_interpolations():
    """data_sources is rendered |safe, so the market name inside it must be
    escaped at source or |safe re-opens the hole."""
    from schema import PropertyBasics
    prop = PropertyBasics(
        name="X", market='Aspen<script>alert(1)</script>', bedrooms=2,
        bathrooms=1.0, max_guests=4, address="1 Main St", short_address="Aspen",
    )
    # The market name is interpolated only when the market curve was used,
    # so exercise that branch.
    m = build_methodology(prop, [], "", "", calculator=None, seasonal_basis="market")
    joined = " ".join(m.data_sources)
    assert "<script>" not in joined, "market name reached a |safe list unescaped"
    assert "&lt;script&gt;" in joined
