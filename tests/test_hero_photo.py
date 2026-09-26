"""The subject's hero image must be a photo of the property.

Found 2026-09-25 on an address subject (1131 Tanrac Trl, Gatlinburg TN): the
report's hero was a Google Static Maps tile. Firecrawl's LLM extraction had
returned it as "the main property photo", Phase A trusts maps.googleapis.com
(for Street View), and so it shipped. The same address on an earlier run got
a 576px living-room shot instead: the extraction is not deterministic.

The page's own og:image is. On Zillow it is the listing's primary photo at
1536px (here, the front of the house), and it arrives in the metadata of the
same Firecrawl response, so preferring it costs nothing.
"""

from __future__ import annotations

import asyncio

import pytest

from scrapers import property_search as PS
from scrapers.property_search import _parse_firecrawl_result, pick_hero_image
from validators.sanity import _TRUSTED_HERO_HOSTS, check_subject_hero

# Real URLs observed on the Gatlinburg runs.
STATIC_MAP = (
    "https://maps.googleapis.com/maps/api/staticmap?center=35.74605,-83.481094"
    "&zoom=15&size=384x288&maptype=roadmap&scale=2"
)
ZILLOW_OG = "https://photos.zillowstatic.com/fp/6aac5f2186aa1f41c969c15ef2394619-cc_ft_1536.jpg"
ZILLOW_SMALL = "https://photos.zillowstatic.com/fp/42bb3e2a08118e2f5814a216ee99fc3c-sr_576_384.jpg"
ZILLOW_SMALL_UPGRADED = "https://photos.zillowstatic.com/fp/42bb3e2a08118e2f5814a216ee99fc3c-cc_ft_1536.jpg"
ZILLOW_LOGO = "https://www.zillowstatic.com/static/images/logos/zillow-logo-win8-tile.png"
ZILLOW_SHARE = "https://www.zillowstatic.com/static/images/social/share_thumbnail.png"
REALTOR_OG = "https://ap.rdcpix.com/63ed5f48bd475239828fbf9179d80f70l-m2245683187od-w640_h480.jpg"
AGENT_SITE_BANNER = "https://cdn.sitephotos.sierrastatic.com/1757_hero_caspian-3--20220203124943.jpg"
STREET_VIEW = "https://maps.googleapis.com/maps/api/streetview?size=640x480&location=35.74,-83.48"


# ── Picking ──

def test_static_map_is_never_the_hero():
    assert pick_hero_image(STATIC_MAP, {}) == ""


def test_portal_og_image_beats_the_extracted_hero():
    assert pick_hero_image(STATIC_MAP, {"ogImage": ZILLOW_OG}) == ZILLOW_OG
    assert pick_hero_image(ZILLOW_SMALL, {"og:image": ZILLOW_OG}) == ZILLOW_OG


def test_realtor_com_og_image_is_used():
    assert pick_hero_image("", {"ogImage": REALTOR_OG}).startswith(
        REALTOR_OG.split("-w640_h480")[0])


def test_non_portal_og_image_is_ignored():
    """On an agent website og:image was a site-wide banner, not the house."""
    assert pick_hero_image("", {"ogImage": AGENT_SITE_BANNER}) == ""


@pytest.mark.parametrize("junk", [ZILLOW_LOGO, ZILLOW_SHARE, "data:image/png;base64,AAAA",
                                  "https://example.com/brand.svg", "not a url"])
def test_logos_and_site_images_are_rejected(junk):
    assert pick_hero_image(junk, {"ogImage": junk}) == ""


def test_small_zillow_photo_is_requested_at_full_size():
    assert pick_hero_image(ZILLOW_SMALL, {}) == ZILLOW_SMALL_UPGRADED


def test_realtor_com_photo_is_requested_at_full_size():
    assert pick_hero_image("", {"ogImage": REALTOR_OG}) == REALTOR_OG.replace(
        "-w640_h480.jpg", "-w1536_h1152.jpg")


def test_street_view_is_only_a_last_resort():
    assert pick_hero_image(STREET_VIEW, {}) == STREET_VIEW
    assert pick_hero_image(STREET_VIEW, {"ogImage": ZILLOW_OG}) == ZILLOW_OG


def test_parse_uses_page_metadata():
    raw = {"bedrooms": 3, "hero_image_url": STATIC_MAP}
    assert _parse_firecrawl_result(raw, "u", {"ogImage": ZILLOW_OG})["hero_image_url"] == ZILLOW_OG
    assert _parse_firecrawl_result(raw, "u")["hero_image_url"] == ""


# ── An upgraded URL that does not resolve falls back to the original ──

def test_unresolvable_upgrade_falls_back(monkeypatch):
    async def dead(url):
        return False
    monkeypatch.setattr(PS, "_image_resolves", dead)
    out = asyncio.run(PS.confirm_hero_variant(ZILLOW_SMALL_UPGRADED, ZILLOW_SMALL))
    assert out == ZILLOW_SMALL


def test_resolvable_upgrade_is_kept(monkeypatch):
    async def live(url):
        return True
    monkeypatch.setattr(PS, "_image_resolves", live)
    out = asyncio.run(PS.confirm_hero_variant(ZILLOW_SMALL_UPGRADED, ZILLOW_SMALL))
    assert out == ZILLOW_SMALL_UPGRADED


# ── Phase A backstop ──

def test_phase_a_blocks_a_map_hero():
    failures = check_subject_hero(STATIC_MAP)
    assert any("map" in f.lower() for f in failures)


def test_phase_a_allows_street_view_and_portal_photos():
    for url in (STREET_VIEW, ZILLOW_OG, REALTOR_OG):
        assert check_subject_hero(url) == [], url


def test_realtor_com_photo_host_is_trusted():
    assert any("rdcpix.com" in h for h in _TRUSTED_HERO_HOSTS)
