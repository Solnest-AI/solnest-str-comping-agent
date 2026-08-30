"""Derive revenue calculator slider defaults and seasonal data from comp set."""

import math
import statistics

from schema import CompProperty, RentalizerData, PropertyBasics, CalculatorDefaults


def _round_down(value: float, step: int) -> int:
    return int(math.floor(value / step) * step)


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def derive_calculator_defaults(
    comps: list[CompProperty],
    rentalizer: RentalizerData,
    prop: PropertyBasics,
) -> CalculatorDefaults:
    """Calculate slider min/max/default from comp data and Rentalizer output."""
    if not comps:
        # Fallback to Rentalizer data only
        return CalculatorDefaults(
            occ_min=30,
            occ_max=75,
            occ_default=round(rentalizer.occupancy_pct),
            adr_min=_round_down(rentalizer.adr * 0.6, 50),
            adr_max=_round_up(rentalizer.adr * 1.5, 50),
            adr_default=_round_down(rentalizer.adr, 50),
            occ_range_text=f"30-75% for premium {prop.market} properties",
            adr_range_text=f"{prop.currency}{int(rentalizer.adr * 0.6):,} - {prop.currency}{int(rentalizer.adr * 1.5):,} based on market data",
        )

    occ_values = [c.occupancy_pct for c in comps]

    # Rate basis: the calculator multiplies rate x booked nights and shows the
    # result next to comp cards whose revenue INCLUDES cleaning/guest fees.
    # Using raw ADR (fee-exclusive) understated the client's own comp cards by
    # a median 17%. Drive the slider off revenue-per-booked-night instead so
    # the slider reproduces the numbers printed above it.
    adr_values = [
        (c.annual_revenue / c.nights_booked) if c.nights_booked else c.adr
        for c in comps
    ]

    occ_min = max(20, _round_down(min(occ_values) - 10, 5))
    occ_max = min(90, _round_up(max(occ_values) + 10, 5))
    # Keep the default inside its own slider bounds, else the headline jumps
    # the instant the client touches any control.
    occ_default = int(min(occ_max, max(occ_min, round(statistics.median(occ_values)))))

    adr_min = _round_down(min(adr_values) * 0.7, 50)
    adr_max = _round_up(max(adr_values) * 1.2, 50)
    # Round to nearest, not down: flooring to the next lower $50 is a
    # one-directional understatement of up to 11.9% in low-ADR markets.
    adr_default = int(round(statistics.median(adr_values) / 50) * 50)
    adr_default = max(adr_min, min(adr_max, adr_default))

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
        adr_step=50,
        days_min=max(100, _round_down(min(c.nights_listed for c in comps), 5)),
        days_max=365,
        days_default=int(round(statistics.median([c.nights_listed for c in comps]))),
        days_step=5,
        occ_range_text=occ_range_text,
        adr_range_text=adr_range_text,
    )


# Data sources may clip peak-season occupancy at exactly 100.0.
# Floor for clipped months when ALL comps are clipped — chosen to match
# realistic STR industry peak for premium markets.
SEASONAL_PEAK_CAP = 85.0


def aggregate_seasonal_from_comps(
    comp_monthly_data: list[list[float | None]],
) -> list[float | None]:
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
            # No data at all for this month. Do NOT invent a number — an
            # 85% occupancy month fabricated into a client chart is worse
            # than no chart. Signal the gap and let the caller decide.
            out.append(None)
    return out


