"""The operator's --exclude must survive the filter-relaxation pass.

Reproduced on 2026-09-20: a 7-comp pool, --exclude naming two of them, pool
drops to 5, the relaxation re-read the unfiltered pool with no filters and no
exclusions, and both excluded comps came back with the run printing "Relaxing
filters to keep the report usable". This is the exact workflow the June note
prescribes for dropping an oversized comp.
"""

from __future__ import annotations

from comp_filters import select_comp_pool

_INLAND = ["Wifi", "Kitchen", "TV", "Heating", "Washer", "Dryer", "Hot tub"]


def _listing(name: str, amenities: list[str] | None = None) -> dict:
    return {
        "listing_info": {"listing_id": name, "listing_name": name},
        "property_details": {"amenities": list(amenities or _INLAND)},
    }


def _names(comps) -> list[str]:
    return [c["listing_info"]["listing_name"] for c in comps]


def _pool() -> list[dict]:
    return (
        [_listing("Modern condo in the heart of Sun Peaks"), _listing("Oversized 4-bed lodge")]
        + [_listing(f"comp{i}") for i in range(5)]
    )


def test_excluded_comps_stay_out_when_the_pool_goes_thin():
    sel = select_comp_pool(
        _pool(), drop_on_water=False, required_features=[],
        exclude_terms=["modern condo in the heart of sun peaks", "oversized 4-bed lodge"],
    )
    assert _names(sel.kept) == [f"comp{i}" for i in range(5)]
    assert sel.excluded == ["Modern condo in the heart of Sun Peaks", "Oversized 4-bed lodge"]


def test_relaxation_restores_filtered_comps_but_never_excluded_ones():
    pool = _pool()
    pool[2] = _listing("comp0", _INLAND + ["Waterfront"])      # dropped by the water gate
    pool[3] = _listing("comp1", _INLAND + ["Waterfront"])
    sel = select_comp_pool(
        pool, drop_on_water=True, required_features=[],
        exclude_terms=["oversized"],
    )
    assert sel.relaxed is True
    kept = _names(sel.kept)
    assert "comp0" in kept and "comp1" in kept, "the filter was relaxed"
    assert "Oversized 4-bed lodge" not in kept, "the operator's exclusion was not"
    assert "Modern condo in the heart of Sun Peaks" in kept


def test_no_relaxation_when_only_exclusions_thinned_the_pool():
    """Relaxing filters that dropped nothing changes nothing; say nothing."""
    sel = select_comp_pool(
        _pool(), drop_on_water=True, required_features=[],
        exclude_terms=["modern condo", "oversized"],
    )
    assert sel.relaxed is False
    assert len(sel.kept) == 5


def test_no_relaxation_when_the_pool_was_already_thin():
    pool = _pool()[:5]
    pool[0] = _listing("Modern condo in the heart of Sun Peaks", _INLAND + ["Waterfront"])
    sel = select_comp_pool(pool, drop_on_water=True, required_features=[], exclude_terms=[])
    assert sel.relaxed is False
    assert len(sel.kept) == 4


def test_exclusion_is_a_case_insensitive_substring_match():
    sel = select_comp_pool(_pool(), drop_on_water=False, required_features=[],
                           exclude_terms=["OVERSIZED"])
    assert "Oversized 4-bed lodge" not in _names(sel.kept)


def test_strict_report_is_returned_for_printing():
    pool = _pool()
    pool[2] = _listing("comp0", _INLAND + ["Waterfront"])
    sel = select_comp_pool(pool, drop_on_water=True, required_features=[], exclude_terms=[])
    assert sel.report.water_dropped == ["comp0"]
    assert sel.relaxed is False
    assert len(sel.kept) == 6
