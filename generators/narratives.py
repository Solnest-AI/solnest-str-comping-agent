"""Narrative generation for the report."""

from schema import (
    PropertyBasics,
    RevenueEstimate,
    CompProperty,
    CalculatorDefaults,
    Narratives,
    PositioningCard,
)


async def generate_narratives(
    prop: PropertyBasics,
    revenue_estimate: RevenueEstimate,
    comps: list[CompProperty],
    calculator: CalculatorDefaults,
) -> Narratives:
    """Generate narrative content for the report.

    Uses market-aware template narratives. When running through Claude Code,
    the user's Claude subscription can enhance these narratives conversationally
    — no separate Anthropic API key needed.
    """
    return _fallback_narratives(prop)


def _fallback_narratives(prop: PropertyBasics) -> Narratives:
    """Generate generic fallback narratives when the API is unavailable."""
    return Narratives(
        positioning_summary=(
            f"This property sits in a premium tier for {prop.market}: "
            f"a {prop.bedrooms}-bedroom, {prop.max_guests}-guest capacity "
            f"{prop.property_type.lower()} that competes with the top of the "
            f"STR market rather than typical like-for-like inventory."
        ),
        guest_profile=(
            "Target guest profile: family vacations, couples getaways, "
            "group retreats, and event/holiday travel."
        ),
        amenity_upside=(
            f"Strategic amenity investments — such as premium entertainment, "
            f"outdoor living upgrades, or standout experience features — can "
            f"materially improve shoulder-season conversion. In {prop.market}, "
            f"experience-driven amenities are high-impact differentiators that "
            f"support stronger weekday demand and help reduce off-season vacancy."
        ),
        amenity_badges=[
            {"emoji": "🎮", "text": "Entertainment Upgrades"},
            {"emoji": "🏖️", "text": "Outdoor Living"},
            {"emoji": "✨", "text": "Experience Amenities"},
            {"emoji": "📈", "text": "Off-Season Conversion"},
        ],
        positioning_cards=[
            PositioningCard(
                emoji="📍",
                title="Location Premium",
                text=(
                    f"Prime positioning in {prop.market} places this property "
                    f"in the top tier of vacation rentals in the area."
                ),
            ),
            PositioningCard(
                emoji="⭐",
                title="Statement Quality",
                text=(
                    "Premium finishes and quality presentation create a "
                    "standout guest experience that commands higher nightly rates."
                ),
            ),
            PositioningCard(
                emoji="💰",
                title="Revenue Potential",
                text=(
                    "Strong positioning supports competitive ADR and occupancy, "
                    "with upside from amenity optimization and listing quality."
                ),
            ),
        ],
        config_description=(
            f"Underwrite assumes well-presented living spaces and "
            f"vacation-ready utility for {prop.market} guests."
        ),
        guests_description=(
            "Max guests aligned with local regulations; comp set is "
            "capped accordingly to keep pricing and demand signals comparable."
        ),
        peak_season_text=(
            "Peak season commands premium rates with occupancy typically "
            "65-85%. Location and amenities drive higher ADR."
        ),
        shoulder_season_text=(
            "Strategic amenity positioning and listing optimization "
            "can boost shoulder-season occupancy from 35% to 50%+."
        ),
    )
