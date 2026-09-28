"""The address search must return the unit that was asked for, or say it didn't.

Live run, 2026-09-28: "5005 Valley Drive Unit 13, Sun Peaks BC" came back as
"Stones Throw #12" (12-5005 Valley Drive). Ranking only looked at how much
the page extracted and at the town, so the better-documented neighbour won,
AirROI was queried with Unit 12's address, and nothing said so.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

import agent
from scrapers import property_search as ps

ASKED = "5005 Valley Drive Unit 13, Sun Peaks BC"


@pytest.mark.parametrize("text, expected", [
    (ASKED, ("5005", "13")),
    ("12-5005 Valley Drive, Sun Peaks, B.C. V0E 5N0", ("5005", "12")),
    ("Unit 13, 5005 Valley Drive", ("5005", "13")),
    ("5005 Valley Dr #013", ("5005", "13")),
    ("Apt 4B, 100 Main St, Austin TX 78701", ("100", "4B")),
    ("100 Main St Apt. 4B, Austin", ("100", "4B")),
    ("5005 Valley Drive, Suite No. 7", ("5005", "7")),
    ("Stones Throw #12", ("", "12")),
    ("812 Ski Mountain Rd, Gatlinburg TN", ("812", "")),
    ("Highway 99, Whistler BC V0N 1B4", ("", "")),
    ("Sun Peaks BC", ("", "")),
])
def test_street_and_unit(text, expected):
    assert ps.street_and_unit(text) == expected


def _hit(address, title, url, rich=True):
    return {"url": url, "metadata": {}, "json": {
        "address": address, "title": title, "market": "Sun Peaks",
        "bedrooms": 2 if rich else None, "bathrooms": 2 if rich else None,
        "sqft": 1000 if rich else None, "description": "Condo" if rich else "",
        "hero_image_url": "https://a0.muscache.com/im/pictures/x.jpg" if rich else "",
    }}


UNIT_12 = _hit("12-5005 Valley Drive, Sun Peaks, B.C. V0E 5N0", "Stones Throw #12",
               "https://www.sunpeakscondos.com/st12/")
UNIT_13_THIN = _hit("13-5005 Valley Drive, Sun Peaks, BC", "Stones Throw #13",
                    "https://example.com/st13/", rich=False)
OTHER_BUILDING = _hit("3250 Village Way, Sun Peaks, BC", "Village Condo",
                      "https://www.zillow.com/village/")


def _search(monkeypatch, hits):
    async def fake_post(endpoint, body):
        return {"data": hits}

    async def fake_hero(extracted, metadata=None):
        return extracted or ""

    async def no_scrape(url, expect_locality=""):
        return None

    monkeypatch.setattr(ps, "_firecrawl_post", fake_post)
    monkeypatch.setattr(ps, "resolved_hero", fake_hero)
    monkeypatch.setattr(ps, "scrape_listing_url", no_scrape)
    return asyncio.run(ps.search_for_property(ASKED))


def test_the_right_units_thin_page_beats_a_richer_neighbour(monkeypatch):
    got = _search(monkeypatch, [UNIT_12, UNIT_13_THIN])
    assert got["listing_url"] == "https://example.com/st13/"
    assert got["unit_mismatch"] == ""


def test_a_neighbouring_unit_is_returned_but_flagged(monkeypatch):
    got = _search(monkeypatch, [UNIT_12])
    assert got["listing_url"] == "https://www.sunpeakscondos.com/st12/"
    assert "Unit 13" in got["unit_mismatch"] and "Unit 12" in got["unit_mismatch"]


def test_another_street_number_is_never_the_subject(monkeypatch):
    assert _search(monkeypatch, [OTHER_BUILDING]) is None


def test_no_unit_asked_means_no_flag():
    assert ps.unit_mismatch("5005 Valley Drive, Sun Peaks BC",
                            {"raw_address": "12-5005 Valley Drive", "title": ""}) == ""


def test_a_listing_that_names_no_unit_is_flagged():
    note = ps.unit_mismatch(ASKED, {"raw_address": "5005 Valley Drive, Sun Peaks", "title": "Ski condo"})
    assert "does not say which unit" in note


def _resolve(monkeypatch, allow):
    async def fake_search(raw):
        return {**ps._parse_firecrawl_result(UNIT_12["json"], UNIT_12["url"]),
                "unit_mismatch": ps.unit_mismatch(ASKED, {"raw_address": UNIT_12["json"]["address"]})}

    monkeypatch.setattr(agent, "search_for_property", fake_search)
    args = SimpleNamespace(input=ASKED, allow_other_unit=allow, hero_url=None, listing_url=None,
                           market=None, beds=None, baths=None, guests=None)
    return asyncio.run(agent._resolve_subject(args))


def test_agent_stops_on_a_flagged_unit_before_any_paid_call(monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _resolve(monkeypatch, allow=False)
    assert e.value.code == 2
    out = capsys.readouterr().out
    assert "Asked for Unit 13" in out and "--allow-other-unit" in out


def test_allow_other_unit_continues(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    prop = _resolve(monkeypatch, allow=True)
    assert prop.bedrooms == 2
    assert "Continuing: --allow-other-unit" in capsys.readouterr().out


def test_missing_details_exit_cleanly_when_stdin_only_looks_like_a_terminal(monkeypatch, capsys):
    """On Windows the NUL device reports isatty() True, and Claude Code runs
    commands that way: the bedrooms prompt hit EOF and crashed with a
    traceback instead of naming the flags to pass (2026-09-28)."""
    async def fake_search(raw):
        return {**ps._parse_firecrawl_result({"address": "5005 Valley Dr #13, Sun Peaks, BC",
                                              "market": "Sun Peaks"}, "https://www.zillow.com/x/"),
                "unit_mismatch": ""}

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(agent, "search_for_property", fake_search)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", eof)
    args = SimpleNamespace(input=ASKED, allow_other_unit=False, hero_url=None, listing_url=None,
                           market=None, beds=None, baths=None, guests=None)
    with pytest.raises(SystemExit) as e:
        asyncio.run(agent._resolve_subject(args))
    assert e.value.code == 1
    assert "--beds, --baths, --guests" in capsys.readouterr().err
