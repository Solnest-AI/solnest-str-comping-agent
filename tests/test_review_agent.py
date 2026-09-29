"""Regression tests for the agent.py fixes from the 2026-09-28 codebase review."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest

import agent
import comp_filters
from schema import PropertyBasics, SubjectPerformance


def _args(**kw):
    base = dict(input=None, allow_other_unit=False, hero_url=None, listing_url=None,
                market=None, beds=None, baths=None, guests=None)
    return SimpleNamespace(**{**base, **kw})


# ── CLI ───────────────────────────────────────────────────────────────

def test_missing_input_is_a_usage_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent.py"])
    with pytest.raises(SystemExit) as e:
        asyncio.run(agent.main())
    assert e.value.code == 2
    assert "--input is required" in capsys.readouterr().err


# ── Input detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("url, room", [
    ("https://www.airbnb.com/rooms/123", "123"),
    ("https://www.airbnb.ca/rooms/123?adults=2", "123"),
    ("https://www.airbnb.com.au/rooms/123", "123"),
    ("https://www.airbnb.co.uk/rooms/123", "123"),
    ("https://fr.airbnb.ca/rooms/123", "123"),
    ("https://m.airbnb.com/rooms/plus/456", "456"),
])
def test_every_airbnb_domain_goes_to_airroi(url, room):
    assert agent._is_airbnb_url(url)
    assert agent._airbnb_room_id(url) == room


@pytest.mark.parametrize("url", [
    "https://notairbnb.com/rooms/1", "https://www.zillow.com/homedetails/1", "123 Main St",
])
def test_non_airbnb_inputs_are_not_airbnb(url):
    assert not agent._is_airbnb_url(url)


@pytest.mark.parametrize("address, currency", [
    ("Whistler, British Columbia, Canada", "CA$"),
    ("Mont-Tremblant, Québec", "CA$"),
    ("Ontario, CA 91761", "$"),          # Ontario, California
    ("Nashville, TN", "$"),
])
def test_written_out_canadian_addresses_are_cad(address, currency):
    assert agent._detect_currency(address) == currency


@pytest.mark.parametrize("market, slug", [
    ("Lake Tahoe / Truckee", "Lake-Tahoe-Truckee"),
    ('Sun Peaks: "Village"?', "Sun-Peaks-Village"),
    ("", "report"),
    (None, "report"),
])
def test_failure_report_slug_is_a_safe_filename(market, slug):
    assert agent._file_slug(market) == slug


# ── Missing details on every path ─────────────────────────────────────

def test_airbnb_path_also_stops_on_missing_size(monkeypatch, capsys):
    """The Airbnb path returned before the missing-details check, so a listing
    AirROI sent without a size ran the paid comp search on 0BR / sleeps 0."""
    async def sizeless(listing_id, **kw):
        return {"listing_info": {"listing_name": "Cabin"},
                "location_info": {"locality": "Gatlinburg", "country_code": "US"},
                "property_details": {"bedrooms": None, "baths": None, "guests": None}}

    monkeypatch.setattr(agent, "get_listing", sizeless)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as e:
        asyncio.run(agent._resolve_subject(_args(input="https://www.airbnb.com/rooms/1")))
    assert e.value.code == 1
    assert "--beds, --baths, --guests" in capsys.readouterr().err


def test_airbnb_counts_sent_as_decimal_strings_parse(monkeypatch):
    async def listing(listing_id, **kw):
        return {"listing_info": {"listing_name": "Cabin"},
                "location_info": {"locality": "Gatlinburg", "country_code": "US"},
                "property_details": {"bedrooms": "3.0", "baths": 2, "guests": "8"}}

    monkeypatch.setattr(agent, "get_listing", listing)
    prop = asyncio.run(agent._resolve_subject(_args(input="https://www.airbnb.com/rooms/1")))
    assert (prop.bedrooms, prop.max_guests) == (3, 8)


def test_hero_url_override_applies_to_airbnb_links_too(monkeypatch):
    async def listing(listing_id, **kw):
        return {"listing_info": {"listing_name": "Cabin", "cover_photo_url": "https://x/old.jpg"},
                "location_info": {"locality": "Gatlinburg"},
                "property_details": {"bedrooms": 3, "baths": 2, "guests": 8}}

    monkeypatch.setattr(agent, "get_listing", listing)
    prop = asyncio.run(agent._resolve_subject(_args(
        input="https://www.airbnb.com/rooms/1", hero_url="https://a0.muscache.com/new.jpg")))
    assert prop.hero_image_url == "https://a0.muscache.com/new.jpg"


def test_prompted_zero_is_not_accepted(monkeypatch, capsys):
    """Three bad answers used to become 0 and the run carried on with it."""
    answers = iter(["", "x", "0"] * 3 + ["Gatlinburg"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    prop = PropertyBasics(address="a", short_address="a", market="Unknown Market",
                          bedrooms=0, bathrooms=0, max_guests=0)
    with pytest.raises(SystemExit) as e:
        agent._require_details(prop)
    assert e.value.code == 1
    assert "Still missing" in capsys.readouterr().err


# ── Currency consistency of the subject's own figures ─────────────────

def _perf_listing(revenue: float) -> dict:
    return {"listing_info": {"listing_id": 7},
            "performance_metrics": {"ttm_revenue": revenue, "ttm_occupancy": 0.5,
                                    "ttm_avg_rate": 200, "ttm_days_reserved": 150,
                                    "ttm_available_days": 150, "ttm_total_days": 365,
                                    "ttm_blocked_days": 65}}


def test_cad_subject_missing_from_pool_is_refetched_in_cad(monkeypatch):
    usd = agent.subject_performance_from_listing(_perf_listing(100_000))
    assert usd is not None
    seen = {}

    async def get_listing(listing_id, currency="usd", **kw):
        seen["currency"] = currency
        return _perf_listing(137_000)

    monkeypatch.setattr(agent, "get_listing", get_listing)
    prop = PropertyBasics(address="a", short_address="a", market="Sun Peaks", bedrooms=2,
                          bathrooms=1, max_guests=4, currency="CA$")
    prop.subject_performance = usd
    prop.airroi_listing_id = 7
    asyncio.run(agent._resource_subject_performance(prop, candidates_raw=[]))
    assert seen["currency"] == "native"
    assert prop.subject_performance.annual_revenue != usd.annual_revenue


def test_cad_subject_is_dropped_rather_than_shown_in_usd_when_refetch_fails(monkeypatch):
    async def broken(listing_id, currency="usd", **kw):
        raise agent.AirROIError(500, "down")

    monkeypatch.setattr(agent, "get_listing", broken)
    prop = PropertyBasics(address="a", short_address="a", market="Sun Peaks", bedrooms=2,
                          bathrooms=1, max_guests=4, currency="CA$")
    prop.subject_performance = agent.subject_performance_from_listing(_perf_listing(100_000))
    prop.airroi_listing_id = 7
    asyncio.run(agent._resource_subject_performance(prop, candidates_raw=[]))
    assert prop.subject_performance is None


# ── Revenue potential never invented ──────────────────────────────────

def test_no_p75_means_the_estimate_itself_not_a_multiplier():
    prop = PropertyBasics(address="a", short_address="a", market="m", bedrooms=2,
                          bathrooms=1, max_guests=4)
    r = agent._build_rentalizer({"revenue": 50_000, "average_daily_rate": 200,
                                 "occupancy": 0.5}, prop)
    assert r.revenue_potential == 50_000


# ── Liveness ──────────────────────────────────────────────────────────

def test_any_redirect_counts_as_live(monkeypatch):
    codes = {"/a": 200, "/b": 307, "/c": 308, "/d": 429, "/e": 404, "/f": 500}

    class Fake(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(codes[r.url.path]))
            super().__init__(*a, **kw)

    monkeypatch.setattr(agent.httpx, "AsyncClient", Fake)
    urls = [f"https://x.test{p}" for p in codes]
    assert asyncio.run(agent._check_liveness(urls)) == [True, True, True, True, False, False]


# ── Widening obeys the main pool's filters ────────────────────────────

def _raw(lid: str, name: str, amenities=()) -> dict:
    return {"listing_info": {"listing_id": lid, "listing_name": name},
            "property_details": {"amenities": list(amenities)}}


def test_widening_never_readmits_an_excluded_or_filtered_comp():
    filters = agent.PoolFilters(drop_on_water=False, required=[], lacking=["pool"],
                                exclude_terms=["oversized"])
    selection = comp_filters.PoolSelection(
        kept=[], report=comp_filters.FilterReport(), excluded=[],
        lacking_dropped=[("Some comp", ["pool"])])
    wider = [_raw("1", "Oversized lodge"), _raw("2", "Cabin with pool", ["Pool"]),
             _raw("3", "Plain cabin")]
    kept = filters.enforce_like(selection, wider)
    assert [agent._listing_id(c) for c in kept] == ["3"]


def test_widening_keeps_a_lacking_feature_the_main_pool_only_ranked_down():
    filters = agent.PoolFilters(drop_on_water=False, required=[], lacking=["pool"],
                                exclude_terms=[])
    selection = comp_filters.PoolSelection(
        kept=[], report=comp_filters.FilterReport(), excluded=[], lacking_relaxed=["pool"])
    kept = filters.enforce_like(selection, [_raw("2", "Cabin with pool", ["Pool"])])
    assert len(kept) == 1


# ── Monthly occupancy parsing (subject and comps share it) ────────────

def test_monthly_occupancy_is_calendar_indexed_and_percent():
    rows = [{"date": "2025-08", "occupancy": {"avg": 0.4}},
            {"date": "2025-01-01", "occupancy": 0.75},
            {"date": "bad"}, "junk", {"date": "2025-13", "occupancy": 0.5}]
    out = agent._monthly_occupancy(rows)
    assert out[7] == pytest.approx(40.0) and out[0] == pytest.approx(75.0)
    assert sum(v is not None for v in out) == 2


def test_subject_performance_type_is_importable():
    # _resource_subject_performance relies on the schema type being re-exported.
    assert SubjectPerformance is not None
