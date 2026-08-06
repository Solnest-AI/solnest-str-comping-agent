"""
AirROI → internal schema adapter.

AirROI returns listings as nested objects (listing_info, property_details,
performance_metrics, etc.). This module flattens them into the shape
comp_scorer.py expects, then converts scored dicts into CompProperty models.

Two conversion stages:
  1. map_for_scorer(airroi_listing)  -> dict         (input to comp_scorer.rank_comps)
  2. to_comp_property(scored_comp)   -> CompProperty  (for ReportData/Jinja)

map_for_scorer() preserves all original nested AirROI fields on the output dict
so to_comp_property() can still read listing_info, location_info, etc. after scoring.
"""

from __future__ import annotations

from typing import Optional

from schema import CompProperty, PropertyBasics


# ── Amenity mapping: AirROI key → (scorer keyword, display label, emoji) ──

# AirROI returns amenities as a LIST of strings.
# We map them to: scorer keyword, display label, emoji.
_AMENITY_MAP: dict[str, tuple[str, str, str]] = {
    "hot_tub":                    ("hot tub",       "Hot Tub",        "🛁"),
    "pool":                       ("pool",          "Pool",           "🏊"),
    "ski_in_ski_out":             ("ski-in/out",    "Ski-in/Out",     "🎿"),
    "sauna":                      ("sauna",         "Sauna",          "🧖"),
    "indoor_fireplace":           ("fireplace",     "Fireplace",      "🔥"),
    "fire_pit":                   ("fire pit",      "Fire Pit",       "🔥"),
    "pets_allowed":               ("pet friendly",  "Pet-Friendly",   "🐕"),
    "free_parking_on_premises":   ("parking",       "Parking",        "🚗"),
    "ev_charger":                 ("ev charger",    "EV Charger",     "⚡"),
    "gym":                        ("gym",           "Gym",            "💪"),
    "exercise_equipment":         ("gym",           "Gym",            "💪"),
    "game_console":               ("game room",     "Games Room",     "🎮"),
    "bbq_grill":                  ("bbq",           "BBQ",            "🍖"),
    "waterfront":                 ("waterfront",    "Waterfront",     "🌊"),
    "lake_access":                ("lake access",   "Lake Access",    "🌊"),
    "beach_access":               ("beach access",  "Beach Access",   "🏖️"),
    "patio_or_balcony":           ("patio",         "Patio/Balcony",  "🌿"),
    "resort_access":              ("resort",        "Resort Access",  "🏨"),
}

# Priority order for badge slots — show the most revenue-relevant amenities first.
# Universal amenities (wifi, kitchen, washer, dryer) are excluded.
_BADGE_PRIORITY = [
    "ski_in_ski_out",
    "hot_tub", "pool", "sauna",
    "indoor_fireplace", "beach_access", "lake_access", "waterfront",
    "fire_pit", "gym", "exercise_equipment",
    "game_console",
    "pets_allowed", "ev_charger",
    "bbq_grill",
]

# Text-signal badges: AirROI's amenity list covers many keys, but some
# high-value signals (views, village proximity, luxury tier) only appear
# in listing names/descriptions.
_TEXT_SIGNAL_PATTERNS: list[tuple[tuple[str, ...], str, str]] = [
    (("ski-in", "ski in", "ski out", "ski/out", "ski access"),
     "Ski-in/Out", "🎿"),
    (("sauna", "cold plunge"),
     "Sauna", "🧖"),
    (("fireplace", "fire place", "wood stove", "gas fireplace"),
     "Fireplace", "🔥"),
    (("mountain view", "valley view", "panoramic view", "scenic view", "lake view", "ocean view"),
     "Views", "🏔️"),
    (("village", "village core", "walk to village", "steps from village", "downtown"),
     "Village", "📍"),
    (("cold plunge", "wellness", "spa", "steam room"),
     "Wellness", "🧘"),
    (("luxury", "luxurious", "premium", "executive", "boutique"),
     "Luxury", "✨"),
]

# Amenities to skip when building badges — universal / low-value
_SKIP_AMENITIES = {
    "wifi", "kitchen", "washer", "dryer", "heating", "air_conditioning",
    "dedicated_workspace", "essentials", "hot_water", "carbon_monoxide_alarm",
    "smoke_alarm", "fire_extinguisher", "first_aid_kit", "hangers",
    "iron", "hair_dryer", "shampoo", "conditioner", "body_soap",
    "bed_linens", "extra_pillows_and_blankets", "clothing_storage",
    "dishes_and_silverware", "cooking_basics", "coffee_maker", "coffee",
    "refrigerator", "microwave", "stove", "oven", "dishwasher", "freezer",
    "cleaning_products", "long_term_stays_allowed",
}


def _flatten_amenities(raw: Optional[list]) -> list[str]:
    """Convert AirROI's amenity list to lowercase keyword strings for the scorer."""
    if not raw or not isinstance(raw, list):
        return []
    words: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        # Normalize: replace underscores with spaces for keyword matching
        keyword = item.replace("_", " ").lower()
        if keyword not in seen:
            seen.add(keyword)
            words.append(keyword)
        # Also include the canonical scorer keyword from the map
        entry = _AMENITY_MAP.get(item)
        if entry and entry[0] not in seen:
            seen.add(entry[0])
            words.append(entry[0])
    return words


