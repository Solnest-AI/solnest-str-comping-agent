"""A comp is usable only when its listing AND its cover photo are live.

Found 2026-09-26 on a random test run (Destin, airbnb 1179026498260706170):
the rescue pass admitted "Beachfront end unit condo w/close dining", whose
listing page was live but whose cover photo returned 404. Liveness only ever
probed the listing page, so Phase A caught the dead photo afterwards and
blocked the whole report over one comp's picture, with a replacement
candidate sitting unused.
"""

from __future__ import annotations

import asyncio

import agent


def _comp(listing_id: str, photo: str) -> dict:
    return {"listing_info": {"listing_id": listing_id, "cover_photo_url": photo}}


def test_dead_photo_makes_a_live_listing_unusable(monkeypatch):
    dead = "https://a0.muscache.com/im/pictures/dead.jpeg"

    async def fake_liveness(urls):
        return [bool(u) and u != dead for u in urls]

    monkeypatch.setattr(agent, "_check_liveness", fake_liveness)
    comps = [_comp("1", "https://a0.muscache.com/im/pictures/ok.jpeg"), _comp("2", dead),
             _comp("3", "")]
    assert asyncio.run(agent._check_comps_usable(comps)) == [True, False, False]


def test_dead_listing_is_unusable_even_with_a_live_photo(monkeypatch):
    async def fake_liveness(urls):
        return ["rooms/9" not in u for u in urls]

    monkeypatch.setattr(agent, "_check_liveness", fake_liveness)
    assert asyncio.run(agent._check_comps_usable(
        [_comp("9", "https://a0.muscache.com/im/pictures/ok.jpeg")])) == [False]
