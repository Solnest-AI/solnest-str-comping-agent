"""
Adapter tests — guard the AirROI → scorer/CompProperty mappings.

Three things we need to be sure about:
  1. map_for_scorer() produces all the field names comp_scorer.py reads
     (right field names, right types, no None where a number is expected)
  2. to_comp_property() converts a scored dict into a renderable CompProperty
     with all template-required fields populated
  3. Edge cases — occupancy conversion (AirROI returns 0-1), missing fields,
     studios (0 bedrooms), etc.

Run: pytest tests/test_adapter.py -v
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from adapters.airroi_to_comp import (
    map_for_scorer,
    to_comp_property,
    subject_for_scorer,
    _flatten_amenities,
    _amenities_to_badges,
    _coerce_occ_pct,
    _derive_revenue_potential,
)
from schema import PropertyBasics, CompProperty


# ── Realistic AirROI comp fixture (shape verified against OpenAPI spec) ────

AIRROI_COMP_FIXTURE = {
    "listing_info": {
        "listing_id": 12345678,
        "listing_name": "Cozy Ski-in/out Chalet with Hot Tub",
        "description": "Beautiful mountain retreat with stunning views.",
        "listing_type": "Entire chalet",
        "room_type": "entire_home",
        "cover_photo_url": "https://a0.muscache.com/im/pictures/abc.jpg",
        "photos_count": 25,
        "guest_favorite": True,
    },
    "host_info": {
        "host_id": 9876543,
        "host_name": "Alice",
        "superhost": True,
        "professional_management": False,
    },
    "location_info": {
        "country_code": "CA",
        "country": "Canada",
        "region": "British Columbia",
        "locality": "Sun Peaks",
        "latitude": 50.8832,
        "longitude": -119.8938,
    },
    "property_details": {
        "guests": 8,
        "bedrooms": 3,
        "beds": 4,
        "baths": 2.0,
        "amenities": [
            "hot_tub", "kitchen", "washer", "ev_charger",
            "free_parking_on_premises", "wifi", "heating",
        ],
    },
    "booking_settings": {
        "instant_book": True,
        "min_nights": 2,
    },
    "pricing_info": {
        "currency": "CAD",
        "cleaning_fee": 200,
    },
    "ratings": {
        "num_reviews": 47,
        "rating_overall": 4.85,
        "rating_cleanliness": 4.9,
    },
    "performance_metrics": {
        "ttm_revenue": 95000,
        "ttm_avg_rate": 425.50,
        "ttm_occupancy": 0.615,
        "ttm_available_days": 350,
        "ttm_days_reserved": 215,
        "l90d_revenue": 28000,
        "l90d_avg_rate": 500,
        "l90d_occupancy": 0.72,
    },
}


# ── map_for_scorer() ─────────────────────────────────────────────────────

def test_map_for_scorer_produces_all_required_fields():
    """The scorer reads specific fields. Adapter must populate them all."""
    out = map_for_scorer(AIRROI_COMP_FIXTURE)

    # Identity / physical
    assert out["name"] == "Cozy Ski-in/out Chalet with Hot Tub"
    assert out["bedrooms"] == 3
    assert out["bathrooms"] == 2.0
    assert out["sleeps"] == 8
    assert out["max_guests"] == 8

    # Financial
    assert out["adr"] == 425.50
    assert out["adr_raw"] == 425.50
    assert out["annual_revenue"] == 95000
    assert out["annual_revenue_raw"] == 95000
    assert out["occupancy_pct"] == 61.5
    # days_available = 365 - blocked_days (industry standard: days on market)
    # Fixture has no blocked_days, so days_available = 365
    assert out["days_available"] == 365
    assert out["revenue_potential"] is not None
    assert out["revenue_potential"] > 0

    # Quality
    assert out["rating"] == 4.85
    assert out["reviews"] == 47
    assert out["review_count"] == 47

    # Amenities + text — scorer scans these as searchable text
    assert isinstance(out["amenities"], list)
    assert "hot tub" in out["amenities"]
    # scorer also reads "description" — must be a string (can be empty)
    assert isinstance(out["description"], str)


def test_map_for_scorer_preserves_original_fields():
    """to_comp_property reads listing_info, etc. from the mapped dict."""
    out = map_for_scorer(AIRROI_COMP_FIXTURE)
    assert out["listing_info"]["listing_id"] == 12345678
    assert out["listing_info"]["cover_photo_url"] == "https://a0.muscache.com/im/pictures/abc.jpg"
    assert out["amenities_raw"] == AIRROI_COMP_FIXTURE["property_details"]["amenities"]


def test_map_for_scorer_handles_decimal_occupancy():
    """AirROI returns occupancy as 0-1 decimal. Adapter must normalize to 0-100."""
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["performance_metrics"] = dict(comp["performance_metrics"])
    comp["performance_metrics"]["ttm_occupancy"] = 0.85
    out = map_for_scorer(comp)
    assert out["occupancy_pct"] == 85.0


def test_map_for_scorer_handles_studio():
    """0-bedroom (studio) is valid input — should not be coerced or rejected."""
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["property_details"] = dict(comp["property_details"])
    comp["property_details"]["bedrooms"] = 0
    comp["property_details"]["guests"] = 2
    out = map_for_scorer(comp)
    assert out["bedrooms"] == 0
    assert out["sleeps"] == 2


def test_map_for_scorer_handles_missing_cover_photo():
    """Some listings lack cover photos. Don't crash."""
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["listing_info"] = dict(comp["listing_info"])
    comp["listing_info"].pop("cover_photo_url", None)
    out = map_for_scorer(comp)
    # Should still have listing_info but no cover_photo_url
    assert "listing_info" in out