def _amenities_to_badges(
    amenity_list: Optional[list],
    limit: int = 3,
    text_context: str = "",
) -> tuple[list[str], list[str]]:
    """Pick the top-N most relevant amenity badges for the comp card.

    Sources (in priority order):
      1. Text signals in `text_context` (name + description)
      2. AirROI amenity list via `_BADGE_PRIORITY`
      3. Any remaining amenities as Title Case fallback

    Returns (feature_labels, emojis) — parallel lists, up to `limit` items each.
    """
    labels: list[str] = []
    emojis: list[str] = []
    seen: set[str] = set()

    def _add(label: str, emoji: str):
        if label and label not in seen and len(labels) < limit:
            seen.add(label)
            labels.append(label)
            emojis.append(emoji)

    # Pass 1: text signals from name/description
    text_lower = (text_context or "").lower()
    for keywords, label, emoji in _TEXT_SIGNAL_PATTERNS:
        if any(k in text_lower for k in keywords):
            _add(label, emoji)

    amenities_set = set(amenity_list) if isinstance(amenity_list, list) else set()

    # Pass 2: prioritized amenities from the AirROI list
    for key in _BADGE_PRIORITY:
        if key not in amenities_set:
            continue
        entry = _AMENITY_MAP.get(key)
        if not entry:
            continue
        _, label, emoji = entry
        _add(label, emoji)

    # Pass 3: any remaining amenities, unmapped keys surface as Title Case
    for key in (amenity_list or []):
        if len(labels) >= limit:
            break
        if not isinstance(key, str):
            continue
        if key in _SKIP_AMENITIES or key in _AMENITY_MAP or key in _BADGE_PRIORITY:
            continue
        label = key.replace("_", " ").title()
        _add(label, "✨")

    return labels, emojis


def _coerce_occ_pct(val) -> Optional[float]:
    """AirROI returns occupancy as 0-1. Normalize to 0-100 percent."""
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if v <= 1.0:
        return round(v * 100, 2)
    return round(v, 2)


def _derive_revenue_potential(
    annual_revenue: Optional[float],
    adr: Optional[float],
    days_available: int,
    occupancy: Optional[float] = None,
) -> Optional[float]:
    """Revenue potential: what this property would earn at 70% occupancy.

    Formula: (annual_revenue / actual_occupancy) × 0.70

    This uses real revenue (which captures seasonal pricing) divided by
    actual occupancy to get the "fully booked" rate, then applies a
    realistic 70% occupancy target. Always higher than actual revenue.
    """
    if annual_revenue and annual_revenue > 0 and occupancy and occupancy > 0:
        # occupancy may be 0-1 or 0-100 — normalize to 0-1
        occ = occupancy if occupancy <= 1.0 else occupancy / 100
        if occ > 0:
            return round((annual_revenue / occ) * 0.70, 2)

    # Fallback: ADR × 365 × 0.70
    if adr and adr > 0:
        return round(float(adr) * 365 * 0.70, 2)

    if annual_revenue and annual_revenue > 0:
        return round(float(annual_revenue) * 1.3, 2)

    return None


# ── Public: Stage 1 — AirROI listing → scorer input dict ────────────

def map_for_scorer(airroi_listing: dict) -> dict:
    """Convert a nested AirROI listing dict into the flat shape
    comp_scorer.score_comp reads. Preserves all original nested fields
    so to_comp_property() can still access them after scoring.
    """
    if not isinstance(airroi_listing, dict):
        raise TypeError(f"map_for_scorer expected dict, got {type(airroi_listing).__name__}")

    # Start from a shallow copy so we don't mutate the caller's dict
    m = dict(airroi_listing)

    # Extract nested sections
    li = airroi_listing.get("listing_info") or {}
    pd = airroi_listing.get("property_details") or {}
    pm = airroi_listing.get("performance_metrics") or {}
    ratings = airroi_listing.get("ratings") or {}

    # ── Identity / physical ──
    m["name"] = li.get("listing_name") or "Unnamed listing"
    m["bedrooms"] = pd.get("bedrooms")
    m["bathrooms"] = pd.get("baths")
    m["sleeps"] = pd.get("guests")
    m["max_guests"] = pd.get("guests")

    # ── Financial ──
    adr = pm.get("ttm_avg_rate")
    if adr is not None:
        adr = float(adr)
    m["adr_raw"] = adr
    m["adr"] = adr

    annual_rev = pm.get("ttm_revenue")
    if annual_rev is not None:
        annual_rev = float(annual_rev)
    m["annual_revenue_raw"] = annual_rev
    m["annual_revenue"] = annual_rev

    m["occupancy_pct"] = _coerce_occ_pct(pm.get("ttm_occupancy"))

    # Days available = industry standard "days on market" = 365 - blocked_days.
    # NOT AirROI's ttm_available_days which means "vacant/unbooked nights."
    blocked = int(pm.get("ttm_blocked_days") or 0)
    m["days_available"] = 365 - blocked  # days the property was listed and active

    days_reserved = pm.get("ttm_days_reserved")
    m["days_reserved"] = int(days_reserved) if days_reserved else 0

    rev_potential = _derive_revenue_potential(annual_rev, adr, m["days_available"], pm.get("ttm_occupancy"))
    m["revenue_potential_raw"] = rev_potential
    m["revenue_potential"] = rev_potential

    # ── Quality ──
    rating = ratings.get("rating_overall")
    m["rating"] = float(rating) if rating is not None else 0.0

    reviews = ratings.get("num_reviews") or 0
    try:
        m["reviews"] = int(reviews)
    except (TypeError, ValueError):
        m["reviews"] = 0
    m["review_count"] = m["reviews"]

    # ── Amenities (scorer does keyword scan on this list) ──
    raw_amenities = pd.get("amenities") or []
    m["amenities"] = _flatten_amenities(raw_amenities)
    m["amenities_raw"] = raw_amenities  # preserve for badge rendering
    m["features"] = []

    # ── Description ──
    descr_parts = []
    for val in (li.get("description"), li.get("listing_type"), li.get("room_type")):
        if val and isinstance(val, str):
            descr_parts.append(val)
    m["description"] = " ".join(descr_parts)

    return m


