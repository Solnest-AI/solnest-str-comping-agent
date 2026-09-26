"""
Adapter tests — guard the AirROI → scorer/CompProperty mappings.

Three things we need to be sure about:
  1. map_for_scorer() produces all the field names comp_scorer.py reads
     (right field names, right types, no None where a number is expected)
  2. to_comp_property() converts a scored dict into a renderable CompProperty
     with all template-required fields populated
  3. Edge cases — occupancy conversion (AirROI returns 0-1), missing fields,
     studios (0 bedrooms), etc.

FIXTURE DISCIPLINE (rewritten 2026-08-29):
The previous fixture carried `ttm_available_days=350` alongside
`ttm_days_reserved=215` — 565 nights in a 365-day year — and snake_case
amenity keys that match nothing in the real AirROI vocabulary. It made the
suite green on behaviour that blocked report generation in 5 of 6 markets.
Every number below is now DERIVED from nights booked so the AirROI invariants
hold, and `test_synthetic_fixture_obeys_airroi_invariants` enforces that.

Run: pytest tests/test_adapter.py -v
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from adapters.airroi_to_comp import (
    map_for_scorer,
    map_batch_for_scorer,
    to_comp_property,
    subject_for_scorer,
    _flatten_amenities,
    _amenities_to_badges,
    _coerce_occ_pct,
    _derive_revenue_potential,
)
from schema import PropertyBasics, CompProperty


# ── Realistic AirROI comp fixture ────────────────────────────────────────
#
# Night accounting, derived from ttm_days_reserved = 215:
#   ttm_total_days      365   (always 365 — invariant, 200/200 live records)
#   ttm_days_reserved   215   booked nights, the only free variable
#   ttm_blocked_days     15   owner-blocked / off-market
#   ttm_available_days  150 = 365 - 215      (UNSOLD nights, NOT inventory)
#   ttm_occupancy      0.589 = 215 / 365
#   ttm_adjusted_occ   0.614 = 215 / (365 - 15)
#
# Revenue basis:
#   ttm_avg_rate  425.50   room rate, fees EXCLUDED
#   room revenue  425.50 x 215 = 91,482.50
#   ttm_revenue   100,000  -> fee multiplier 1.09 (corpus range 0.78 - 1.59)
#   ttm_revpar    262.40 ~= ttm_avg_rate x ttm_occupancy x 1.047
#
# Amenities are exact members of the real 136-string vocabulary
# (tests/fixtures/amenity_vocab.json). The old snake_case keys matched nothing.

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
            "Hot tub", "Kitchen", "Washer", "EV charger",
            "Free parking on premises", "Wifi", "Heating",
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
        "ttm_revenue": 100000,
        "ttm_avg_rate": 425.50,
        "ttm_revpar": 262.40,
        "ttm_occupancy": 215 / 365,
        "ttm_adjusted_occupancy": 215 / 350,
        "ttm_total_days": 365,
        "ttm_blocked_days": 15,
        "ttm_available_days": 150,
        "ttm_days_reserved": 215,
        "ttm_avg_length_of_stay": 4.0,
        "l90d_revenue": 28000,
        "l90d_avg_rate": 500,
        "l90d_total_days": 90,
        "l90d_blocked_days": 3,
        "l90d_days_reserved": 58,
        "l90d_available_days": 32,
        "l90d_occupancy": 58 / 90,
        "l90d_adjusted_occupancy": 58 / 87,
    },
}

NIGHTS_BOOKED = 215
NIGHTS_LISTED = 350          # 365 total - 15 blocked
NIGHTS_UNSOLD = 150          # 365 total - 215 booked
ADJUSTED_OCC_PCT = round(215 / 350 * 100, 2)   # 61.43


def test_synthetic_fixture_obeys_airroi_invariants():
    """This module's fixture must be physically possible.

    Regression guard: the previous fixture claimed 350 available nights AND
    215 reserved nights in a 365-day year, which is why the old (inverted)
    availability handling looked correct.
    """
    pm = AIRROI_COMP_FIXTURE["performance_metrics"]
    assert pm["ttm_total_days"] == 365
    assert pm["ttm_available_days"] == pm["ttm_total_days"] - pm["ttm_days_reserved"]
    assert pm["ttm_occupancy"] == pytest.approx(pm["ttm_days_reserved"] / 365)
    assert pm["ttm_adjusted_occupancy"] == pytest.approx(
        pm["ttm_days_reserved"] / (pm["ttm_total_days"] - pm["ttm_blocked_days"])
    )
    assert pm["l90d_available_days"] == pm["l90d_total_days"] - pm["l90d_days_reserved"]


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
    assert out["annual_revenue"] == 100000        # fee-INCLUSIVE
    assert out["annual_revenue_raw"] == 100000
    # Room revenue is ttm_revpar x ttm_total_days, not ttm_avg_rate x nights:
    # ttm_avg_rate missed the rate paid by -13.8% to +18.9% (2026-09-25).
    assert out["room_revenue"] == pytest.approx(262.40 * 365)  # fee-EXCLUSIVE

    # Night accounting — three distinct quantities, never conflated
    assert out["nights_booked"] == NIGHTS_BOOKED
    assert out["nights_listed"] == NIGHTS_LISTED, "open inventory = total - blocked"
    assert out["nights_unsold"] == NIGHTS_UNSOLD, "ttm_available_days is UNSOLD nights"
    assert "days_available" not in out, (
        "days_available meant unsold nights and was read as open inventory. "
        "The name is gone on purpose — do not reintroduce it."
    )

    # Occupancy defaults to ADJUSTED (booked / open nights), not booked / 365
    assert out["occupancy_pct"] == ADJUSTED_OCC_PCT
    assert out["occupancy_raw_pct"] == round(215 / 365 * 100, 2)

    assert out["revpar"] == 262.40
    assert out["revenue_potential"] is not None
    assert out["revenue_potential"] > 0

    # Quality
    assert out["rating"] == 4.85
    assert out["rating_is_unrated"] is False
    assert out["reviews"] == 47
    assert out["review_count"] == 47

    # Amenities + text — scorer scans these as searchable text
    assert isinstance(out["amenities"], list)
    assert "hot tub" in out["amenities"]
    # scorer also reads "description" — must be a string (can be empty)
    assert isinstance(out["description"], str)


def test_map_for_scorer_revenue_potential_is_a_real_ceiling():
    """Revenue potential must sit above actual revenue — it is a ceiling.

    A comp card printing 'Revenue Potential' BELOW the 'Annual Revenue'
    directly above it happened on 62 of 150 live cards before the fix.
    """
    out = map_for_scorer(AIRROI_COMP_FIXTURE)
    assert out["revenue_potential"] >= out["annual_revenue"]


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
    # 298 booked of 350 open nights
    comp["performance_metrics"]["ttm_days_reserved"] = 298
    comp["performance_metrics"]["ttm_available_days"] = 365 - 298
    comp["performance_metrics"]["ttm_occupancy"] = 298 / 365
    comp["performance_metrics"]["ttm_adjusted_occupancy"] = 0.85
    out = map_for_scorer(comp)
    assert out["occupancy_pct"] == 85.0


def test_map_for_scorer_fully_booked_listing_is_not_read_as_idle():
    """ttm_available_days == 0 means SOLD OUT, not 'no inventory data'.

    Truthiness checks on this field turned a 100%-occupancy listing into the
    365-night default, i.e. the worst possible reading of the best comp.
    """
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["performance_metrics"] = dict(comp["performance_metrics"])
    comp["performance_metrics"]["ttm_days_reserved"] = 365
    comp["performance_metrics"]["ttm_blocked_days"] = 0
    comp["performance_metrics"]["ttm_available_days"] = 0
    comp["performance_metrics"]["ttm_occupancy"] = 1.0
    comp["performance_metrics"]["ttm_adjusted_occupancy"] = 1.0
    out = map_for_scorer(comp)
    assert out["nights_booked"] == 365
    assert out["nights_listed"] == 365
    assert out["nights_unsold"] == 0
    assert out["occupancy_pct"] == 100.0


def test_map_for_scorer_handles_studio():
    """0-bedroom (studio) is valid input — should not be coerced or rejected."""
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["property_details"] = dict(comp["property_details"])
    comp["property_details"]["bedrooms"] = 0
    comp["property_details"]["guests"] = 2
    out = map_for_scorer(comp)
    assert out["bedrooms"] == 0
    assert out["sleeps"] == 2


def test_map_for_scorer_treats_zero_rating_as_unrated_sentinel():
    """AirROI reports rating_overall 0.0 for 'too few reviews to rate'."""
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["ratings"] = dict(comp["ratings"])
    comp["ratings"]["rating_overall"] = 0.0
    out = map_for_scorer(comp)
    assert out["rating"] is None
    assert out["rating_is_unrated"] is True


def test_map_for_scorer_handles_missing_cover_photo():
    """Some listings lack cover photos. Don't crash."""
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["listing_info"] = dict(comp["listing_info"])
    comp["listing_info"].pop("cover_photo_url", None)
    out = map_for_scorer(comp)
    # Should still have listing_info but no cover_photo_url
    assert "listing_info" in out


