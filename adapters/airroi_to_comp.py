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

import re
from typing import Optional

from schema import CompProperty, PropertyBasics, SubjectPerformance


# ── Amenity mapping: AirROI key → (scorer keyword, display label, emoji) ──

# AirROI returns amenities as a LIST of strings.
# We map them to: scorer keyword, display label, emoji.
# AirROI returns amenities as Title Case DISPLAY STRINGS ("Hot tub", "BBQ grill"),
# never snake_case. Verified: 136 distinct strings across 200 live records, ZERO
# containing an underscore. The previous snake_case maps matched nothing at all,
# so badge Pass 2 never fired and the skip-list never fired -- which is why comp
# cards advertised "Hair Dryer" and "Hot Water" as headline features.
#
# Keys here MUST be exact members of amenity_vocab.json (tests/test_amenities.py
# enforces this). Never substring-match them: "Pool" is a substring of
# "Pool table" and "Pool view"; "Fire" is a substring of the near-universal
# "Fire extinguisher".
_AMENITY_MAP: dict[str, tuple[str, str, str]] = {
    "Hot tub":                  ("hot tub",         "Hot Tub",         "\U0001F6C1"),
    "Pool":                     ("pool",            "Pool",            "\U0001F3CA"),
    "Ski-in/Ski-out":           ("ski-in/out",      "Ski-in/Out",      "\U0001F3BF"),
    "Indoor fireplace":         ("fireplace",       "Fireplace",       "\U0001F525"),
    "Fire pit":                 ("fire pit",        "Fire Pit",        "\U0001F525"),
    "Pets allowed":             ("pet friendly",    "Pet-Friendly",    "\U0001F415"),
    "Free parking on premises": ("parking",         "Parking",         "\U0001F697"),
    "EV charger":               ("ev charger",      "EV Charger",      "\U000026A1"),
    "Gym":                      ("gym",             "Gym",             "\U0001F4AA"),
    "Exercise equipment":       ("gym",             "Gym",             "\U0001F4AA"),
    "Pool table":               ("games room",      "Games Room",      "\U0001F3B1"),
    "Game console":             ("games room",      "Games Room",      "\U0001F3AE"),
    "Arcade games":             ("games room",      "Games Room",      "\U0001F579"),
    "Life size games":          ("games room",      "Games Room",      "\U0001F3AF"),
    "BBQ grill":                ("bbq",             "BBQ",             "\U0001F356"),
    "Waterfront":               ("waterfront",      "Waterfront",      "\U0001F30A"),
    "Lake access":              ("lake access",     "Lake Access",     "\U0001F30A"),
    "Beach access":             ("beach access",    "Beach Access",    "\U0001F3D6"),
    "Ocean view":               ("views",           "Ocean View",      "\U0001F30A"),
    "River view":               ("views",           "River View",      "\U0001F3DE"),
    "Pool view":                ("views",           "Pool View",       "\U0001F3CA"),
    "Garden view":              ("views",           "Garden View",     "\U0001F33F"),
    "Patio or balcony":         ("patio",           "Patio/Balcony",   "\U0001F33F"),
    "Outdoor kitchen":          ("outdoor kitchen", "Outdoor Kitchen", "\U0001F373"),
    "Outdoor shower":           ("outdoor shower",  "Outdoor Shower",  "\U0001F6BF"),
    "Outdoor playground":       ("playground",      "Playground",      "\U0001F6DD"),
    "Resort access":            ("resort",          "Resort Access",   "\U0001F3E8"),
    "Elevator":                 ("elevator",        "Elevator",        "\U0001F6D7"),
    "Kayak":                    ("kayak",           "Kayak",           "\U0001F6F6"),
    "Bikes":                    ("bikes",           "Bikes",           "\U0001F6B2"),
    "Backyard":                 ("backyard",        "Backyard",        "\U0001F333"),
}