def test_map_for_scorer_rejects_non_dict():
    import pytest
    with pytest.raises(TypeError):
        map_for_scorer("not a dict")


# ── to_comp_property() ──────────────────────────────────────────────────

def test_to_comp_property_renders_all_required_fields():
    """The Jinja template reads specific CompProperty attrs. All must be set."""
    mapped = map_for_scorer(AIRROI_COMP_FIXTURE)
    cp = to_comp_property(mapped)

    assert isinstance(cp, CompProperty)
    assert cp.name == "Cozy Ski-in/out Chalet with Hot Tub"
    assert cp.image_url == "https://a0.muscache.com/im/pictures/abc.jpg"
    assert cp.airbnb_url == "https://www.airbnb.com/rooms/12345678"
    assert cp.bedrooms == 3
    assert cp.bathrooms == 2.0
    assert cp.sleeps == 8
    assert cp.rating == 4.85
    assert cp.review_count == 47
    assert cp.adr == 425.50
    assert cp.annual_revenue == 95000
    assert cp.occupancy_pct == 61.5
    assert cp.days_available == 365  # 365 - blocked_days (0 in fixture)
    assert cp.revenue_potential > 0


def test_to_comp_property_surfaces_text_signal_badges():
    """Ski-in/out in the listing name should be detected as a badge."""
    mapped = map_for_scorer(AIRROI_COMP_FIXTURE)
    cp = to_comp_property(mapped)
    assert "Ski-in/Out" in cp.feature_badges, (
        f"Expected Ski-in/Out from text signal, got {cp.feature_badges}"
    )
    assert "Hot Tub" in cp.feature_badges, (
        f"Expected Hot Tub from amenity list, got {cp.feature_badges}"
    )


def test_to_comp_property_synthesizes_airbnb_url_from_id():
    mapped = map_for_scorer(AIRROI_COMP_FIXTURE)
    cp = to_comp_property(mapped)
    assert cp.airbnb_url == "https://www.airbnb.com/rooms/12345678"


def test_to_comp_property_handles_missing_listing_id():
    """If listing_id is missing, airbnb_url should be empty string."""
    mapped = map_for_scorer(AIRROI_COMP_FIXTURE)
    mapped["listing_info"] = dict(mapped["listing_info"])
    mapped["listing_info"].pop("listing_id", None)
    cp = to_comp_property(mapped)
    assert cp.airbnb_url == ""


# ── Helpers ──────────────────────────────────────────────────────────────

def test_flatten_amenities_produces_keywords():
    raw = ["hot_tub", "pool", "fire_pit", "wifi"]
    out = _flatten_amenities(raw)
    assert "hot tub" in out
    assert "fire pit" in out
    assert "pool" in out


def test_flatten_amenities_handles_none():
    assert _flatten_amenities(None) == []
    assert _flatten_amenities([]) == []


def test_amenities_to_badges_text_signals_take_priority():
    """Text-context signals outrank list-only amenities for the badge limit."""
    labels, emojis = _amenities_to_badges(
        amenity_list=["hot_tub", "ev_charger", "fire_pit"],
        limit=3,
        text_context="Luxury Ski-in/Out Chalet with Mountain View and Fireplace",
    )
    assert "Ski-in/Out" in labels
    assert "Views" in labels  # mountain view → Views


def test_coerce_occ_pct_handles_decimal_and_percent():
    assert _coerce_occ_pct(0.65) == 65.0
    assert _coerce_occ_pct(65) == 65.0
    assert _coerce_occ_pct(65.5) == 65.5
    assert _coerce_occ_pct(None) is None
    assert _coerce_occ_pct("bad") is None


def test_revenue_potential_with_occupancy():
    """Primary formula: (revenue / occupancy) × 0.70."""
    rp = _derive_revenue_potential(annual_revenue=85000, adr=400, days_available=365, occupancy=0.50)
    assert rp == round((85000 / 0.50) * 0.70, 2)  # $119,000


def test_revenue_potential_fallback_adr():
    """When no occupancy, fall back to ADR × 365 × 0.70."""
    rp = _derive_revenue_potential(annual_revenue=85000, adr=400, days_available=365)
    assert rp == round(400 * 365 * 0.70, 2)


def test_revenue_potential_fallback_revenue_only():
    """When no ADR or occupancy, fall back to 1.3x annual_revenue."""
    rp = _derive_revenue_potential(annual_revenue=85000, adr=None, days_available=365)
    assert rp == round(85000 * 1.3, 2)


# ── subject_for_scorer() ────────────────────────────────────────────────

def test_subject_for_scorer_produces_scorer_shape():
    """Subject dict must include the exact fields rank_comps reads."""
    prop = PropertyBasics(
        address="5005 Valley Drive Unit 13, Sun Peaks BC",
        short_address="5005 Valley Drive Unit 13",
        market="Sun Peaks",
        bedrooms=2, bathrooms=2, max_guests=6,
        property_type="Ski Condo",
        title="Stone's Throw Ski-in/Out Condo",
        amenities=["ski-in/out", "fireplace", "hot tub"],
    )
    estimate_data = {"average_daily_rate": 300, "revenue": 75000, "occupancy": 0.68}
    out = subject_for_scorer(prop, estimate_data)

    assert out["bedrooms"] == 2
    assert out["max_guests"] == 6
    assert out["guests"] == 6
    assert out["adr"] == 300
    assert "Stone's Throw" in out["description"] or "Ski Condo" in out["description"]
    assert isinstance(out.get("host"), dict)
    assert isinstance(out.get("reviews"), list)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