def map_batch_for_scorer(airroi_listings: list[dict]) -> list[dict]:
    """Bulk convenience wrapper around map_for_scorer."""
    return [map_for_scorer(c) for c in airroi_listings if isinstance(c, dict)]


# ── Public: Stage 2 — scored dict → CompProperty ────────────────────

def to_comp_property(scored_comp: dict) -> CompProperty:
    """Convert a post-scoring AirROI comp into the CompProperty model used by
    the Jinja template."""
    li = scored_comp.get("listing_info") or {}
    listing_id = li.get("listing_id") or ""
    airbnb_url = f"https://www.airbnb.com/rooms/{listing_id}" if listing_id else ""

    image_url = li.get("cover_photo_url") or ""

    # Build text context for badge extractor
    text_context = " ".join(str(scored_comp.get(k, "")) for k in (
        "name", "description",
    ))
    # Also check listing_info fields directly
    text_context += " " + " ".join(str(li.get(k, "")) for k in (
        "listing_name", "description", "listing_type",
    ))

    amenities_raw = scored_comp.get("amenities_raw") or []

    feature_badges, badge_emojis = _amenities_to_badges(
        amenities_raw, text_context=text_context,
    )

    def _safe_float(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _safe_int(v, default=0):
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    return CompProperty(
        name=scored_comp.get("name") or "Unnamed listing",
        image_url=image_url,
        sleeps=_safe_int(scored_comp.get("sleeps") or scored_comp.get("max_guests")),
        bedrooms=_safe_int(scored_comp.get("bedrooms")),
        bathrooms=_safe_float(scored_comp.get("bathrooms")),
        rating=_safe_float(scored_comp.get("rating")),
        review_count=_safe_int(scored_comp.get("reviews") or scored_comp.get("review_count")),
        feature_badges=feature_badges,
        badge_emojis=badge_emojis,
        revenue_potential=_safe_float(scored_comp.get("revenue_potential_raw") or scored_comp.get("revenue_potential")),
        annual_revenue=_safe_float(scored_comp.get("annual_revenue_raw") or scored_comp.get("annual_revenue")),
        occupancy_pct=_safe_float(scored_comp.get("occupancy_pct")),
        adr=_safe_float(scored_comp.get("adr_raw") or scored_comp.get("adr")),
        days_available=_safe_int(scored_comp.get("days_available"), 365),
        days_booked=_safe_int(scored_comp.get("days_reserved"), 0),
        airbnb_url=airbnb_url,
    )


# ── Public: subject adapter — AirROI estimate → scorer subject dict ──

def subject_for_scorer(
    prop: PropertyBasics,
    estimate_data: dict,
) -> dict:
    """Build a subject-property dict in the shape comp_scorer.rank_comps
    expects for `subject`."""
    adr = estimate_data.get("average_daily_rate")
    if adr is not None:
        adr = float(adr)

    amenities = list(prop.amenities or [])

    description_parts = [
        prop.property_type or "",
        prop.title or "",
        prop.short_address or "",
        prop.description or "",
    ]
    description = " ".join(p for p in description_parts if p).strip()

    return {
        "title":         prop.title or prop.property_type or prop.short_address,
        "description":   description,
        "amenities":     amenities,
        "configuration": f"{prop.bedrooms}BR / {prop.bathrooms}BA / Sleeps {prop.max_guests}",
        "bedrooms":      prop.bedrooms,
        "bathrooms":     prop.bathrooms,
        "max_guests":    prop.max_guests,
        "guests":        prop.max_guests,
        "adr":           adr,
        "host": {
            "is_superhost": prop.is_superhost,
        },
        "reviews": [],
    }
