"""Tests for the universal property search module."""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from scrapers.property_search import PROPERTY_SCHEMA, _parse_firecrawl_result, complete_address


# ── Schema validation ────────────────────────────────────────────────

def test_property_schema_has_required_fields():
    """The JSON schema sent to Firecrawl must request all fields we need."""
    props = PROPERTY_SCHEMA["properties"]
    required = {"address", "bedrooms", "bathrooms", "sqft", "property_type",
                "description", "hero_image_url", "title", "market"}
    assert required.issubset(set(props.keys())), (
        f"Missing schema fields: {required - set(props.keys())}"
    )


# ── Result parsing ───────────────────────────────────────────────────

def test_parse_firecrawl_result_complete():
    """A complete Firecrawl JSON extraction should map to all dict fields."""
    raw = {
        "address": "102 Duck Landing Ln, Duck, NC 27949",
        "bedrooms": 6,
        "bathrooms": 7,
        "sqft": 3834,
        "property_type": "House",
        "description": "Beautiful oceanfront home",
        "hero_image_url": "https://example.com/photo.jpg",
        "title": "Duck Landing Estate",
        "market": "Duck",
    }
    result = _parse_firecrawl_result(raw, "https://zillow.com/123")
    assert result["bedrooms"] == 6
    assert result["bathrooms"] == 7.0
    assert result["sqft"] == 3834
    assert result["hero_image_url"] == "https://example.com/photo.jpg"
    assert result["listing_url"] == "https://zillow.com/123"
    assert result["raw_address"] == "102 Duck Landing Ln, Duck, NC 27949"


def test_parse_firecrawl_result_missing_fields():
    """Missing fields should return None values, not crash."""
    raw = {"address": "123 Main St", "bedrooms": 3}
    result = _parse_firecrawl_result(raw, "https://example.com")
    assert result["bedrooms"] == 3
    assert result["bathrooms"] is None
    assert result["hero_image_url"] == ""
    assert result["sqft"] is None


def test_parse_firecrawl_result_string_numbers():
    """Firecrawl may return numbers as strings — parser must coerce."""
    raw = {"bedrooms": "4", "bathrooms": "2.5", "sqft": "2,100"}
    result = _parse_firecrawl_result(raw, "https://example.com")
    assert result["bedrooms"] == 4
    assert result["bathrooms"] == 2.5
    assert result["sqft"] == 2100


# ── An address extracted without its town ────────────────────────────

REALTOR_CA_TITLE = ("For sale: 5050 VALLEY Drive Unit# 2, Sun Peaks, British Columbia V0E5N0 "
                    "- 10394991 | REALTOR.ca")


def test_street_only_address_is_completed_from_the_realtor_ca_title():
    """2026-10-22: realtor.ca extracted "5050 VALLEY Drive Unit# 2" and AirROI
    answered "No results found for address"."""
    raw = {"address": "5050 VALLEY Drive Unit# 2", "market": "Sun Peaks", "bedrooms": 3}
    result = _parse_firecrawl_result(raw, "https://www.realtor.ca/real-estate/30048282/x",
                                     {"title": REALTOR_CA_TITLE})
    assert result["raw_address"] == "5050 VALLEY Drive Unit# 2, Sun Peaks, British Columbia V0E5N0"


def test_zillow_title_ends_the_address_at_the_pipe():
    title = "123 Main St, Nashville, TN 37203 | MLS #2601234 | Zillow"
    assert complete_address("123 Main St", "Nashville", {"ogTitle": title}) == \
        "123 Main St, Nashville, TN 37203"


def test_title_given_as_a_list_is_read():
    assert complete_address("123 Main St", "", {"og:title": ["123 Main St, Duck, NC 27949 | Redfin"]}) == \
        "123 Main St, Duck, NC 27949"


def test_street_only_address_falls_back_to_the_extracted_town():
    assert complete_address("5050 VALLEY Drive Unit# 2", "Sun Peaks", {"title": "REALTOR.ca"}) == \
        "5050 VALLEY Drive Unit# 2, Sun Peaks"


def test_complete_address_is_left_alone():
    address = "102 Duck Landing Ln, Duck, NC 27949"
    assert complete_address(address, "Somewhere Else", {"title": "1 Other Rd, Elsewhere | Zillow"}) == address


def test_town_already_in_the_address_is_not_added_twice():
    assert complete_address("12 Sun Peaks Rd", "Sun Peaks") == "12 Sun Peaks Rd"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
