"""The vendor cache must save money without ever being able to break a run."""
import time

import pytest

from scrapers import _cache


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("AIRROI_CACHE", "1")
    _cache._STATS.update(hit=0, miss=0, write=0)
    yield tmp_path


def test_roundtrip():
    _cache.put("airroi", "/listings", {"id": 1}, {"ok": True})
    assert _cache.get("airroi", "/listings", {"id": 1}) == {"ok": True}


def test_different_params_are_different_entries():
    """A changed radius/bedroom/currency must never be served a stale hit."""
    _cache.put("airroi", "/listings/comparables", {"bedrooms": 3}, ["three"])
    assert _cache.get("airroi", "/listings/comparables", {"bedrooms": 4}) is None


def test_param_order_does_not_matter():
    _cache.put("airroi", "/x", {"a": 1, "b": 2}, "v")
    assert _cache.get("airroi", "/x", {"b": 2, "a": 1}) == "v"


def test_expired_entry_is_a_miss(isolated_cache, monkeypatch):
    _cache.put("airroi", "/listings", {"id": 9}, {"stale": True})
    monkeypatch.setattr(_cache, "TTL_SECONDS", 0)
    time.sleep(0.01)
    assert _cache.get("airroi", "/listings", {"id": 9}) is None


def test_corrupt_entry_is_a_miss_not_an_error(isolated_cache):
    _cache.put("airroi", "/listings", {"id": 5}, {"good": 1})
    for f in isolated_cache.glob("*.json"):
        f.write_text("{ this is not json")
    assert _cache.get("airroi", "/listings", {"id": 5}) is None


def test_unwritable_cache_dir_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(_cache, "CACHE_DIR", tmp_path / "nope" / "\0bad")
    _cache.put("airroi", "/listings", {"id": 1}, {"a": 1})   # must not raise


def test_unserialisable_payload_does_not_raise():
    _cache.put("airroi", "/listings", {"id": 1}, {object()})  # must not raise


def test_disabled_by_env(monkeypatch):
    _cache.put("airroi", "/listings", {"id": 2}, {"v": 1})
    monkeypatch.setenv("AIRROI_CACHE", "0")
    assert not _cache.enabled()
    assert _cache.get("airroi", "/listings", {"id": 2}) is None


def test_write_is_atomic_no_tmp_left_behind(isolated_cache):
    _cache.put("airroi", "/listings", {"id": 7}, {"v": 1})
    assert list(isolated_cache.glob("*.tmp")) == []
    assert len(list(isolated_cache.glob("*.json"))) == 1
