"""Derive revenue calculator slider defaults, projections, and seasonal data from comp set."""

import math
import statistics

from schema import CompProperty, RevenueEstimate, PropertyBasics, CalculatorDefaults, RevenueProjection


def _round_down(value: float, step: int) -> int:
    return int(math.floor(value / step) * step)


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def derive_calculator_defaults(
    comps: list[CompProperty],
    revenue_estimate: RevenueEstimate,
    prop: PropertyBasics,
) -> CalculatorDefaults:
    """Calculate slider min/max/default from comp data and Revenue Estimate output."""
    if not comps:
        # Fallback to Revenue Estimate data only
        return CalculatorDefaults(
            occ_min=30,
            occ_max=75,
            occ_default=round(revenue_estimate.occupancy_pct),
            adr_min=_round_down(revenue_estimate.adr * 0.6, 50),
            adr_max=_round_up(revenue_estimate.adr * 1.5, 50),
            adr_default=_round_down(revenue_estimate.adr, 50),
            occ_range_text=f"30-75% for premium {prop.market} properties",
            adr_range_text=f"{prop.currency}{int(revenue_estimate.adr * 0.6):,} - {prop.currency}{int(revenue_estimate.adr * 1.5):,} based on market data",
        )

    occ_values = [c.occupancy_pct for c in comps]
    adr_values = [c.adr for c in comps]

    occ_min = max(20, _round_down(min(occ_values) - 10, 5))
    occ_max = min(90, _round_up(max(occ_values) + 10, 5))
    occ_default = round(statistics.median(occ_values))

    adr_min = _round_down(min(adr_values) * 0.7, 50)
    adr_max = _round_up(max(adr_values) * 1.2, 50)
    adr_default = _round_down(statistics.median(adr_values), 50)

    occ_range_text = f"{occ_min}-{occ_max}% for premium {prop.market} properties"
    adr_range_text = (
        f"{prop.currency}{int(min(adr_values)):,} - "
        f"{prop.currency}{int(max(adr_values)):,} based on market data"
    )

    return CalculatorDefaults(
        occ_min=occ_min,
        occ_max=occ_max,
        occ_default=occ_default,
        occ_step=1,
        adr_min=adr_min,
        adr_max=adr_max,
        adr_default=adr_default,
        adr_step=5,
        days_min=100,
        days_max=365,
        days_default=365,
        days_step=5,
        occ_range_text=occ_range_text,
        adr_range_text=adr_range_text,
    )


