"""Central data models — every module produces or consumes these."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


# A trailing-twelve-month figure is only a year's worth of evidence when the
# listing was live for most of that year. See SubjectPerformance.is_stabilized.
MIN_MONTHS_FOR_STABILIZED = 9
MAX_L90D_SHARE_FOR_STABILIZED = 0.75


class SubjectPerformance(BaseModel):
    """The subject property's OWN trailing-12-month numbers.

    AirROI returns a full `performance_metrics` block on `get_listing()` for
    any already-listed property. That block used to be fetched and dropped on
    the floor: the report then inferred the subject's occupancy from the comp
    set even when we were holding its real, measured occupancy. On the
    Gatlinburg fixture that produced a report telling a property running 84.3%
    that it was at 50% and should improve toward the comp median it was
    already 14 points above.

    Present only when the subject is a live Airbnb listing with history.
    A property not yet on the market has no `subject_performance` and the
    report falls back to market inference, which is the correct behaviour
    for a pre-purchase analysis.
    """
    annual_revenue: float = 0                # ttm_revenue, fee-INCLUSIVE
    occupancy_pct: float = 0                 # adjusted: booked / open nights
    occupancy_raw_pct: Optional[float] = None  # booked / 365
    adr: float = 0                           # room rate, fees EXCLUDED
    nights_booked: int = 0
    nights_listed: int = 0                   # open inventory (total - blocked)
    revpar: Optional[float] = None
    l90d_occupancy_pct: Optional[float] = None
    l90d_nights_booked: Optional[int] = None
    # How many of the trailing 12 months the listing actually reported data
    # for. Filled from /listings/metrics/all when we buy it; None when we
    # did not. See is_stabilized for why this matters more than it sounds.
    months_with_data: Optional[int] = None

    @property
    def has_history(self) -> bool:
        """True when the listing has ANY measured result to report.

        Deliberately a low bar: it gates whether we *display* the property's
        trailing numbers, and a three-month-old listing's real bookings are
        still real. Use is_stabilized to decide whether to *project* from
        them.
        """
        return self.nights_booked > 0 and self.annual_revenue > 0

    @property
    def is_stabilized(self) -> bool:
        """True when the trailing twelve months describe a full operating year.

        AirROI computes ttm_occupancy over the whole 365-day window whether or
        not the listing existed for it, counting the pre-launch period as
        blocked inventory. A listing that went live three months ago therefore
        reports a trailing occupancy of roughly a quarter of its real pace, and
        anchoring a projection to that number tells a healthy property it is
        failing.

        Measured on "Sleeps 12 Log Cabin" (Sun Peaks, live since June 2026):
        AirROI reported 20% trailing occupancy over 245 "open" nights. Its
        three actual months ran 53.3%, 38.7% and 61.3%, against a market p50 of
        25.3% and a p75 of 43.7% in the same months. The report called a
        top-quartile performer a 20%-occupancy dog.

        Two detectors, strongest first:

        * Month count, when we bought /listings/metrics/all. Unambiguous.
        * Otherwise the share of the year's bookings that landed in the last
          90 days. A stabilized listing cannot put most of its year in one
          quarter; the cabin put 47 of 49 nights (96%) there. The threshold is
          deliberately loose because a genuinely seasonal ski property polled
          in March can legitimately run high.
        """
        if not self.has_history:
            return False
        if self.months_with_data is not None:
            return self.months_with_data >= MIN_MONTHS_FOR_STABILIZED
        if self.l90d_nights_booked is not None and self.nights_booked > 0:
            share = float(self.l90d_nights_booked) / float(self.nights_booked)
            return share <= MAX_L90D_SHARE_FOR_STABILIZED
        # No signal either way. Treat as stabilized: the old behaviour, and
        # refusing to project from a listing we cannot fault is worse than
        # occasionally projecting from a young one.
        return True

    @property
    def revenue_per_booked_night(self) -> float:
        """Fee-INCLUSIVE rate basis, matching how comp cards report revenue."""
        if self.nights_booked:
            return self.annual_revenue / self.nights_booked
        return self.adr


class PropertyBasics(BaseModel):
    address: str                                    # "5411 Lookout Ridge, Sun Peaks, BC V0E 5N0"
    short_address: str                              # "5411 Lookout Ridge, Sun Peaks BC"
    market: str                                     # "Sun Peaks"
    bedrooms: int
    bathrooms: float                                # supports 3.5
    max_guests: int
    # Never a marketing adjective. The old default was "Luxury Chalet", and
    # comp_scorer reads the subject's type into its luxury signal, so every
    # subject that arrived without a type was scored as a luxury listing and
    # comps priced under 55% of the model ADR were hard-failed.
    property_type: str = "Property"
    hero_image_url: str = ""
    listing_url: Optional[str] = None               # external listing link (MLS, etc.)
    airbnb_url: Optional[str] = None
    airroi_listing_id: Optional[int] = None         # set when resolved via AirROI /listings
    currency: str = "$"
    title: Optional[str] = None                     # Airbnb listing title
    rating: Optional[float] = None
    review_count: Optional[int] = None
    sqft: Optional[int] = None
    is_superhost: bool = False
    amenities: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country_code: Optional[str] = None    # ISO-2 from AirROI location_info
    # The subject's own trailing-12-month performance, when it is already
    # listed. None for a pre-purchase / not-yet-listed property.
    subject_performance: Optional[SubjectPerformance] = None


class RentalizerData(BaseModel):
    revenue_potential: float                        # e.g. 142300.0
    adr: float                                      # average daily rate
    occupancy_pct: float                            # 0-100
    monthly_occupancy: list[float] = Field(default_factory=list)  # 12 values Jan-Dec
    monthly_revenue: list[float] = Field(default_factory=list)    # 12 values


class CompProperty(BaseModel):
    name: str
    image_url: str = ""
    sleeps: int
    bedrooms: int
    bathrooms: float
    rating: Optional[float] = None                                # None = too few reviews to rate
    review_count: int = 0
    feature_badges: list[str] = Field(default_factory=list)       # ["Ski-in/Out", "Views"]
    badge_emojis: list[str] = Field(default_factory=list)         # ["🏔️", "👁️"]
    # Still computed, and still used by comp_scorer's efficiency signal, but
    # NOT printed on a comp card any more. On the Sun Peaks set 4 of 6
    # resolved to the comp's own annual_revenue x 1.02: a comp at or above
    # the pool's p75 gets its own occupancy as its ceiling, and the floor
    # rounds the rest up. Publishing that as "potential" presented
    # arithmetic as a finding.
    revenue_potential: float = 0                                  # fee-inclusive ceiling
    annual_revenue: float = 0                                     # fee-inclusive (ttm_revenue)
    occupancy_pct: float = 0                                      # ADJUSTED: booked / open nights
    adr: float = 0                                                # room rate, fees EXCLUDED
    # Night accounting. nights_booked + unsold == nights_listed (approx).
    # There is deliberately no field called "days_available": AirROI's
    # ttm_available_days means UNSOLD nights and reading it as availability
    # inverted this entire tool. See adapters/airroi_to_comp.py.
    nights_booked: int = 0                                        # ttm_days_reserved
    nights_listed: int = 365                                      # total - blocked (open inventory)
    revpar: float = 0
    l90d_occupancy_pct: Optional[float] = None                    # freshness
    l90d_nights_booked: Optional[int] = None
    superhost: bool = False
    professional_management: bool = False
    guest_favorite: bool = False
    cleaning_fee: Optional[float] = None
    min_nights: Optional[int] = None
    distance_km: Optional[float] = None                           # from subject
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    rescued: bool = False                                         # admitted via rescue pass
    airbnb_url: str = ""

    @property
    def revenue_per_listed_night(self) -> float:
        """Fee-inclusive revenue per night the listing was OPEN for booking.

        The only way to compare listings whose open inventory differs. MORRISEY
        earns CA$793 per listed night across 187 nights; West Pine earns CA$462
        across 365. They finish the year within 12% of each other and the card
        could not show why without this.

        Computed here rather than read from AirROI's `ttm_revpar` /
        `ttm_adjusted_revpar`, which reconcile to neither revenue/365 nor
        revenue/listed: measured ratios ran 0.80-0.97 across the Sun Peaks pool,
        so their definition is not one this code can state. Both inputs below
        are fields this codebase pins down exactly.
        """
        if not self.nights_listed:
            return 0.0
        return self.annual_revenue / self.nights_listed


class CalculatorDefaults(BaseModel):
    occ_min: int = 30
    occ_max: int = 75
    occ_default: int = 55
    occ_step: int = 1
    adr_min: int = 600
    adr_max: int = 1800
    adr_default: int = 1100
    adr_step: int = 50
    days_min: int = 100
    days_max: int = 365
    days_default: int = 365
    days_step: int = 5
    occ_range_text: str = ""
    adr_range_text: str = ""
    # Where each default came from, so the report can disclose its basis
    # instead of presenting every number as if it were derived the same way.
    # "subject" = the property's own trailing 12 months (strongest, and only
    #   used when that year is stabilized: see SubjectPerformance.is_stabilized),
    # "market_typical" = market median, for a listing too young to project from,
    # "market_strong" = market upper quartile, same case but the property beat
    #   the market median in every month it has actually operated,
    # "market_pool" = median of every comparable listing AirROI returned,
    # "comp_set" = median of the six displayed comps (weakest: those six are
    # selected for quality, so they sit well above the market median).
    occ_basis: str = "comp_set"
    adr_basis: str = "comp_set"


class PositioningCard(BaseModel):
    emoji: str
    title: str
    text: str


class Narratives(BaseModel):
    positioning_summary: str = ""
    guest_profile: str = ""
    amenity_upside: str = ""
    amenity_badges: list[dict] = Field(default_factory=list)      # [{"emoji": "🎮", "text": "..."}]
    positioning_cards: list[PositioningCard] = Field(default_factory=list)
    config_description: str = ""
    guests_description: str = ""
    peak_season_text: str = ""
    shoulder_season_text: str = ""
    peak_season_label: str = ""        # derived from monthly revenue distribution
    shoulder_season_label: str = ""


class MethodologyData(BaseModel):
    comp_criteria: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    performance_drivers: list[str] = Field(default_factory=list)


class ReportData(BaseModel):
    property: PropertyBasics
    rentalizer: RentalizerData
    comps: list[CompProperty] = Field(default_factory=list)
    calculator: CalculatorDefaults = Field(default_factory=CalculatorDefaults)
    narratives: Narratives = Field(default_factory=Narratives)
    methodology: MethodologyData = Field(default_factory=MethodologyData)
    report_date: str = ""
    seasonal_data: list[float] = Field(default_factory=list)      # 12 monthly occupancy values

    # The market spread behind seasonal_data, so the chart can shade a band
    # rather than assert one line. Both are 12 values, Jan-Dec, percent.
    # Empty when the curve did not come from AirROI market percentiles.
    seasonal_p25: list[float] = Field(default_factory=list)
    seasonal_p75: list[float] = Field(default_factory=list)

    # THIS property's own monthly occupancy, overlaid on the band so an owner
    # sees where they sit inside their market. Empty for a property with no
    # track record — nothing is invented to fill the line. Entries may be None
    # for months the listing was not open at all.
    subject_monthly: list[float | None] = Field(default_factory=list)
