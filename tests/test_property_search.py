"""Tests for the universal property search module."""

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from scrapers.property_search import PROPERTY_SCHEMA, _parse_firecrawl_result


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


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