def derive_revenue_projection(
    comps: list[CompProperty],
    revenue_estimate: RevenueEstimate,
) -> RevenueProjection:
    """Derive comp-based revenue projection with three tiers.

    Uses actual TTM annual_revenue from comps (observed data, not formulas).
    Sorted low-to-high, then:
      - Conservative: average of comp[1] and comp[2] (25th percentile)
      - Base case: average of comp[2] and comp[3] (median)
      - Optimistic: average of comp[3] and comp[4] (75th percentile)

    AirROI estimate is kept as a footnote sanity check. If it diverges
    more than 30% from the comp median, a flag is raised.
    """
    if not comps:
        return RevenueProjection(
            base_case=revenue_estimate.revenue_potential,
            base_case_adr=revenue_estimate.adr,
            base_case_occ=revenue_estimate.occupancy_pct,
            airroi_estimate=revenue_estimate.revenue_potential,
            source_description="Based on AirROI market estimate (no comps available)",
        )

    # Sort comps by actual observed revenue
    sorted_by_rev = sorted(comps, key=lambda c: c.annual_revenue)
    sorted_by_adr = sorted(comps, key=lambda c: c.adr)
    sorted_by_occ = sorted(comps, key=lambda c: c.occupancy_pct)

    def _percentile(sorted_list, pct, key):
        """Get value at a percentile from a sorted list."""
        vals = [key(c) for c in sorted_list]
        idx = pct / 100 * (len(vals) - 1)
        low = int(idx)
        high = min(low + 1, len(vals) - 1)
        frac = idx - low
        return vals[low] + frac * (vals[high] - vals[low])

    conservative_rev = round(_percentile(sorted_by_rev, 25, lambda c: c.annual_revenue))
    base_case_rev = round(_percentile(sorted_by_rev, 50, lambda c: c.annual_revenue))
    optimistic_rev = round(_percentile(sorted_by_rev, 75, lambda c: c.annual_revenue))

    conservative_adr = round(_percentile(sorted_by_adr, 25, lambda c: c.adr))
    base_case_adr = round(_percentile(sorted_by_adr, 50, lambda c: c.adr))
    optimistic_adr = round(_percentile(sorted_by_adr, 75, lambda c: c.adr))

    conservative_occ = round(_percentile(sorted_by_occ, 25, lambda c: c.occupancy_pct))
    base_case_occ = round(_percentile(sorted_by_occ, 50, lambda c: c.occupancy_pct))
    optimistic_occ = round(_percentile(sorted_by_occ, 75, lambda c: c.occupancy_pct))

    # AirROI sanity check
    airroi_est = revenue_estimate.adr * 365 * (revenue_estimate.occupancy_pct / 100)
    divergence_pct = 0.0
    if base_case_rev > 0:
        divergence_pct = round(abs(airroi_est - base_case_rev) / base_case_rev * 100, 1)

    return RevenueProjection(
        conservative=conservative_rev,
        base_case=base_case_rev,
        optimistic=optimistic_rev,
        conservative_adr=conservative_adr,
        base_case_adr=base_case_adr,
        optimistic_adr=optimistic_adr,
        conservative_occ=conservative_occ,
        base_case_occ=base_case_occ,
        optimistic_occ=optimistic_occ,
        airroi_estimate=round(airroi_est),
        airroi_divergence_pct=divergence_pct,
        airroi_divergence_flag=divergence_pct > 30,
    )


def align_calculator_to_base_case(
    calc: CalculatorDefaults,
    base_case: float,
) -> CalculatorDefaults:
    """Move the calculator's starting ADR so its default state reproduces the
    headline Base Case revenue.

    The headline Base Case is the median of comps' actual TTM revenue, while the
    calculator default is ``occ_default × adr_default × days_default``. Those live
    on different axes — median(revenue) ≠ median(occ) × median(ADR) — so the
    calculator's opening number can disagree badly with the headline (e.g. a
    CA$47k calculator default under a CA$77k Base Case). This back-solves the
    default ADR from the Base Case at the default occupancy and day-count so the
    two agree on load. Only the starting ADR moves; the sliders stay fully
    interactive. Returns the same (mutated) CalculatorDefaults for convenience.
    """
    step = calc.adr_step or 5
    occ_frac = calc.occ_default / 100
    denom = occ_frac * calc.days_default
    if base_case <= 0 or denom <= 0:
        return calc

    aligned_adr = int(round(base_case / denom / step) * step)
    calc.adr_default = aligned_adr

    # Keep the ADR slider able to reach its own (possibly repositioned) default.
    if calc.adr_min > aligned_adr:
        calc.adr_min = _round_down(aligned_adr * 0.7, 50)
    if calc.adr_max < aligned_adr:
        calc.adr_max = _round_up(aligned_adr * 1.15, 50)
    return calc


# Data sources may clip peak-season occupancy at exactly 100.0.
# Floor for clipped months when ALL comps are clipped — chosen to match
# realistic STR industry peak for premium markets.
SEASONAL_PEAK_CAP = 85.0