# Only these earn a badge slot. Anything not listed is not a selling point --
# a badge reading "Shampoo" costs a slot a hot tub deserved.
_BADGE_PRIORITY = [
    "Ski-in/Ski-out",
    "Hot tub", "Pool", "Waterfront",
    "Beach access", "Lake access",
    "Ocean view", "River view", "Pool view", "Garden view",
    "Indoor fireplace", "Fire pit",
    "Pool table", "Game console", "Arcade games", "Life size games",
    "Gym", "Exercise equipment",
    "Outdoor kitchen", "Outdoor shower", "Outdoor playground",
    "Resort access", "Kayak", "Bikes", "Backyard",
    "Pets allowed", "EV charger", "BBQ grill",
    "Patio or balcony", "Elevator", "Free parking on premises",
]

# Kept for backward compatibility with importers. Badge building is now an
# ALLOWLIST (_BADGE_PRIORITY), not a denylist, so a comp with no premium
# amenity shows fewer badges instead of junk ones.
_SKIP_AMENITIES: set[str] = set()

_TEXT_SIGNAL_PATTERNS: list[tuple[tuple[str, ...], str, str]] = [
    # Free-text signals ONLY for things with no amenity-vocabulary key.
    # "sauna" is here because it appears in 0 of the 136 real amenity strings.
    # Matched on word boundaries: the old bare "spa" token matched
    # "space"/"spacious"/"workspace" and stamped Wellness on 151/200 cards.
    # The "luxury/premium/executive" signal is deliberately gone — it fired on
    # 61/125 comps purely from host-written marketing adjectives. Objective
    # host_info.professional_management and guest_favorite replace it.
    (("ski-in", "ski in", "ski out", "ski/out", "ski access"),
     "Ski-in/Out", "\U0001F3BF"),
    (("sauna", "cold plunge", "steam room"),
     "Sauna", "\U0001F9D6"),
    (("mountain view", "valley view", "panoramic view", "scenic view"),
     "Views", "\U0001F3D4"),
    (("village", "village core", "walk to village", "steps from village", "downtown"),
     "Village", "\U0001F4CD"),
]


def _strip_html(text: str) -> str:
    """Drop HTML tags before text-signal matching.

    AirROI descriptions contain markup and `space</b><br` was matching the
    old bare "spa" token.
    """
    return re.sub(r"<[^>]+>", " ", text or "")


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

    # Pass 1: text signals from name/description, word-boundary matched.
    text_lower = _strip_html(text_context or "").lower()
    for keywords, label, emoji in _TEXT_SIGNAL_PATTERNS:
        if any(re.search(r"\b" + re.escape(k) + r"\b", text_lower) for k in keywords):
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

    # Deliberately NO catch-all pass. If a comp has no premium amenity it
    # shows fewer than `limit` badges. The old fallback title-cased whatever
    # came first in the raw list, which is why cards advertised "Bathtub",
    # "Hair Dryer" and "Shampoo" as headline features.

    return labels, emojis


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0):
    try:
        return int(float(v)) if v is not None else default
    except (TypeError, ValueError):
        return default


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


# Corpus-wide fee uplift: ttm_revenue / (ttm_avg_rate x nights_booked).
# Median 1.192 over 200 live records (p10 1.069, p90 1.334). Used only when a
# listing has no cleaning_fee/length-of-stay data to compute its own uplift.
DEFAULT_FEE_FACTOR = 1.19


# Fallback achievable-occupancy ceiling when there is no pool to measure.
# Deliberately NOT 90%: no market in the 200-record corpus reaches a p90 of
# 90% adjusted occupancy (observed range 57%-84%). A "potential" the market
# has never achieved is not a projection, it is a sales pitch.
DEFAULT_OCC_CEILING = 0.65
OCC_CEILING_FLOOR = 0.45
OCC_CEILING_CAP = 0.85


def _derive_revenue_potential(
    annual_revenue: Optional[float],
    adr: Optional[float],
    nights_listed: int,
    *,
    cleaning_fee: Optional[float] = None,
    avg_los: Optional[float] = None,
    occ_ceiling: float = DEFAULT_OCC_CEILING,
) -> Optional[float]:
    """Revenue ceiling on the SAME fee-inclusive basis as ttm_revenue.

    Ceiling = (adr x open nights x achievable occupancy) + cleaning fees.

    `occ_ceiling` is the occupancy a strong operator in THIS market actually
    reaches (p75 of the comp pool, see market_occupancy_ceiling). It is not a
    universal constant.

    `nights_listed` must be OPEN INVENTORY (total - blocked), never
    ttm_available_days (which is unsold nights and inverts the result).
    """
    if not (adr and adr > 0 and nights_listed and nights_listed > 0):
        return None
    booked_ceiling = nights_listed * float(occ_ceiling)
    room = float(adr) * booked_ceiling
    if cleaning_fee and avg_los and avg_los > 0:
        fees = float(cleaning_fee) * (booked_ceiling / float(avg_los))
    else:
        fees = room * (DEFAULT_FEE_FACTOR - 1.0)
    return round(room + fees, 2)


