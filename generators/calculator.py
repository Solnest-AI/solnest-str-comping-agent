"""Derive revenue calculator slider defaults and seasonal data from comp set."""

import math
import statistics

from schema import CompProperty, RentalizerData, PropertyBasics, CalculatorDefaults


def _round_down(value: float, step: int) -> int:
    return int(math.floor(value / step) * step)


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


# Minimum market-pool size before its median is trusted over the comp median.
# Below this a "pool" is just the comp set with extra steps.
MIN_POOL_FOR_ANCHOR = 8

# Slider granularity for the nightly rate. $50 steps quantise a ~$420 rate by
# 12%, which cost up to 5% of the headline purely to rounding and partly undid
# the point of anchoring to the subject's own numbers. Measured across the 125
# backtested listings: $50 -> median 1.70% error, 92% within 5%;
# $25 -> median 1.04%, 100% within 5%. Finer steps buy almost nothing more.
ADR_STEP = 25


def _occupancy_anchor(
    comps: list[CompProperty],
    prop: PropertyBasics,
    pool_occupancies: list[float] | None,
) -> tuple[float, str]:
    """Pick the occupancy the headline projection is built on.

    Order matters and it is measured, not aesthetic. Backtested leave-one-out
    against 125 live listings with known trailing-12-month revenue:

        anchor            median error   bias    within +/-30%
        comp-set median          47%     +47%        31%
        market-pool median       36%      +5%        48%
        subject's own history    19%      +8%        66%

    The comp-set median is the worst of the three and it was the default.
    The six displayed comps are SELECTED for quality, so their median sits
    around the market's 81st percentile (63.8% occupancy against a market
    median of 40.8%). Anchoring a projection to them projects the top of the
    market onto an average property.
    """
    sp = getattr(prop, "subject_performance", None)
    if sp is not None and sp.has_history and sp.occupancy_pct > 0:
        return float(sp.occupancy_pct), "subject"

    if pool_occupancies:
        usable = [float(o) for o in pool_occupancies if o is not None and o > 0]
        if len(usable) >= MIN_POOL_FOR_ANCHOR:
            return statistics.median(usable), "market_pool"

    return statistics.median([c.occupancy_pct for c in comps]), "comp_set"


def _rate_anchor(
    comps: list[CompProperty],
    prop: PropertyBasics,
    adr_values: list[float],
) -> tuple[float, str]:
    """Pick the nightly rate the headline projection is built on.

    Fee-INCLUSIVE (revenue per booked night), because the calculator's output
    sits next to comp cards whose revenue includes cleaning and guest fees.
    Driving it off raw ADR understated those cards by a median 17%.

    Unlike occupancy, the rate default was already close to unbiased (+2%
    bias, 19% error) — comps are size- and market-matched, so they price
    similarly even when they out-operate the subject. The subject's own rate
    still wins when we have it.
    """
    sp = getattr(prop, "subject_performance", None)
    if sp is not None and sp.has_history:
        own = sp.revenue_per_booked_night
        if own > 0:
            return own, "subject"

    return statistics.median(adr_values), "comp_set"


def derive_calculator_defaults(
    comps: list[CompProperty],
    rentalizer: RentalizerData,
    prop: PropertyBasics,
    pool_occupancies: list[float] | None = None,
) -> CalculatorDefaults:
    """Calculate slider min/max/default from comp data and Rentalizer output.

    `pool_occupancies` is the adjusted occupancy of EVERY comparable listing
    the market query returned, not just the six that made the report. Pass it
    whenever it is available: see _occupancy_anchor for why the six selected
    comps are the wrong thing to anchor a projection to.
    """
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

    occ_anchor, occ_basis = _occupancy_anchor(comps, prop, pool_occupancies)
    adr_anchor, adr_basis = _rate_anchor(comps, prop, adr_values)

    # Bounds must CONTAIN the anchor. Deriving them from the comps alone and
    # then clamping would silently drag a 40% market anchor back up to the
    # comp-set floor and undo the correction without saying so.
    occ_min = max(10, _round_down(min(min(occ_values), occ_anchor) - 10, 5))
    occ_max = min(95, _round_up(max(max(occ_values), occ_anchor) + 10, 5))
    occ_default = int(min(occ_max, max(occ_min, round(occ_anchor))))

    adr_min = _round_down(min(min(adr_values), adr_anchor) * 0.7, ADR_STEP)
    adr_max = _round_up(max(max(adr_values), adr_anchor) * 1.2, ADR_STEP)
    # Round to nearest, not down: flooring to the next lower $50 is a
    # one-directional understatement of up to 11.9% in low-ADR markets.
    adr_default = int(round(adr_anchor / ADR_STEP) * ADR_STEP)
    adr_default = max(adr_min, min(adr_max, adr_default))

    # Nights listed: the subject's own open inventory when we have it. A host
    # who blocks half the year for personal use should not be shown a
    # 365-night projection just because the comps run year-round.
    sp = getattr(prop, "subject_performance", None)
    comp_days = [c.nights_listed for c in comps]
    if sp is not None and sp.has_history and sp.nights_listed > 0:
        days_default = int(sp.nights_listed)
    else:
        days_default = int(round(statistics.median(comp_days)))
    days_min = max(100, _round_down(min(min(comp_days), days_default), 5))
    days_default = max(days_min, min(365, days_default))

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
        adr_step=ADR_STEP,
        days_min=days_min,
        days_max=365,
        days_default=days_default,
        days_step=5,
        occ_range_text=occ_range_text,
        adr_range_text=adr_range_text,
        occ_basis=occ_basis,
        adr_basis=adr_basis,
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


