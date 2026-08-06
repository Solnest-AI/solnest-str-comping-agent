"""Central data models — every module produces or consumes these."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional


class PropertyBasics(BaseModel):
    address: str                                    # "5411 Lookout Ridge, Sun Peaks, BC V0E 5N0"
    short_address: str                              # "5411 Lookout Ridge, Sun Peaks BC"
    market: str                                     # "Sun Peaks"
    bedrooms: int
    bathrooms: float                                # supports 3.5
    max_guests: int
    property_type: str = "Property"
    hero_image_url: str = ""
    listing_url: Optional[str] = None               # external listing link (MLS, etc.)
    airbnb_url: Optional[str] = None
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


class RevenueEstimate(BaseModel):
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
    rating: float = 0.0
    review_count: int = 0
    feature_badges: list[str] = Field(default_factory=list)       # ["Ski-in/Out", "Views"]
    badge_emojis: list[str] = Field(default_factory=list)         # ["🏔️", "👁️"]
    revenue_potential: float = 0
    annual_revenue: float = 0
    occupancy_pct: float = 0
    adr: float = 0
    days_available: int = 365                        # vacant nights (calendar open, not booked)
    days_booked: int = 0                             # nights actually booked (reserved)
    airbnb_url: str = ""


class CalculatorDefaults(BaseModel):
    occ_min: int = 30
    occ_max: int = 75
    occ_default: int = 55
    occ_step: int = 1
    adr_min: int = 600
    adr_max: int = 1800
    adr_default: int = 1100
    adr_step: int = 5
    days_min: int = 100
    days_max: int = 365
    days_default: int = 365
    days_step: int = 5
    occ_range_text: str = ""
    adr_range_text: str = ""


class RevenueProjection(BaseModel):
    """Comp-derived revenue projection with three tiers."""
    conservative: float = 0                          # 25th percentile of comp revenues
    base_case: float = 0                             # median of comp revenues
    optimistic: float = 0                            # 75th percentile of comp revenues
    conservative_adr: float = 0
    base_case_adr: float = 0
    optimistic_adr: float = 0
    conservative_occ: float = 0
    base_case_occ: float = 0
    optimistic_occ: float = 0
    airroi_estimate: float = 0                       # AirROI's model estimate (footnote)
    airroi_divergence_pct: float = 0                 # % divergence from comp median
    airroi_divergence_flag: bool = False              # True if >30% divergence
    source_description: str = "Based on trailing 12-month performance of 6 comparable properties"


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


class MethodologyData(BaseModel):
    comp_criteria: list[str] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    performance_drivers: list[str] = Field(default_factory=list)


class ReportData(BaseModel):
    property: PropertyBasics
    revenue_estimate: RevenueEstimate
    projection: RevenueProjection = Field(default_factory=RevenueProjection)
    comps: list[CompProperty] = Field(default_factory=list)
    calculator: CalculatorDefaults = Field(default_factory=CalculatorDefaults)
    narratives: Narratives = Field(default_factory=Narratives)
    methodology: MethodologyData = Field(default_factory=MethodologyData)
    report_date: str = ""
    seasonal_data: list[float] = Field(default_factory=list)      # 12 monthly occupancy values