def test_map_for_scorer_rejects_non_dict():
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
    assert cp.annual_revenue == 100000
    assert cp.occupancy_pct == ADJUSTED_OCC_PCT
    assert cp.nights_booked == NIGHTS_BOOKED
    assert cp.nights_listed == NIGHTS_LISTED
    assert cp.revpar == 262.40
    assert cp.revenue_potential >= cp.annual_revenue
    assert cp.superhost is True
    assert cp.guest_favorite is True
    assert cp.professional_management is False
    assert cp.cleaning_fee == 200
    assert cp.min_nights == 2
    assert not hasattr(cp, "days_available")


def test_to_comp_property_unrated_comp_keeps_rating_none():
    """None must survive to the template so it can print 'New listing'.

    0.0 would render as a literal zero-star rating on a client-facing card.
    """
    comp = dict(AIRROI_COMP_FIXTURE)
    comp["ratings"] = dict(comp["ratings"])
    comp["ratings"]["rating_overall"] = 0.0
    cp = to_comp_property(map_for_scorer(comp))
    assert cp.rating is None


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
    """Real AirROI amenities are Title Case display strings, not snake_case."""
    raw = ["Hot tub", "Pool", "Fire pit", "Wifi"]
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
        amenity_list=["Hot tub", "EV charger", "Fire pit"],
        limit=3,
        text_context="Ski-in/Out Chalet with Mountain View and Fireplace",
    )
    assert len(labels) == len(emojis)
    assert "Ski-in/Out" in labels
    assert "Views" in labels  # mountain view → Views
    assert "Hot Tub" in labels