def market_occupancy_to_seasonal(results: list[dict]) -> list[float | None]:
    """Convert AirROI /markets/metrics/occupancy rows to a 12-element calendar.

    Rows carry `date` as "YYYY-MM-01" over a TRAILING twelve months, so they
    must be mapped by calendar month, not by position. Occupancy arrives as a
    0-1 fraction and is returned as a percentage to match every other seasonal
    source. Uncovered months stay None; the caller decides, exactly as with
    airbtics_to_seasonal.

    `avg` is used rather than `p50`: the series it replaces was an average
    across comps, and the chart is labelled "Occupancy %" for the market.
    """
    out: list[float | None] = [None] * 12
    for row in results or []:
        if not isinstance(row, dict):
            continue
        try:
            mo = int(str(row.get("date") or "").split("-")[1]) - 1
        except (ValueError, IndexError):
            continue
        if not (0 <= mo <= 11):
            continue
        v = row.get("avg")
        if isinstance(v, (int, float)):
            out[mo] = float(v) * 100 if float(v) <= 1 else float(v)
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


def derive_seasonal_data_with_basis(
    rentalizer: RentalizerData,
    comp_monthly_data: list[list[float | None]] | None = None,
    airbtics_metrics: list[dict] | None = None,
    market_occupancy: list[dict] | None = None,
) -> tuple[list[float], str]:
    """Return (12 monthly occupancy values Jan-Dec, basis label).

    The basis says WHICH source supplied the curve: "airbtics", "comps",
    "subject" or "" when nothing did. Callers use it to decide whether the
    per-comp AirROI metric calls can be skipped — only an Airbtics-supplied
    curve justifies skipping them, and only Airbtics may be named as the source.

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
                return [min(v, SEASONAL_PEAK_CAP) for v in filled], "airbtics"

    # Priority 2: AirROI market occupancy (one $0.10 call). Whole-market curve
    # with percentiles, in place of averaging six comps that were selected for
    # quality and sit above the market — the same bias the occupancy anchor
    # removes from the headline. Same >=9 coverage bar as Airbtics.
    if market_occupancy:
        market_series = market_occupancy_to_seasonal(market_occupancy)
        if sum(1 for v in market_series if v is not None) >= 9:
            filled = _interpolate_gaps(market_series)
            if filled is not None:
                return [min(v, SEASONAL_PEAK_CAP) for v in filled], "market"

    # Priority 3: per-comp AirROI monthly metrics (drops clipped values)
    if comp_monthly_data and any(any(v is not None for v in c) for c in comp_monthly_data):
        series = aggregate_seasonal_from_comps(comp_monthly_data)
        # Too many blank months to be a credible seasonal curve — fall through
        # and ultimately let the sanity gate block rather than ship a guess.
        if sum(1 for v in series if v is None) <= 3:
            return [v if v is not None else 0.0 for v in series], "comps"

    # Priority 3: subject's own monthly data (capped, since it's only one data point).
    #
    # This MUST reject a series containing None. agent.py assigns
    # rentalizer.monthly_occupancy = airbtics_to_seasonal(metrics) when the
    # subject has no monthly data of its own, and that helper leaves uncovered
    # months as None by design. Without this guard a market Airbtics tracks for
    # fewer than 9 months would be rejected by Priority 1 and then resurrected
    # here, where min(None, CAP) raises TypeError and kills the run AFTER the
    # AirROI calls have been paid for. Verified by test_seasonal_sparse.py.
    own = rentalizer.monthly_occupancy
    if own and len(own) == 12 and all(v is not None for v in own):
        return [min(v, SEASONAL_PEAK_CAP) for v in own], "subject"

    # Fully exhausted — return empty so sanity gate blocks delivery.
    return [], ""



def derive_seasonal_data(
    rentalizer: RentalizerData,
    comp_monthly_data: list[list[float | None]] | None = None,
    airbtics_metrics: list[dict] | None = None,
    market_occupancy: list[dict] | None = None,
) -> list[float]:
    """Back-compat wrapper: the series only. Use the _with_basis form when the
    caller needs to know which source paid for the curve."""
    series, _ = derive_seasonal_data_with_basis(
        rentalizer, comp_monthly_data=comp_monthly_data,
        airbtics_metrics=airbtics_metrics, market_occupancy=market_occupancy,
    )
    return series


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
