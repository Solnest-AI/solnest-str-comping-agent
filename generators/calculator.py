"""Derive revenue calculator slider defaults and seasonal data from comp set."""

import math
import statistics

from schema import CompProperty, RentalizerData, PropertyBasics, CalculatorDefaults


def _round_down(value: float, step: int) -> int:
    return int(math.floor(value / step) * step)


def _round_up(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def _inward(value: float, digits: int, up: bool) -> float:
    scaled = round(value * 10 ** digits, 6)
    return (math.ceil(scaled) if up else math.floor(scaled)) / 10 ** digits


def occupancy_band_text(low: float, high: float) -> str:
    """"61-89%" for an observed band, with both bounds rounded INWARD.

    Half-up rounding turned a real 61.4-88.6% into "61-89%", a claim wider than
    the data. Low rounds up and high rounds down; a band that holds no whole
    number falls back to one decimal, also inward.
    """
    lo, hi = _inward(low, 0, True), _inward(high, 0, False)
    if lo < hi:
        return f"{lo:.0f}-{hi:.0f}%"
    if lo == hi:
        return f"{lo:.0f}%"
    lo, hi = _inward(low, 1, True), _inward(high, 1, False)
    return f"{lo:.1f}-{hi:.1f}%" if lo < hi else f"{low:.1f}%"


# Minimum market-pool size before its median is trusted over the comp median.
# Below this a "pool" is just the comp set with extra steps.
MIN_POOL_FOR_ANCHOR = 8

# Slider granularity for the nightly rate. $50 steps quantise a ~$420 rate by
# 12%, which cost up to 5% of the headline purely to rounding and partly undid
# the point of anchoring to the subject's own numbers. Measured across the 125
# backtested listings: $50 -> median 1.70% error, 92% within 5%;
# $25 -> median 1.04%, 100% within 5%. Finer steps buy almost nothing more.
ADR_STEP = 25

# A market curve needs enough reporting months to be a baseline at all.
MIN_MARKET_MONTHS = 6
# And the subject needs enough of its own months before its measured
# performance is allowed to move that baseline up a quartile.
MIN_SUBJECT_MONTHS_FOR_STEP_UP = 2


# ── Market-level occupancy, with AirROI's no-data months removed ────

# When a market has no listings reporting for a month, AirROI does not omit
# the row and does not null it: it returns the SAME number for avg/p25/p50/
# p75/p90, almost always 0.0. Three of Sun Peaks' twelve months look like
# this. Averaging them in drags the market baseline down by a third.
def market_has_data(row: dict) -> bool:
    """False only when a row is positively identifiable as the no-data sentinel.

    The sentinel is IDENTICAL percentiles, not MISSING ones. An earlier version
    of this required all five keys to be present and rejected any partial row,
    which silently blanked every legitimately sparse market. When fewer than two
    numeric percentiles are present there is nothing to compare, so the row is
    passed through and the ordinary `is not None` handling downstream decides.
    """
    if not isinstance(row, dict):
        return False
    nums = [float(v) for v in (row.get(k) for k in ("avg", "p25", "p50", "p75", "p90"))
            if isinstance(v, (int, float))]
    if len(nums) < 2:
        return True
    return len(set(nums)) > 1


def _market_level(results: list[dict] | None, key: str) -> float | None:
    """Mean of one percentile across the months the market actually reported.

    Returned as a PERCENT, matching CompProperty.occupancy_pct. AirROI sends
    market occupancy as a 0-1 fraction and comp occupancy as a percent, and
    mixing the two silently produces a 100x error.
    """
    rows = [r for r in (results or []) if market_has_data(r)]
    if len(rows) < MIN_MARKET_MONTHS:
        return None
    vals = [float(r[key]) for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    mean = statistics.mean(vals)
    return mean * 100 if mean <= 1 else mean


def _beats_market_median(
    subject_monthly: list[float | None] | None,
    results: list[dict] | None,
) -> bool:
    """True when the subject out-ran the market median in EVERY month it ran.

    Compares like calendar months only. A young listing's summer says nothing
    about the market's winter, so months the property was dark are excluded
    rather than scored as zero.

    Every month, not on average. The report, the methodology and the template
    disclosure all print "ran above the market median every month", and an
    earlier version compared means: 90 / 90 / 5 against a market p50 of
    27 / 26 / 23 stepped the anchor up to p75 and printed that sentence.
    """
    if not subject_monthly or not results:
        return False
    mkt: dict[int, float] = {}
    for r in results or []:
        if not market_has_data(r):
            continue
        try:
            mo = int(str(r.get("date") or "").split("-")[1]) - 1
        except (ValueError, IndexError):
            continue
        if 0 <= mo <= 11 and isinstance(r.get("p50"), (int, float)):
            v = float(r["p50"])
            mkt[mo] = v * 100 if v <= 1 else v
    pairs = [(float(sv), mkt[i]) for i, sv in enumerate(subject_monthly)
             if sv is not None and i in mkt]
    if len(pairs) < MIN_SUBJECT_MONTHS_FOR_STEP_UP:
        return False
    return all(subject > market for subject, market in pairs)


def _occupancy_anchor(
    comps: list[CompProperty],
    prop: PropertyBasics,
    pool_occupancies: list[float] | None,
    market_occupancy: list[dict] | None = None,
    subject_monthly: list[float | None] | None = None,
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

    The subject branch requires is_stabilized, not has_history. A listing
    live for three months has a real result and a fake trailing year, and
    AirROI reports the fake one. When it is not stabilized the order becomes
    market p50, stepped to market p75 if the property beat the median in the
    months it actually ran. That keeps the scenario a market scenario while
    still crediting a property that has demonstrably out-performed.
    """
    sp = getattr(prop, "subject_performance", None)
    if sp is not None and sp.is_stabilized and sp.occupancy_pct > 0:
        return float(sp.occupancy_pct), "subject"

    # The subject has history but not a full year of it, so its trailing
    # occupancy is arithmetic over months it did not exist. Fall through to
    # the market, which is the honest baseline for "what does this do in a
    # normal year", and let the property's own real months move it.
    p50 = _market_level(market_occupancy, "p50")
    p75 = _market_level(market_occupancy, "p75")
    if p50 is not None:
        if p75 is not None and _beats_market_median(subject_monthly, market_occupancy):
            # It beat the market median in every month it actually operated,
            # so the market's upper quartile is the defensible starting point.
            # NOT its own raw figure: three summer months in a ski market are
            # not a year, and projecting them across one is the mirror image
            # of the bug this branch exists to fix.
            return float(p75), "market_strong"
        return float(p50), "market_typical"

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
    market_occupancy: list[dict] | None = None,
    subject_monthly: list[float | None] | None = None,
) -> CalculatorDefaults:
    """Calculate slider min/max/default from comp data and Rentalizer output.

    `pool_occupancies` is the adjusted occupancy of EVERY comparable listing
    the market query returned, not just the six that made the report. Pass it
    whenever it is available: see _occupancy_anchor for why the six selected
    comps are the wrong thing to anchor a projection to.

    `market_occupancy` is the raw /markets/metrics/occupancy rows and
    `subject_monthly` the subject's own 12-month occupancy line. Both are
    already bought for the seasonality chart; passing them here is what lets
    an unstabilized listing fall back to a market scenario instead of
    projecting a pre-launch blackout.
    """
    if not comps:
        # Fallback to Rentalizer data only
        return CalculatorDefaults(
            occ_min=30,
            occ_max=75,
            occ_default=round(rentalizer.occupancy_pct),
            adr_min=max(50, _round_down(rentalizer.adr * 0.6, 50)),
            adr_max=max(100, _round_up(rentalizer.adr * 1.5, 50)),
            adr_default=max(50, _round_down(rentalizer.adr, 50)),
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

    occ_anchor, occ_basis = _occupancy_anchor(
        comps, prop, pool_occupancies,
        market_occupancy=market_occupancy, subject_monthly=subject_monthly,
    )
    adr_anchor, adr_basis = _rate_anchor(comps, prop, adr_values)

    # Bounds must CONTAIN the anchor. Deriving them from the comps alone and
    # then clamping would silently drag a 40% market anchor back up to the
    # comp-set floor and undo the correction without saying so.
    occ_min = max(10, _round_down(min(min(occ_values), occ_anchor) - 10, 5))
    occ_max = min(95, _round_up(max(max(occ_values), occ_anchor) + 10, 5))
    occ_default = int(min(occ_max, max(occ_min, round(occ_anchor))))

    # One step is the floor: a $0 minimum fails the sanity gate and a slider
    # that starts at zero says nothing.
    adr_min = max(ADR_STEP, _round_down(min(min(adr_values), adr_anchor) * 0.7, ADR_STEP))
    adr_max = max(adr_min + ADR_STEP,
                  _round_up(max(max(adr_values), adr_anchor) * 1.2, ADR_STEP))
    # Round to nearest, not down: flooring to the next lower $50 is a
    # one-directional understatement of up to 11.9% in low-ADR markets.
    adr_default = int(round(adr_anchor / ADR_STEP) * ADR_STEP)
    adr_default = max(adr_min, min(adr_max, adr_default))

    # Nights listed: the subject's own open inventory when we have it. A host
    # who blocks half the year for personal use should not be shown a
    # 365-night projection just because the comps run year-round.
    sp = getattr(prop, "subject_performance", None)
    comp_days = [c.nights_listed for c in comps]
    if sp is not None and sp.is_stabilized and sp.nights_listed > 0:
        days_default = int(sp.nights_listed)
    else:
        # Open inventory is only the host's choice when the listing existed
        # all year. AirROI counts a pre-launch period as blocked, so the
        # cabin's "245 open nights" is 365 minus the 120 days before it went
        # live. Taking the larger of that and the comp median keeps a host who
        # genuinely blocks half the year at their own number while refusing to
        # bill a launch date as a lifestyle decision.
        days_default = int(round(statistics.median(comp_days)))
        if sp is not None and sp.nights_listed > days_default:
            days_default = int(sp.nights_listed)
    days_min = max(100, _round_down(min(min(comp_days), days_default), 5))
    days_default = max(days_min, min(365, days_default))

    # Say what the sliders' bounds ARE (an adjustable range we chose) and what
    # the comps actually DID (an observation), separately. "Industry range: 10-90%
    # for premium properties" implied both were market data and neither was: the
    # bounds are the anchor padded out, and no industry source was consulted.
    occ_range_text = (
        f"{occ_min}-{occ_max}% adjustable; the six comps observed "
        f"{occupancy_band_text(min(occ_values), max(occ_values))}"
    )
    adr_range_text = (
        f"{prop.currency}{int(min(adr_values)):,} - "
        f"{prop.currency}{int(max(adr_values)):,} per booked night across the six "
        f"comps, fees included; the nightly rate on each card excludes fees"
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
        # Step 1: the default is the property's real open-nights figure (e.g.
        # 343). A 5-night grid made the browser snap it to 345, a number
        # nobody measured, next to prose quoting 343.
        days_step=1,
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


def market_occupancy_to_seasonal(results: list[dict]) -> list[float | None]:
    """Convert AirROI /markets/metrics/occupancy rows to a 12-element calendar.

    Rows carry `date` as "YYYY-MM-01" over a TRAILING twelve months, so they
    must be mapped by calendar month, not by position. Occupancy arrives as a
    0-1 fraction and is returned as a percentage to match every other seasonal
    source. Uncovered months stay None; the caller decides.

    `p50` (median), NOT `avg`. Measured on Sun Peaks against the subject's own
    12 months: avg is off by 20.9 points, p50 by 16.9, against 16.1 for the
    six-comp average it replaces. The mean is dragged down by dead listings
    parked at 0% occupancy; the median is the typical OPERATING property, which
    is what a seasonality chart is about. p75 (17.1) and p90 (23.2) were also
    tested and are worse. Falls back to `avg` when a row carries no `p50`.
    """
    out: list[float | None] = [None] * 12
    for row in results or []:
        if not isinstance(row, dict):
            continue
        # A no-data month arrives as a real row of zeros, not as an absent one.
        # Left in, it draws a 0% shoulder season that an owner reads as "nobody
        # books here in October". Treated as the gap it is, _interpolate_gaps
        # fills it from its neighbours, which is what the gap handling below was
        # always for.
        if not market_has_data(row):
            continue
        try:
            mo = int(str(row.get("date") or "").split("-")[1]) - 1
        except (ValueError, IndexError):
            continue
        if not (0 <= mo <= 11):
            continue
        v = row.get("p50")
        if not isinstance(v, (int, float)):
            v = row.get("avg")
        if isinstance(v, (int, float)):
            out[mo] = float(v) * 100 if float(v) <= 1 else float(v)
    return out


def market_occupancy_band(results: list[dict]) -> dict[str, list[float | None]]:
    """Pull the p25/p50/p75 spread out of a market-occupancy response.

    The same paid call that gives the median also carries the percentiles, and
    throwing them away costs nothing to stop doing. Shading p25-p75 behind the
    median turns "the market does X" into "the market ranges from X to Y and
    you are here", which is the comparison an owner actually wants to see.

    Same calendar mapping rules as market_occupancy_to_seasonal: rows cover a
    trailing twelve months and map by calendar month, fractions become percent.
    """
    out: dict[str, list[float | None]] = {k: [None] * 12 for k in ("p25", "p50", "p75")}
    for row in results or []:
        if not isinstance(row, dict):
            continue
        if not market_has_data(row):
            continue
        try:
            mo = int(str(row.get("date") or "").split("-")[1]) - 1
        except (ValueError, IndexError):
            continue
        if not (0 <= mo <= 11):
            continue
        for key in ("p25", "p50", "p75"):
            v = row.get(key)
            if isinstance(v, (int, float)):
                out[key][mo] = float(v) * 100 if float(v) <= 1 else float(v)
    # Interpolate the band over the same gaps the median line fills, or the
    # shading collapses to the axis for those months while the line above it
    # runs at a sensible level.
    for key in ("p25", "p50", "p75"):
        filled = _interpolate_gaps(out[key])
        if filled is not None:
            out[key] = list(filled)
    return out


def _row_month(row: dict) -> int | None:
    """Calendar month 0-11 of a market row's `date`, or None if unreadable."""
    try:
        mo = int(str((row or {}).get("date") or "").split("-")[1]) - 1
    except (ValueError, IndexError):
        return None
    return mo if 0 <= mo <= 11 else None


def market_months_missing(results: list[dict] | None) -> int:
    """How many of the twelve calendar months the market reported nothing for.

    Counts absent months as well as the zero-row sentinel: twelve minus the
    distinct calendar months that carry real data. With no market response at
    all there is no curve to have gaps in, so that is 0.
    """
    if not results:
        return 0
    covered = {
        mo for r in results
        if market_has_data(r) and (mo := _row_month(r)) is not None
    }
    return 12 - len(covered)


def _interpolate_gaps(series: list[float | None]) -> list[float] | None:
    """Fill isolated None months by linear interpolation between real months.

    Each gap is placed between the nearest REAL months either side (wrapping
    across year-end), so a run of gaps fills as a straight line. Reading from
    the list being filled instead made the second gap of a run lean on the
    first gap's guess: anchors 10 and 50 with three gaps between filled
    30 / 40 / 45, not 20 / 30 / 40.

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
        back, prev_v = next(((k, series[(i - k) % 12]) for k in range(1, 12)
                             if series[(i - k) % 12] is not None), (None, None))
        ahead, next_v = next(((k, series[(i + k) % 12]) for k in range(1, 12)
                              if series[(i + k) % 12] is not None), (None, None))
        if prev_v is None or next_v is None:
            return None
        out[i] = prev_v + (next_v - prev_v) * back / (back + ahead)
    return [float(v) for v in out]


def derive_seasonal_data_with_basis(
    rentalizer: RentalizerData,
    comp_monthly_data: list[list[float | None]] | None = None,
    market_occupancy: list[dict] | None = None,
) -> tuple[list[float], str]:
    """Return (12 monthly occupancy values Jan-Dec, basis label).

    The basis says WHICH source supplied the curve: "market", "comps",
    "subject" or "" when nothing did. Callers use it to decide whether the
    per-comp AirROI metric calls can be skipped — only a market curve justifies
    skipping them, and the report names the source from this label.

    Priority order (most reliable first):
      1. AirROI market occupancy — whole market, with p25-p90 percentiles
      2. Per-comp AirROI monthly metrics, averaged with clipped values dropped
      3. Subject's own monthly data (already in rentalizer.monthly_occupancy)

    No hardcoded template fallback — if all data sources fail, returns an empty
    list and lets the sanity gate block delivery. Reports must reflect actual
    market data, not generic seasonality assumptions.
    """
    # Priority 1: AirROI market occupancy (one $0.10 call). AirROI is the SINGLE
    # market-data source for this report (Ryan-stated 2026-09-20): a second
    # provider meant "the market" silently meant different things on different
    # client reports, and only this call carries the p25/p75 percentiles the
    # chart shades as a band.
    #
    # Fill the occasional single-month gap from its neighbours rather than
    # printing a zero. With more than 3 months missing there is no credible
    # curve, so fall through to the next source.
    if market_occupancy:
        market_series = market_occupancy_to_seasonal(market_occupancy)
        if sum(1 for v in market_series if v is not None) >= 9:
            filled = _interpolate_gaps(market_series)
            if filled is not None:
                return [min(v, SEASONAL_PEAK_CAP) for v in filled], "market"

    # Priority 2: per-comp AirROI monthly metrics (drops clipped values).
    # A month no comp reported is a gap, not a 0% month: fill it from its
    # neighbours exactly as the market curve does above. This path used to
    # write 0.0 there and chart it, the same false-zero the market path was
    # fixed for, and it is the path every address subject runs on.
    if comp_monthly_data and any(any(v is not None for v in c) for c in comp_monthly_data):
        series = aggregate_seasonal_from_comps(comp_monthly_data)
        # Too many blank months to be a credible seasonal curve — fall through
        # and ultimately let the sanity gate block rather than ship a guess.
        filled = _interpolate_gaps(series)
        if filled is not None:
            return filled, "comps"

    # Priority 3: subject's own monthly data (capped, since it's only one data point).
    #
    # This MUST reject a series containing None. A partial series on
    # rentalizer.monthly_occupancy would be rejected by Priority 1 and then
    # resurrected here, where min(None, CAP) raises TypeError and kills the run
    # AFTER the AirROI calls have been paid for. Verified by
    # test_seasonal_sparse.py.
    own = rentalizer.monthly_occupancy
    if own and len(own) == 12 and all(v is not None for v in own):
        return [min(v, SEASONAL_PEAK_CAP) for v in own], "subject"

    # Fully exhausted — return empty so sanity gate blocks delivery.
    return [], ""



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
    Falls back to the seasonal occupancy series when that is missing or sums to
    zero. The fallback names the months only: occupancy is not revenue, so the
    "% of annual revenue" share is left off.

    The season was previously hardcoded to "Dec-Mar (ski season)" in four
    places, which shipped a ski narrative on a Florida beach report whose own
    chart peaked in July.
    """
    series = None
    share_known = False
    if monthly_distribution and len(monthly_distribution) == 12:
        dist = [float(v or 0) for v in monthly_distribution]
        if sum(dist) > 0:
            series, share_known = dist, True
    if series is None and seasonal_data and len(seasonal_data) == 12:
        occ = [float(v or 0) for v in seasonal_data]
        if sum(occ) > 0:
            series = occ
    if series is None:
        return "", ""

    ranked = sorted(range(12), key=lambda i: series[i], reverse=True)
    peak = ranked[:4]
    shoulder = [i for i in range(12) if i not in peak]
    peak_label = f"Peak Season ({_contiguous_label(peak)})"
    if share_known:
        # Only a revenue distribution can say what share of revenue the peak
        # carries. Occupancy points are not revenue: quoting a share off them
        # would invent a number that then flows into the brief.
        peak_share = sum(series[i] for i in peak) / sum(series)
        peak_label += f": {peak_share:.0%} of annual revenue"
    return peak_label, f"Shoulder Season ({_contiguous_label(shoulder)})"