def market_occupancy_ceiling(mapped: list[dict]) -> float:
    """Achievable occupancy for this market = p75 of the pool's adjusted occupancy.

    Bounded to [45%, 85%] so a pathologically dead or pathologically hot sample
    cannot produce an absurd ceiling.
    """
    occ = sorted(r["occupancy_pct"] for r in mapped
                 if r.get("occupancy_pct") is not None)
    if not occ:
        return DEFAULT_OCC_CEILING
    p75 = occ[min(len(occ) - 1, (3 * len(occ)) // 4)] / 100.0
    return max(OCC_CEILING_FLOOR, min(OCC_CEILING_CAP, p75))


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
    host = airroi_listing.get("host_info") or {}

    # ── Identity / physical ──
    m["name"] = li.get("listing_name") or "Unnamed listing"
    m["bedrooms"] = pd.get("bedrooms")
    m["bathrooms"] = pd.get("baths")
    m["sleeps"] = pd.get("guests")
    m["max_guests"] = pd.get("guests")

    # ── Financial ──
    # NOTE ON BASIS (verified against 200 live AirROI records):
    #   ttm_revenue  INCLUDES cleaning + guest fees (median 1.192x room revenue)
    #   ttm_avg_rate EXCLUDES them (it is the nightly room rate)
    # Never divide one by the other without accounting for the fee gap.
    adr = pm.get("ttm_avg_rate")
    if adr is not None:
        adr = float(adr)
    m["adr_raw"] = adr
    m["adr"] = adr

    annual_rev = pm.get("ttm_revenue")
    if annual_rev is not None:
        annual_rev = float(annual_rev)
    m["annual_revenue_raw"] = annual_rev
    m["annual_revenue"] = annual_rev          # fee-INCLUSIVE

    # ── Night accounting ──
    # ttm_available_days is UNSOLD nights (365 - days_reserved), NOT open
    # inventory. Verified invariant, holds 200/200. Never use it as a
    # denominator and never call it "available".
    total   = pm.get("ttm_total_days")
    blocked = pm.get("ttm_blocked_days")
    booked  = pm.get("ttm_days_reserved")
    total   = int(total) if total is not None else 365
    blocked = int(blocked) if blocked is not None else 0
    m["nights_booked"] = int(booked) if booked is not None else 0
    m["nights_listed"] = max(0, total - blocked)          # open inventory
    m["nights_unsold"] = pm.get("ttm_available_days")     # reference only
    m["total_days"]    = total
    m["blocked_days"]  = blocked

    # Room revenue (fee-EXCLUSIVE) — the basis that pairs with adr
    m["room_revenue"] = (adr or 0.0) * m["nights_booked"]

    # Occupancy: prefer ADJUSTED (booked / open inventory). Raw ttm_occupancy
    # divides by 365 and so understates any listing that was blocked off.
    # 73/125 live comps have blocked days; max understatement observed 56.6pts.
    m["occupancy_pct"] = _coerce_occ_pct(pm.get("ttm_adjusted_occupancy"))
    if m["occupancy_pct"] is None:
        m["occupancy_pct"] = _coerce_occ_pct(pm.get("ttm_occupancy"))
    m["occupancy_raw_pct"] = _coerce_occ_pct(pm.get("ttm_occupancy"))

    # AirROI computes RevPAR itself — use it instead of hand-rolling a ratio.
    m["revpar"] = pm.get("ttm_revpar")
    m["adjusted_revpar"] = pm.get("ttm_adjusted_revpar")

    # ── Freshness (trailing 90 days) — is this comp alive RIGHT NOW? ──
    m["l90d_nights_booked"] = pm.get("l90d_days_reserved")
    m["l90d_occupancy_pct"] = _coerce_occ_pct(pm.get("l90d_adjusted_occupancy"))
    m["l90d_revenue"] = pm.get("l90d_revenue")

    # ── Fees / stay pattern ──
    pricing = airroi_listing.get("pricing_info") or {}
    m["cleaning_fee"] = pricing.get("cleaning_fee")
    m["avg_length_of_stay"] = pm.get("ttm_avg_length_of_stay")
    m["min_nights"] = (airroi_listing.get("booking_settings") or {}).get("min_nights")

    rev_potential = _derive_revenue_potential(
        annual_rev, adr, m["nights_listed"],
        cleaning_fee=m["cleaning_fee"], avg_los=m["avg_length_of_stay"],
    )
    m["revenue_potential_raw"] = rev_potential
    m["revenue_potential"] = rev_potential

    # ── Quality ──
    # AirROI reports rating_overall == 0.0 as a "too few reviews" SENTINEL,
    # not as a real zero rating. Keep it distinguishable from a genuine score.
    rating = ratings.get("rating_overall")
    m["rating_is_unrated"] = rating in (None, 0, 0.0)
    m["rating"] = None if m["rating_is_unrated"] else float(rating)
    m["rating_cleanliness"] = ratings.get("rating_cleanliness")
    m["rating_location"] = ratings.get("rating_location")
    m["rating_value"] = ratings.get("rating_value")

    # ── Host / listing quality signals (objective, replace luxury-adjective guessing) ──
    m["superhost"] = bool(host.get("superhost"))
    m["professional_management"] = bool(host.get("professional_management"))
    m["guest_favorite"] = bool(li.get("guest_favorite"))

    # ── Geo (for distance scoring) ──
    loc = airroi_listing.get("location_info") or {}
    m["latitude"] = loc.get("latitude")
    m["longitude"] = loc.get("longitude")
    m["country_code"] = loc.get("country_code")

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
    """Map a comp pool, then re-derive revenue_potential against the pool itself.

    Two passes: the achievable-occupancy ceiling is a property of the MARKET,
    so it can only be computed once the whole pool is mapped.
    """
    mapped = [map_for_scorer(c) for c in airroi_listings if isinstance(c, dict)]
    if not mapped:
        return mapped
    ceiling = market_occupancy_ceiling(mapped)
    for m in mapped:
        m["market_occ_ceiling"] = ceiling
        # A property already outperforming the market p75 has its OWN result as
        # the floor for its ceiling. Otherwise the card would print a "Revenue
        # Potential" below the "Annual Revenue" directly above it, which is
        # nonsense to a client and discredits the whole report.
        own = (m.get("occupancy_pct") or 0) / 100.0
        occ_for_potential = min(OCC_CEILING_CAP, max(ceiling, own))
        pot = _derive_revenue_potential(
            m.get("annual_revenue"), m.get("adr"), m.get("nights_listed") or 0,
            cleaning_fee=m.get("cleaning_fee"), avg_los=m.get("avg_length_of_stay"),
            occ_ceiling=occ_for_potential,
        )
        # Final guard: potential is a CEILING. It can never sit below actual.
        if pot is not None and m.get("annual_revenue"):
            pot = max(pot, round(float(m["annual_revenue"]) * 1.02, 2))
        m["revenue_potential_raw"] = pot
        m["revenue_potential"] = pot
    return mapped


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

    return CompProperty(
        name=scored_comp.get("name") or "Unnamed listing",
        image_url=image_url,
        sleeps=_safe_int(scored_comp.get("sleeps") or scored_comp.get("max_guests")),
        bedrooms=_safe_int(scored_comp.get("bedrooms")),
        bathrooms=_safe_float(scored_comp.get("bathrooms")),
        rating=(None if scored_comp.get("rating_is_unrated")
                else _safe_float(scored_comp.get("rating")) or None),
        review_count=_safe_int(scored_comp.get("reviews") or scored_comp.get("review_count")),
        feature_badges=feature_badges,
        badge_emojis=badge_emojis,
        revenue_potential=_safe_float(scored_comp.get("revenue_potential_raw") or scored_comp.get("revenue_potential")),
        annual_revenue=_safe_float(scored_comp.get("annual_revenue_raw") or scored_comp.get("annual_revenue")),
        occupancy_pct=_safe_float(scored_comp.get("occupancy_pct")),
        adr=_safe_float(scored_comp.get("adr_raw") or scored_comp.get("adr")),
        nights_booked=_safe_int(scored_comp.get("nights_booked"), 0),
        nights_listed=_safe_int(scored_comp.get("nights_listed"), 365),
        revpar=_safe_float(scored_comp.get("revpar")),
        l90d_occupancy_pct=(_safe_float(scored_comp["l90d_occupancy_pct"])
                            if scored_comp.get("l90d_occupancy_pct") is not None else None),
        l90d_nights_booked=(_safe_int(scored_comp["l90d_nights_booked"])
                            if scored_comp.get("l90d_nights_booked") is not None else None),
        superhost=bool(scored_comp.get("superhost")),
        professional_management=bool(scored_comp.get("professional_management")),
        guest_favorite=bool(scored_comp.get("guest_favorite")),
        cleaning_fee=(_safe_float(scored_comp["cleaning_fee"])
                      if scored_comp.get("cleaning_fee") is not None else None),
        min_nights=(_safe_int(scored_comp["min_nights"])
                    if scored_comp.get("min_nights") is not None else None),
        distance_km=(_safe_float(scored_comp["distance_km"])
                     if scored_comp.get("distance_km") is not None else None),
        latitude=scored_comp.get("latitude"),
        longitude=scored_comp.get("longitude"),
        rescued=bool(scored_comp.get("rescued")),
        airbnb_url=airbnb_url,
    )


# ── Public: subject's OWN performance — AirROI get_listing → SubjectPerformance ──

def subject_performance_from_listing(
    airroi_listing: Optional[dict],
) -> Optional[SubjectPerformance]:
    """Extract the subject's own trailing-12-month metrics from get_listing().

    Deliberately routed through map_for_scorer() rather than reading
    performance_metrics directly. That function already encodes the two
    corrections this data needs and both were expensive to find:

      * nights_listed = ttm_total_days - ttm_blocked_days. Reading
        ttm_available_days here instead INVERTS the number, because AirROI's
        "available" means UNSOLD, not open-for-booking.
      * ttm_revenue is fee-inclusive while ttm_avg_rate is not, so revenue
        and rate must never be divided into one another naively.

    A second implementation would drift from those on the first schema change.

    Returns None when the listing has no usable trailing history — a brand
    new listing, a delisted one, or a non-Airbnb subject. Callers must treat
    None as "infer from the market", not as zero.
    """
    if not isinstance(airroi_listing, dict):
        return None

    try:
        m = map_for_scorer(airroi_listing)
    except (TypeError, ValueError):
        return None

    booked = _safe_int(m.get("nights_booked"))
    revenue = _safe_float(m.get("annual_revenue"))

    # No bookings or no revenue means no history to anchor on. Say so with
    # None rather than returning a zeroed model that reads as "0% occupancy".
    if not booked or not revenue or revenue <= 0:
        return None

    return SubjectPerformance(
        annual_revenue=revenue,
        occupancy_pct=_safe_float(m.get("occupancy_pct")),
        occupancy_raw_pct=(_safe_float(m.get("occupancy_raw_pct"))
                           if m.get("occupancy_raw_pct") is not None else None),
        adr=_safe_float(m.get("adr")),
        nights_booked=booked,
        nights_listed=_safe_int(m.get("nights_listed")),
        revpar=(_safe_float(m.get("revpar"))
                if m.get("revpar") is not None else None),
        l90d_occupancy_pct=(_safe_float(m.get("l90d_occupancy_pct"))
                            if m.get("l90d_occupancy_pct") is not None else None),
        l90d_nights_booked=(_safe_int(m.get("l90d_nights_booked"))
                            if m.get("l90d_nights_booked") is not None else None),
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
        "airdna_adr":    adr,  # scorer accepts either key
        "latitude":      prop.latitude,
        "longitude":     prop.longitude,
        "host": {
            "is_superhost": prop.is_superhost,
        },
        "reviews": [],
    }
