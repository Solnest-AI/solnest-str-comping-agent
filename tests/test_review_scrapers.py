"""Regressions from the 2026-09-28 codebase review of the scrapers, kit, config
and email layers. Hermetic: no network, no keys.
"""

from __future__ import annotations

import asyncio
import json
import ssl

import httpx
import pytest

import config
import kit
from report import email_sender
from scrapers import _cache, airroi, property_search as ps
from scrapers.airbnb import parse_airbnb_html

URL = "https://www.airbnb.ca/rooms/1"


def _page(head: str = "", body: str = "") -> str:
    return (
        '<html><head><meta property="og:title" content="Cabin in Peachland · ★4.9 · '
        f'2 bedrooms · 3 beds · 1 bath">{head}</head><body>{body}</body></html>'
    )


# ── email_sender ─────────────────────────────────────────────────────

class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, **kwargs):
        self.host, self.port, self.kwargs = host, port, kwargs
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.sent.append(msg)


def _send(monkeypatch, tmp_path, brand, address="1 Main St"):
    _FakeSMTP.instances.clear()
    report = tmp_path / "my report.html"
    report.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(config, "BRANDING", brand)
    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", _FakeSMTP)
    email_sender.send_report_email("you@example.com", address, report)
    server = _FakeSMTP.instances[0]
    msg = server.sent[0]
    return server, msg, msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


_BRAND = {
    "company_name": "Acme", "tagline": "Comps", "logo_url": "https://x.test/logo.png",
    "website_url": "https://x.test", "primary_color": "#112233", "accent_color": "#445566",
}


