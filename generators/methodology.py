"""Build the methodology section dynamically from property + comp data."""

from schema import PropertyBasics, CompProperty, MethodologyData


def build_methodology(
    prop: PropertyBasics,
    comps: list[CompProperty],
) -> MethodologyData:
    bed_low = prop.bedrooms - 1
    bed_high = prop.bedrooms + 1

    if comps:
        guest_low = min(c.sleeps for c in comps)
        guest_high = max(c.sleeps for c in comps)
        guest_range = f"{guest_low}-{guest_high}"
    else:
        guest_range = str(prop.max_guests)

    return MethodologyData(
        comp_criteria=[
            f"Premium {bed_low}-{bed_high} bedroom properties in {prop.market}",
            f"Sleeps {guest_range} guests (aligned with subject capacity)",
            "Active AirROI performance data (12-month trailing)",
            "Luxury tier finishes and amenities",
            "Strong review ratings (4.9+ stars)",
        ],
        data_sources=[
            "<strong>AirROI:</strong> Property-level STR revenue, ADR, and occupancy",
            "<strong>Airbtics:</strong> Market-level overlay where coverage exists",
            "<strong>Airbnb:</strong> Live listing data and guest reviews",
            f"<strong>Market Research:</strong> {prop.market} tourism trends",
            "<strong>Comp Analysis:</strong> 6-category scoring engine (physical, financial, quality, amenity, reliability, must-match)",
        ],
        assumptions=[
            "Adjustable available nights per year (100-365)",
            "Professional property management",
            "Premium photography and listing optimization",
            "Competitive dynamic pricing strategy",
            "Consistent 5-star guest experience delivery",
        ],
        performance_drivers=[
            f"<strong>Peak Season:</strong> Highest demand period for {prop.market}",
            "<strong>Shoulder Season:</strong> Lower-demand months with optimization potential",
            "<strong>Amenity Premium:</strong> Standout features that drive higher ADR",
            "<strong>Location Premium:</strong> Proximity to attractions and points of interest",
        ],
    )