def airbtics_to_seasonal(airbtics_metrics: list[dict]) -> list[float | None]:
    """Convert Airbtics monthly_metrics array to 12-element calendar series.

    Airbtics metrics carry "month" keys (typically YYYY-MM strings) and
    "occupancy" values. Maps each entry to its calendar month (0-11). If multiple
    years cover the same month, the most recent value wins (insertion order).

    Returns 12 values, Jan-Dec, with None for months Airbtics did not cover.
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
    # A missing month must stay None, not 0.0. Airbtics returns a TRAILING
    # 12-month window, so at every month boundary it can briefly return 11
    # entries — and a 0% month rendered on a client seasonality chart reads as
    # broken software, not as absent data. The caller decides what to do.
    return out


def _interpolate_gaps(series: list[float | None]) -> list[float] | None:
    """Fill isolated None months from their circular neighbours.

    Returns None when more than 3 months are missing — at that point the curve
    is a guess, and the caller should try another source or block the report.
    """
    missing = [i for i, v in enumerate(series) if v is None]
    if not missing:
        return [float(v) for v in series]
    if len(missing) > 3:
        return None
    out = list(series)
    for i in missing:
        prev_v = next((out[(i - k) % 12] for k in range(1, 12)
                       if out[(i - k) % 12] is not None), None)
        next_v = next((out[(i + k) % 12] for k in range(1, 12)
                       if out[(i + k) % 12] is not None), None)
        vals = [v for v in (prev_v, next_v) if v is not None]
        out[i] = sum(vals) / len(vals) if vals else None
    return [float(v) for v in out] if all(v is not None for v in out) else None


def derive_seasonal_data(
    rentalizer: RentalizerData,
    comp_monthly_data: list[list[float | None]] | None = None,
    airbtics_metrics: list[dict] | None = None,
) -> list[float]:
    """Return 12 monthly occupancy values (Jan-Dec) from real market data.

    Priority order (most reliable first):
      1. Airbtics monthly_metrics — clean, no clipping (when market is tracked)
      2. Per-comp AirROI monthly metrics, averaged with clipped values dropped
      3. Subject's own monthly data (already in rentalizer.monthly_occupancy)

    No hardcoded template fallback — if all data sources fail, returns an empty
    list and lets the sanity gate block delivery. Reports must reflect actual
    market data, not generic seasonality assumptions.
    """
    # Priority 1: Airbtics
    if airbtics_metrics:
        airbtics_series = airbtics_to_seasonal(airbtics_metrics)
        covered = sum(1 for v in airbtics_series if v is not None)
        if covered >= 9:
            # Fill the occasional single-month gap from its neighbours rather
            # than printing a zero. With more than 3 months missing we do not
            # have a credible curve, so fall through to the next source.
            filled = _interpolate_gaps(airbtics_series)
            if filled is not None:
                return [min(v, SEASONAL_PEAK_CAP) for v in filled]

    # Priority 2: per-comp AirROI monthly metrics (drops clipped values)
    if comp_monthly_data and any(any(v is not None for v in c) for c in comp_monthly_data):
        series = aggregate_seasonal_from_comps(comp_monthly_data)
        # Too many blank months to be a credible seasonal curve — fall through
        # and ultimately let the sanity gate block rather than ship a guess.
        if sum(1 for v in series if v is None) <= 3:
            return [v if v is not None else 0.0 for v in series]

    # Priority 3: subject's own monthly data (capped, since it's only one data point)
    if rentalizer.monthly_occupancy and len(rentalizer.monthly_occupancy) == 12:
        return [min(v, SEASONAL_PEAK_CAP) for v in rentalizer.monthly_occupancy]

    # Fully exhausted — return empty so sanity gate blocks delivery.
    return []


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _contiguous_label(months: list[int]) -> str:
    """Render month indices as a compact label, wrapping across year-end."""
    if not months:
        return ""
    ms = sorted(months)
    # Detect a run that wraps December -> January (e.g. ski season)
    for start in ms:
        run = [(start + k) % 12 for k in range(len(ms))]
        if sorted(run) == ms:
            return f"{_MONTH_ABBR[run[0]]}-{_MONTH_ABBR[run[-1]]}"
    return ", ".join(_MONTH_ABBR[m] for m in ms)


def derive_season_labels(
    monthly_distribution: list[float] | None,
    seasonal_data: list[float] | None = None,
) -> tuple[str, str]:
    """Return (peak_label, shoulder_label) from REAL market data.

    Prefers AirROI's `monthly_revenue_distributions` (12 floats summing to ~1.0,
    already fetched by the estimate call and previously referenced nowhere).
    Falls back to the seasonal occupancy series.

    The season was previously hardcoded to "Dec-Mar (ski season)" in four
    places, which shipped a ski narrative on a Florida beach report whose own
    chart peaked in July.
    """
    series = None
    if monthly_distribution and len(monthly_distribution) == 12:
        series = [float(v or 0) for v in monthly_distribution]
    elif seasonal_data and len(seasonal_data) == 12:
        series = [float(v or 0) for v in seasonal_data]
    if not series or sum(series) <= 0:
        return "", ""

    ranked = sorted(range(12), key=lambda i: series[i], reverse=True)
    peak = ranked[:4]
    shoulder = [i for i in range(12) if i not in peak]
    peak_share = sum(series[i] for i in peak) / sum(series)
    return (
        f"Peak Season ({_contiguous_label(peak)}) — {peak_share:.0%} of annual revenue",
        f"Shoulder Season ({_contiguous_label(shoulder)})",
    )