def test_amenities_to_badges_ignores_non_selling_points():
    """Badges are an ALLOWLIST. Toiletries never earn a card slot.

    The old denylist-plus-fallback shipped cards headlining 'Hair Dryer',
    'Shampoo' and 'Hot Water' as features.
    """
    labels, _ = _amenities_to_badges(
        amenity_list=["Shampoo", "Hair dryer", "Hot water", "Bathtub", "Iron"],
        limit=3,
        text_context="A comfortable home",
    )
    assert labels == [], f"expected no badges from toiletries, got {labels}"


def test_coerce_occ_pct_handles_decimal_and_percent():
    assert _coerce_occ_pct(0.65) == 65.0
    assert _coerce_occ_pct(65) == 65.0
    assert _coerce_occ_pct(65.5) == 65.5
    assert _coerce_occ_pct(None) is None
    assert _coerce_occ_pct("bad") is None


def test_revenue_potential_uses_open_inventory_not_unsold_nights():
    """Ceiling = adr x open nights x achievable occupancy, plus cleaning fees.

    Signature takes `nights_listed` (total - blocked). Passing unsold nights
    here is what inverted the metric: the worst comps got the biggest ceiling.
    """
    from adapters.airroi_to_comp import DEFAULT_OCC_CEILING, DEFAULT_FEE_FACTOR

    rp = _derive_revenue_potential(annual_revenue=85000, adr=400, nights_listed=365)
    room = 400 * 365 * DEFAULT_OCC_CEILING
    expected = round(room + room * (DEFAULT_FEE_FACTOR - 1.0), 2)
    assert rp == pytest.approx(expected)

    # A listing with fewer open nights must get a SMALLER ceiling.
    rp_small = _derive_revenue_potential(annual_revenue=85000, adr=400, nights_listed=180)
    assert rp_small < rp


def test_revenue_potential_adds_cleaning_fees_when_known():
    """ttm_revenue is fee-inclusive, so the ceiling must be too."""
    from adapters.airroi_to_comp import DEFAULT_OCC_CEILING

    rp = _derive_revenue_potential(
        annual_revenue=85000, adr=400, nights_listed=365,
        cleaning_fee=250, avg_los=4.0,
    )
    ceiling_nights = 365 * DEFAULT_OCC_CEILING
    expected = round(400 * ceiling_nights + 250 * (ceiling_nights / 4.0), 2)
    assert rp == pytest.approx(expected)


def test_revenue_potential_returns_none_without_adr():
    """No ADR, no defensible ceiling.

    The old `annual_revenue * 1.5` fallback fired on 0 of 200 live records and
    was not a defensible number for a client-facing 'potential'.
    """
    assert _derive_revenue_potential(annual_revenue=85000, adr=None, nights_listed=365) is None
    assert _derive_revenue_potential(annual_revenue=85000, adr=400, nights_listed=0) is None


def test_map_batch_guarantees_potential_above_actual():
    """Pool-level pass: no comp may print a ceiling below its own actual."""
    batch = map_batch_for_scorer([AIRROI_COMP_FIXTURE])
    assert len(batch) == 1
    m = batch[0]
    assert m["revenue_potential"] >= m["annual_revenue"]
    assert "market_occ_ceiling" in m


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
        amenities=["Ski-in/Ski-out", "Indoor fireplace", "Hot tub"],
        latitude=50.8832, longitude=-119.8938,
    )
    estimate_data = {"average_daily_rate": 300, "revenue": 75000, "occupancy": 0.68}
    out = subject_for_scorer(prop, estimate_data)

    assert out["bedrooms"] == 2
    assert out["max_guests"] == 6
    assert out["guests"] == 6
    assert out["adr"] == 300
    assert out["airdna_adr"] == 300  # legacy key compat
    assert out["latitude"] == 50.8832
    assert out["longitude"] == -119.8938
    assert "Stone's Throw" in out["description"] or "Ski Condo" in out["description"]
    assert isinstance(out.get("host"), dict)
    assert isinstance(out.get("reviews"), list)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