def aggregate_seasonal_from_comps(
    comp_monthly_data: list[list[float | None]],
) -> list[float]:
    """Aggregate per-comp monthly occupancy into a single 12-month series.

    Strategy per calendar month:
      1. Filter out clipped values (exactly 100.0) — these may be data-source caps, not real data.
      2. Average the remaining unclipped values.
      3. If ALL comps are clipped for that month, return SEASONAL_PEAK_CAP (85)
         as the realistic industry peak.

    Args:
      comp_monthly_data: list of 12-element lists; each inner list is one comp's
        monthly occupancy (None for months with no data).

    Returns:
      12 floats, Jan-Dec.
    """
    out: list[float] = []
    for month_idx in range(12):
        # Collect this month's values across all comps
        month_vals = [
            comp[month_idx]
            for comp in comp_monthly_data
            if month_idx < len(comp) and comp[month_idx] is not None
        ]
        # Separate clipped from unclipped
        unclipped = [v for v in month_vals if v != 100.0]
        if unclipped:
            # Real data wins — average the unclipped values
            out.append(round(sum(unclipped) / len(unclipped), 1))
        elif month_vals:
            # All values for this month were clipped at 100 → use realistic cap
            out.append(SEASONAL_PEAK_CAP)
        else:
            # No data at all for this month — should be rare; cap as safe default
            out.append(SEASONAL_PEAK_CAP)
    return out


def airbtics_to_seasonal(airbtics_metrics: list[dict]) -> list[float]:
    """Convert Airbtics monthly_metrics array to 12-element calendar series.

    Airbtics metrics carry "month" keys (typically YYYY-MM strings) and
    "occupancy" values. Maps each entry to its calendar month (0-11). If multiple
    years cover the same month, the most recent value wins (insertion order).

    Returns 12 floats, Jan-Dec, with 0.0 for months with no Airbtics data.
    """
    out: list[float | None] = [None] * 12
    for entry in airbtics_metrics or []:
        if not isinstance(entry, dict):
            continue
        month_key = entry.get("month") or ""
        try:
            mo = int(str(month_key).split("-")[1]) - 1
            if not (0 <= mo <= 11):
                continue
        except (ValueError, IndexError):
            continue
        occ = entry.get("occupancy")
        if isinstance(occ, (int, float)):
            out[mo] = float(occ)
    return [v if v is not None else 0.0 for v in out]


def derive_seasonal_data(
    revenue_estimate: RevenueEstimate,
    comp_monthly_data: list[list[float | None]] | None = None,
    airbtics_metrics: list[dict] | None = None,
) -> list[float]:
    """Return 12 monthly occupancy values (Jan-Dec) from real market data.

    Priority order (most reliable first):
      1. Airbtics monthly_metrics — clean, no clipping (when market is tracked)
      2. Per-comp AirROI monthly metrics, averaged with clipped values dropped
      3. Subject's own monthly data (already in revenue_estimate.monthly_occupancy)

    No hardcoded template fallback — if all data sources fail, returns an empty
    list and lets the sanity gate block delivery. Reports must reflect actual
    market data, not generic seasonality assumptions.
    """
    # Priority 1: Airbtics
    if airbtics_metrics:
        airbtics_series = airbtics_to_seasonal(airbtics_metrics)
        # Only use if we have real values for at least 9/12 months
        non_zero = sum(1 for v in airbtics_series if v > 0)
        if non_zero >= 9:
            return [min(v, SEASONAL_PEAK_CAP) if v > SEASONAL_PEAK_CAP else v
                    for v in airbtics_series]

    # Priority 2: per-comp AirROI monthly metrics (drops clipped values)
    if comp_monthly_data and any(any(v is not None for v in c) for c in comp_monthly_data):
        return aggregate_seasonal_from_comps(comp_monthly_data)

    # Priority 3: subject's own monthly data (capped, since it's only one data point)
    if revenue_estimate.monthly_occupancy and len(revenue_estimate.monthly_occupancy) == 12:
        return [min(v, SEASONAL_PEAK_CAP) for v in revenue_estimate.monthly_occupancy]

    # Fully exhausted — return empty so sanity gate blocks delivery.
    return []
