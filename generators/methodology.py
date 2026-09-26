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
    if basis == "market_typical" and (sp is None or not sp.has_history):
        return (
            "Occupancy anchored to this market's median occupancy. This "
            "property has no Airbnb history in AirROI, so there is no trailing "
            "figure of its own to use"
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


def _seasonal_source_line(prop: PropertyBasics, seasonal_basis: str) -> str:
    """Name the source the seasonality chart was actually drawn from.

    This used to assert the market curve on every report. An address subject
    never had coordinates, so it never bought that curve and its chart was the
    six comps averaged, while this line still read "whole-market occupancy for
    Gatlinburg ... every listing in the market" (2026-09-25).
    """
    if seasonal_basis == "market":
        return (f"<strong>AirROI market curve:</strong> whole-market occupancy for "
                f"{escape(prop.market)}, median with 25th-75th percentile band, "
                f"every listing in the market, not the six shown")
    if seasonal_basis == "comps":
        return ("<strong>AirROI comp average:</strong> the seasonality chart averages "
                "the monthly occupancy of the six comparables shown, not the "
                "wider market; no occupancy data for the wider market was "
                "available for this property")
    if seasonal_basis == "subject":
        return ("<strong>AirROI subject history:</strong> the seasonality chart is "
                "this property's own monthly occupancy")
    return ("<strong>AirROI modelled seasonality:</strong> the seasonality chart "
            "is shaped from AirROI's monthly revenue distribution for this "
            "property profile, not from measured monthly occupancy")


def _operator_assumptions(calculator: "CalculatorDefaults | None") -> list[str]:
    """What the projection assumes about how the property is run.

    A subject-anchored projection IS the property's own trailing year, so it
    assumes nothing changes. It used to list professional management and
    premium photography there too, on a listing that had neither and was
    being projected exactly as it ran (Four Corners, 2026-09-25).
    """
    if getattr(calculator, "occ_basis", None) == "subject":
        return [
            "Assumes the property keeps operating as it has over the last 12 "
            "months: same management, pricing and presentation",
        ]
    return [
        "Professional property management",
        "Premium photography and listing optimization",
        "Competitive dynamic pricing strategy",
    ]


_FEATURE_WORDS = {"pool": "pool", "hot_tub": "hot tub", "sauna": "sauna",
                  "ski_in_out": "ski-in/ski-out access", "ev_charger": "EV charger"}
# The badge each feature shows as on a comp card (adapters._amenities_to_badges).
_FEATURE_BADGES = {"pool": "Pool", "hot_tub": "Hot Tub", "sauna": "Sauna",
                   "ski_in_out": "Ski-in/Out", "ev_charger": "EV Charger"}


def _lacking_lines(comps: list, lacking: list[str], relaxed: list[str]) -> list[str]:
    """Say what was done about premium features the subject does not have.

    Added after Gatlinburg: six hot-tub comps priced a house with no hot tub,
    and nothing on the report said so. Features filtered out get one line;
    features too few comps were without get a count, because then the
    projection describes properties that HAVE the feature.
    """
    word = lambda f: _FEATURE_WORDS.get(f, f.replace("_", " "))  # noqa: E731
    filtered = [f for f in lacking if f not in relaxed]
    lines = []
    if filtered:
        lines.append(f"No {' or '.join(word(f) for f in filtered)}: this property's "
                     f"listing does not mention one, so comparables with one were excluded")
    for feature in (f for f in lacking if f in relaxed):
        badge = _FEATURE_BADGES.get(feature, "")
        n = sum(1 for c in comps if badge and badge in (c.feature_badges or []))
        if n:
            lines.append(
                f"{n} of {len(comps)} comparables have a {word(feature)}, which this "
                f"property's listing does not mention. Too few comparable listings "
                f"without one were available to leave them out, so the projection "
                f"describes properties that have one"
            )
    return lines


def _comp_funnel_line(funnel: dict | None) -> str:
    """One line describing how many candidates became the delivered six."""
    if not funnel:
        return ""
    total = funnel.get("candidates") or 0
    selected = funnel.get("selected") or 0
    if not total or not selected:
        return ""
    parts: list[str] = []
    if funnel.get("subject_removed"):
        parts.append("the subject itself removed")
    if funnel.get("filtered_out"):
        n = funnel["filtered_out"]
        # Worded for both directions: a comp can be dropped for lacking a
        # feature the subject has OR for having one the subject lacks.
        parts.append(f"{n} dropped by the feature and location filters")
    if funnel.get("hard_fails"):
        n = funnel["hard_fails"]
        parts.append(f"{n} failed the activity and history gates")
    tail = f" ({'; '.join(parts)})" if parts else ""
    widened = (" A wider second search was needed to reach six, so some come "
               "from beyond the radius above." if funnel.get("widened") else "")
    return (f"{total} candidate listings considered, {selected} selected{tail}."
            f"{widened}")


def build_methodology(
    prop: PropertyBasics,
    comps: list[CompProperty],
    peak_label: str = "",
    shoulder_label: str = "",
    calculator: CalculatorDefaults | None = None,
    comp_funnel: dict | None = None,
    market_months_missing: int = 0,
    seasonal_basis: str = "",
    lacking_features: list[str] | tuple = (),
    lacking_relaxed: list[str] | tuple = (),
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

    # How the six were arrived at, not just what they had to satisfy. A reader
    # who can see 25 went in and 6 came out can judge how selective the set is;
    # criteria alone read as if six were all that existed.
    funnel_line = _comp_funnel_line(comp_funnel)
    if funnel_line:
        criteria.append(funnel_line)
    criteria.extend(_lacking_lines(comps, list(lacking_features), list(lacking_relaxed)))

    # AirROI reports a month with no market activity as a row of zeros. Those
    # points are filled from their neighbours so the chart does not draw a false
    # 0% shoulder season, which means they are modelled and must say so.
    interpolation_note = []
    if market_months_missing:
        interpolation_note.append(
            f"<strong>Interpolated months:</strong> {market_months_missing} of 12 "
            f"months had no reported market activity; those points on the "
            f"seasonality chart are filled from the adjacent months, not measured"
        )

    return MethodologyData(
        comp_criteria=criteria,
        data_sources=[
            "<strong>AirROI:</strong> Property-level STR revenue, nights booked, RevPAR and occupancy",
            _seasonal_source_line(prop, seasonal_basis),
            "<strong>Airbnb:</strong> Live listing data and guest reviews",
            "<strong>Comp Analysis:</strong> 6-category weighted comparable scoring",
            *interpolation_note,
        ],
        assumptions=[
            _occupancy_assumption(prop, calculator),
            "Adjustable listed nights per year (100-365)",
            "Revenue shown gross of platform fees, utilities, taxes, and management",
            *_operator_assumptions(calculator),
        ],
        performance_drivers=drivers,
    )