def test_email_verifies_tls_and_times_out(monkeypatch, tmp_path):
    server, _, _ = _send(monkeypatch, tmp_path, dict(_BRAND))
    ctx = server.kwargs["context"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert server.kwargs["timeout"] == 30


def test_email_escapes_every_interpolated_string(monkeypatch, tmp_path):
    brand = dict(_BRAND, company_name='<b>"Evil"</b>', tagline="<i>t</i>",
                 logo_url='https://x.test/l.png" onerror="alert(1)',
                 website_url='https://x.test/?a=1&b="2"')
    _, _, body = _send(monkeypatch, tmp_path, brand, address="<script>x</script> & Co")
    assert "<script>" not in body and "<b>" not in body and "<i>t</i>" not in body
    assert 'onerror="alert' not in body
    assert "&lt;script&gt;x&lt;/script&gt; &amp; Co" in body


def test_email_subject_cannot_inject_headers(monkeypatch, tmp_path):
    brand = dict(_BRAND, company_name="Acme\nBcc: evil@example.com")
    _, msg, _ = _send(monkeypatch, tmp_path, brand)
    assert "\n" not in msg["Subject"] and msg["Bcc"] is None


def test_email_uses_branding_colours_not_hardcoded_ones(monkeypatch, tmp_path):
    _, _, body = _send(monkeypatch, tmp_path, dict(_BRAND))
    assert "#112233" in body and "#445566" in body
    assert "#1f3c34" not in body and "#4b7c6b" not in body


def test_email_omits_logo_and_link_when_branding_has_neither(monkeypatch, tmp_path):
    _, _, body = _send(monkeypatch, tmp_path, dict(_BRAND, logo_url="", website_url=""))
    assert "<img" not in body and "<a " not in body


def test_email_attachment_filename_with_a_space_is_quoted(monkeypatch, tmp_path):
    _, msg, _ = _send(monkeypatch, tmp_path, dict(_BRAND))
    assert msg.get_payload()[1].get_filename() == "my report.html"


# ── kit.py ───────────────────────────────────────────────────────────

def test_non_utf8_env_reads_as_empty_not_a_crash(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("KEY=abc\n".encode("utf-16"))
    assert kit.read_env(env) == {}


def test_rewriting_a_non_utf8_env_refuses_and_leaves_it_alone(tmp_path):
    env = tmp_path / ".env"
    raw = "KEY=abc\n".encode("utf-16")
    env.write_bytes(raw)
    with pytest.raises(ValueError, match="UTF-8"):
        kit.set_value(env, "KEY", "new")
    assert env.read_bytes() == raw
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_read_env_matches_dotenv_for_comments_quotes_and_export(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "A=abc # note\n"
        'B="abc" # note\n'
        "C='a # b'\n"
        "export D=xyz\n"
        "export E=\n"
        'F="a # b"\n'
        "G=ab#c\n"
        "H=\n",
        encoding="utf-8",
    )
    assert kit.read_env(env) == {
        "A": "abc", "B": "abc", "C": "a # b", "D": "xyz", "E": "",
        "F": "a # b", "G": "ab#c", "H": "",
    }


def _make_kit(d):
    d.mkdir(parents=True)
    (d / "CONNECTIONS.md").write_text("", encoding="utf-8")
    (d / "fan-out-env.sh").write_text("", encoding="utf-8")
    return d


def test_find_kit_walks_two_levels_not_three(tmp_path, monkeypatch):
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    kit._FOUND.clear()
    _make_kit(tmp_path / "Documents" / "a" / "b" / "str-secrets-connections")
    assert kit.find_kit(home=tmp_path, near=tmp_path / "x" / "y") is None
    two = _make_kit(tmp_path / "Documents" / "a" / "str-secrets-connections")
    assert kit.find_kit(home=tmp_path, near=tmp_path / "x" / "y") == two.resolve()


def test_overlapping_roots_are_walked_once(tmp_path, monkeypatch):
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    kit._FOUND.clear()
    docs = tmp_path / "Documents"
    (docs / "app").mkdir(parents=True)
    listed = []
    real = kit.Path.iterdir

    def spy(self):
        listed.append(self.resolve())
        return real(self)

    monkeypatch.setattr(kit.Path, "iterdir", spy)
    kit.find_kit(home=tmp_path, near=docs / "app")   # near.parent IS ~/Documents
    assert listed.count(docs.resolve()) == 1


def test_find_kit_result_is_remembered_but_a_miss_is_not(tmp_path, monkeypatch):
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    kit._FOUND.clear()
    near = tmp_path / "x" / "y"
    assert kit.find_kit(home=tmp_path, near=near) is None
    k = _make_kit(tmp_path / "Desktop" / "str-secrets-connections")
    assert kit.find_kit(home=tmp_path, near=near) == k.resolve()

    def no_walk(*_a):
        raise AssertionError("second call must not walk the disk")

    monkeypatch.setattr(kit, "_walk", no_walk)
    assert kit.find_kit(home=tmp_path, near=near) == k.resolve()


# ── config.py ────────────────────────────────────────────────────────

def test_blank_setting_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("NARRATIVE_MODEL", "")
    assert config._get("NARRATIVE_MODEL", "claude-sonnet-5") == "claude-sonnet-5"
    monkeypatch.setenv("NARRATIVE_MODEL", "   ")
    assert config._get("NARRATIVE_MODEL", "claude-sonnet-5") == "claude-sonnet-5"
    monkeypatch.setenv("NARRATIVE_MODEL", "haiku")
    assert config._get("NARRATIVE_MODEL", "claude-sonnet-5") == "haiku"


def test_dead_airbtics_and_ski_settings_are_gone():
    assert not hasattr(config, "AIRBTICS_API_KEY")
    assert not hasattr(config, "AIRBTICS_BASE_URL")
    assert not hasattr(config, "SKI_RESORT_SEASONAL_TEMPLATE")


@pytest.mark.parametrize("servers", [
    [],
    "text",
    {"a": {"url": "https://mcp.airroi.com", "headers": ["X-API-KEY"], "env": "nope"}},
    {"a": {"url": ["https://mcp.airroi.com"], "headers": None, "env": []}},
    {"a": {"url": 5, "args": 7}},
    {"a": {"command": "python", "args": [None, 3, {"x": 1}]}},
])
def test_malformed_mcp_servers_read_as_no_key(tmp_path, servers):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    assert config.key_from_connections_kit("AIRROI_API_KEY", p) == ""


# ── scrapers/_cache.py ───────────────────────────────────────────────

@pytest.mark.parametrize("payload", ["[]", "null", "[1, 2]", '"text"', "3", '{"at": "soon", "data": 1}'])
def test_cache_file_of_the_wrong_shape_is_a_miss(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("AIRROI_CACHE", "1")
    _cache.put("airroi", "/x", {"a": 1}, {"ok": 1})
    for f in tmp_path.glob("*.json"):
        f.write_text(payload, encoding="utf-8")
    assert _cache.get("airroi", "/x", {"a": 1}) is None


@pytest.mark.parametrize("raw,want", [("", 86400), ("   ", 86400), ("abc", 86400), ("60", 60)])
def test_cache_ttl_tolerates_blank_or_bad_values(monkeypatch, raw, want):
    monkeypatch.setenv("AIRROI_CACHE_TTL", raw)
    assert _cache._ttl() == want


# ── scrapers/airroi.py ───────────────────────────────────────────────

@pytest.fixture
def airroi_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AIRROI_API_KEY", "ar-test-key")
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("AIRROI_CACHE", "1")

    async def no_sleep(_s):
        pass

    monkeypatch.setattr(airroi.asyncio, "sleep", no_sleep)


def _run_post(handler, endpoint="/markets/metrics/occupancy", body=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await airroi._post(endpoint, body or {"market": {"country": "US"}}, client=c)

    return asyncio.run(go())


def test_post_retries_a_503_like_get(airroi_env):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(503, json={"message": "busy"})
        return httpx.Response(200, json={"results": [{"date": "2026-01-01"}]})

    assert _run_post(handler)["results"]
    assert len(calls) == 3
    assert calls[0].method == "POST" and calls[0].headers["X-API-KEY"] == "ar-test-key"


def test_post_transport_error_becomes_airroi_error(airroi_env):
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    with pytest.raises(airroi.AirROIError) as e:
        _run_post(handler)
    assert e.value.status == airroi.NO_HTTP_STATUS


def test_post_non_json_200_is_an_airroi_error_not_a_value_error(airroi_env):
    with pytest.raises(airroi.AirROIError, match="non-JSON"):
        _run_post(lambda req: httpx.Response(200, text="<html>maintenance</html>"))


def test_post_key_failure_is_not_absorbed(airroi_env):
    with pytest.raises(kit.KeyFailure):
        _run_post(lambda req: httpx.Response(402, json={"message": "no credit ar-test-key"}))


def test_a_json_list_200_is_an_airroi_error(airroi_env):
    with pytest.raises(airroi.AirROIError, match="unexpected list"):
        _run_post(lambda req: httpx.Response(200, json=[1, 2]))


def test_error_body_never_echoes_the_key(airroi_env):
    with pytest.raises(airroi.AirROIError) as e:
        _run_post(lambda req: httpx.Response(422, json={"message": "bad ar-test-key"}))
    assert "ar-test-key" not in str(e.value)


def test_an_empty_comparables_payload_is_not_cached(airroi_env):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"listings": []})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            for _ in range(2):
                assert await airroi.get_comparables(
                    address="1 Main St", bedrooms=2, baths=1, guests=4, client=c) == []

    asyncio.run(go())
    assert len(calls) == 2


def test_a_real_payload_is_still_cached(airroi_env):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"listings": [{"listing_info": {"listing_id": 1}}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            for _ in range(2):
                await airroi.get_comparables(
                    address="1 Main St", bedrooms=2, baths=1, guests=4, client=c)

    asyncio.run(go())
    assert len(calls) == 1


def test_new_client_honours_proxy_environment(monkeypatch):
    """A custom transport= would make httpx ignore HTTPS_PROXY."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    async def go():
        async with airroi._new_client() as c:
            return bool(c._mounts)

    assert asyncio.run(go())


# ── scrapers/airbnb.py ───────────────────────────────────────────────

def test_the_word_superhost_alone_is_not_a_superhost():
    page = _page(body="<p>Learn about Superhost</p><script>var badge='superhost'</script>")
    assert parse_airbnb_html(page, URL).is_superhost is False


def test_a_true_superhost_flag_is_read():
    page = _page(body='<script>{"host":{"isSuperhost":true}}</script>')
    assert parse_airbnb_html(page, URL).is_superhost is True


def test_the_listings_own_flag_beats_another_hosts_in_the_page():
    data = {"props": {"pageProps": {"listing": {"name": "Cabin", "isSuperhost": False}}}}
    page = _page(
        head=f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>',
        body='<script>{"similar":{"isSuperhost":true}}</script>',
    )
    assert parse_airbnb_html(page, URL).is_superhost is False


def test_null_levels_in_next_data_do_not_crash():
    data = {"props": {"pageProps": {"listingData": None, "listing": None}}}
    page = _page(head=f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>')
    assert parse_airbnb_html(page, URL).title


def _ld_page(ld: dict) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(dict(ld, **{'@type': 'VacationRental'}))}</script></head><body></body></html>"
    )


def test_ld_json_empty_image_list_and_imageobject():
    assert parse_airbnb_html(_ld_page({"name": "Cabin", "image": []}), URL).hero_image_url == ""
    obj = {"name": "Cabin", "image": {"@type": "ImageObject", "url": "https://x.test/a.jpg"}}
    assert parse_airbnb_html(_ld_page(obj), URL).hero_image_url == "https://x.test/a.jpg"


def test_ld_json_without_an_address_does_not_yield_a_bare_comma():
    prop = parse_airbnb_html(_ld_page({"name": "Cabin", "address": {}}), URL)
    assert "," not in prop.address.strip(", ") and prop.address.strip() != ","
    partial = parse_airbnb_html(
        _ld_page({"name": "Cabin", "address": {"addressLocality": "Peachland"}}), URL)
    assert partial.address == "Peachland"


def test_placeholder_and_out_of_range_coordinates_are_skipped():
    page = _page(body='<script>{"lat":0,"lng":0}{"lat":95.0,"lng":10}{"lat":49.1,"lng":-122.5}</script>')
    prop = parse_airbnb_html(page, URL)
    assert (prop.latitude, prop.longitude) == (49.1, -122.5)
    only_zero = parse_airbnb_html(_page(body='<script>{"lat":0.0,"lng":0.0}</script>'), URL)
    assert only_zero.latitude is None and only_zero.longitude is None


def test_review_count_with_thousands_separator():
    page = _page(body="<p>1,234 reviews</p><p>12 reviews</p>")
    assert parse_airbnb_html(page, URL).review_count == 1234


@pytest.mark.parametrize("title,want", [
    ("2 bedrooms · 1 half-bath", 0.5),
    ("2 bedrooms · 2 baths · 1 half-bath", 2.5),
    ("2 bedrooms · 2 half baths", 1.0),
    ("2 bedrooms · 1 private bath", 1.0),
    ("2 bedrooms · 2.5 baths", 2.5),
])
def test_half_baths_count_half(title, want):
    page = (f'<html><head><meta property="og:title" content="Cabin in Peachland · {title}">'
            "</head><body></body></html>")
    assert parse_airbnb_html(page, URL).bathrooms == want


# ── scrapers/property_search.py ──────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("1500 4th Ave, Kelowna BC", ("1500", "")),
    ("78-261 Manukai St Unit 2305, Waipahu, HI", ("78-261", "2305")),
    ("12-5005 Valley Drive, Sun Peaks, B.C.", ("5005", "12")),
    ("Unit 12-5005 Valley Drive, Sun Peaks", ("5005", "12")),
    ("5005 Valley Drive Unit 13, Sun Peaks BC", ("5005", "13")),
    ("Unit 13, 5005 Valley Drive", ("5005", "13")),
])
def test_street_and_unit_shapes(text, want):
    assert ps.street_and_unit(text) == want


def test_accented_letters_fold_instead_of_becoming_spaces():
    assert ps._norm("Québec") == "quebec"
    assert ps._is_region_token("Québec")
    assert ps.query_locality("Montréal, Québec") == "Montréal"
    assert ps._mentions_locality("Montreal", "12 Rue X, Montréal, QC")


def test_kelowna_is_not_west_kelowna():
    assert not ps._mentions_locality("Kelowna", "West Kelowna, BC")
    assert not ps.locality_agrees("Kelowna", {"raw_address": "1 Main St, West Kelowna, BC"})
    assert ps._mentions_locality("Kelowna", "123 Lakeshore Dr North, Kelowna, BC")
    assert ps._mentions_locality("West Kelowna", "West Kelowna, BC")
    assert ps.locality_agrees("Kelowna", {"raw_address": "1 Main St, Kelowna, BC"})


def _fc_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(ps.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    monkeypatch.setattr(ps.config, "FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(ps, "_RETRY_DELAY", 0)


def test_firecrawl_non_dict_body_is_an_empty_result(monkeypatch):
    _fc_client(monkeypatch, lambda req: httpx.Response(200, json=[1, 2]))
    assert asyncio.run(ps._firecrawl_post("/search", {})) == {}


def test_firecrawl_retries_a_503_then_succeeds(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={"data": []})

    _fc_client(monkeypatch, handler)
    assert asyncio.run(ps._firecrawl_post("/search", {})) == {"data": []}
    assert len(calls) == 2


def test_firecrawl_gives_up_after_the_attempt_cap(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429)

    _fc_client(monkeypatch, handler)
    assert asyncio.run(ps._firecrawl_post("/search", {})) == {}
    assert len(calls) == ps._ATTEMPTS


def test_firecrawl_retries_connect_errors(monkeypatch):
    calls = []

    def connect(req):
        calls.append(req)
        raise httpx.ConnectError("down", request=req)

    _fc_client(monkeypatch, connect)
    assert asyncio.run(ps._firecrawl_post("/search", {})) == {}
    assert len(calls) == ps._ATTEMPTS


def test_firecrawl_does_not_retry_a_read_timeout(monkeypatch):
    calls = []

    def slow(req):
        calls.append(req)
        raise httpx.ReadTimeout("slow", request=req)

    _fc_client(monkeypatch, slow)
    assert asyncio.run(ps._firecrawl_post("/search", {})) == {}
    assert len(calls) == 1


def test_firecrawl_key_failure_is_not_retried(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(401, text="nope")

    _fc_client(monkeypatch, handler)
    with pytest.raises(kit.KeyFailure):
        asyncio.run(ps._firecrawl_post("/search", {}))
    assert len(calls) == 1


def test_junk_rows_in_a_search_response_are_ignored(monkeypatch):
    async def fake_post(endpoint, body):
        return {"data": ["junk", None, 3, {"url": "https://x.test", "json": None}]}

    monkeypatch.setattr(ps, "_firecrawl_post", fake_post)
    monkeypatch.setattr(ps, "resolved_hero", lambda *a, **k: asyncio.sleep(0, ""))
    assert asyncio.run(ps.search_hero_image("1 Main St")) is None


def test_scrape_with_a_non_dict_data_payload_returns_none(monkeypatch):
    async def fake_post(endpoint, body):
        return {"data": ["not", "a", "dict"]}

    monkeypatch.setattr(ps, "_firecrawl_post", fake_post)
    assert asyncio.run(ps.scrape_listing_url("https://x.test/l")) is None

