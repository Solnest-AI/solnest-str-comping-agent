"""Build the methodology section dynamically from property + comp data."""

from markupsafe import escape
from schema import (
    PropertyBasics, CompProperty, MethodologyData, CalculatorDefaults,
)


def _bed_tolerance(bedrooms: int) -> int:
    """Mirror comp_scorer's bedroom gate so the stated criteria match reality."""
    if bedrooms <= 4:
        return 1
    if bedrooms <= 7:
        return 2
    return 3


def _occupancy_assumption(
    prop: PropertyBasics,
    calculator: "CalculatorDefaults | None",
) -> str:
    """State which occupancy the projection was built on.

    The three bases are not equally strong and the report should not present
    them as if they were. Anchoring to the six displayed comps over-projected
    by +72% (median) across 125 backtested listings, because those six are
    selected for quality and sit near the market's 81st percentile.
    """
    basis = getattr(calculator, "occ_basis", None) if calculator else None
    sp = getattr(prop, "subject_performance", None)

    if basis == "subject" and sp is not None:
        return (
            f"Occupancy anchored to this property's own trailing 12 months "
            f"({sp.occupancy_pct:.0f}% of {sp.nights_listed} open nights), "
            f"not inferred from the comparables"
        )
    if basis == "market_strong":
        months = getattr(sp, "months_with_data", None) if sp is not None else None
        covered = f"the {months} months" if months else "the months"
        return (
            f"Occupancy anchored to this market's UPPER-QUARTILE occupancy. The "
            f"subject has not been listed a full year, so its trailing-12-month "
            f"figure covers a period it was not on the market; across {covered} "
            f"it has operated it ran above the market median every month"
        )
    if basis == "market_typical":
        return (
            "Occupancy anchored to this market's median occupancy. The subject "
            "has not been listed a full year, so its trailing-12-month figure "
            "covers a period it was not on the market and is not used here"
        )
    if basis == "market_pool":
        return (
            "Occupancy anchored to the median of every comparable listing in "
            "this market, not to the six shown (those are selected for "
            "quality and run above the market median)"
        )
    return (
        "Occupancy anchored to the median of the six comparables shown - a "
        "well-run-operator figure rather than a market average"
    )


def build_methodology(
    prop: PropertyBasics,
    comps: list[CompProperty],
    peak_label: str = "",
    shoulder_label: str = "",
    calculator: CalculatorDefaults | None = None,
) -> MethodologyData:
    tol = _bed_tolerance(prop.bedrooms)
    bed_low = max(0, prop.bedrooms - tol)
    bed_high = prop.bedrooms + tol
    bed_range = "studio" if bed_high == 0 else (
        f"{bed_low}-{bed_high} bedroom" if bed_low != bed_high else f"{bed_low} bedroom"
    )

    criteria = [f"{bed_range.capitalize()} properties in {prop.market}"]

    if comps:
        guest_low = min(c.sleeps for c in comps)
        guest_high = max(c.sleeps for c in comps)
        criteria.append(f"Sleeps {guest_low}-{guest_high} guests (aligned with subject capacity)")

        # State the rating floor we ACTUALLY delivered, not an aspiration.
        rated = [c.rating for c in comps if c.rating]
        if rated:
            criteria.append(f"Review ratings {min(rated):.2f}+ stars")

        booked = [c.nights_booked for c in comps if c.nights_booked]
        if booked:
            criteria.append(
                f"Active operators only ({min(booked)}+ nights booked in the trailing 12 months)"
            )

        dists = [c.distance_km for c in comps if c.distance_km is not None]
        if dists:
            criteria.append(f"Within {max(dists):.1f} km of the subject property")

        n_pm = sum(1 for c in comps if c.professional_management)
        if n_pm:
            criteria.append(f"{n_pm} of {len(comps)} professionally managed")

        n_rescued = sum(1 for c in comps if c.rescued)
        if n_rescued:
            criteria.append(
                f"Note: {n_rescued} comp(s) admitted on relaxed criteria due to a thin local pool"
            )
    else:
        criteria.append(f"Sleeps {prop.max_guests} guests")

    criteria.append("Active AirROI performance data (12-month trailing)")

    drivers = []
    if peak_label:
        drivers.append(f"<strong>Peak Season:</strong> {escape(peak_label)}")
    if shoulder_label:
        drivers.append(f"<strong>Shoulder Season:</strong> {escape(shoulder_label)}")
    drivers += [
        "<strong>Amenity Premium:</strong> Differentiating features vs the comp set",
        "<strong>Location Premium:</strong> Proximity and setting",
    ]

    return MethodologyData(
        comp_criteria=criteria,
        data_sources=[
            "<strong>AirROI:</strong> Property-level STR revenue, ADR, and occupancy",
            (f"<strong>AirROI market curve:</strong> whole-market occupancy for "
             f"{escape(prop.market)}, median with 25th-75th percentile band — "
             f"every listing in the market, not the six shown"),
            "<strong>Airbnb:</strong> Live listing data and guest reviews",
            "<strong>Comp Analysis:</strong> 6-category weighted comparable scoring",
        ],
        assumptions=[
            _occupancy_assumption(prop, calculator),
            "Adjustable listed nights per year (100-365)",
            "Revenue shown gross of platform fees, utilities, taxes, and management",
            "Professional property management",
            "Premium photography and listing optimization",
            "Competitive dynamic pricing strategy",
        ],
        performance_drivers=drivers,
    )
