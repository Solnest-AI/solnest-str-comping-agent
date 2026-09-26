#!/usr/bin/env python3
"""
STR Comp Scorer
Scores and ranks comp candidates against the subject property's
quality signals. Outputs the top N comps sorted by score descending.

Scoring categories (weighted):
  1. Physical match    — bedrooms, guest capacity, property config
  2. Financial match   — ADR proximity, RevPAN, revenue efficiency
  3. Quality match     — rating, review volume, occupancy performance
  4. Amenity match     — amenity signals, luxury tier, feature alignment
  5. Data reliability  — completeness of financial data, days available

Each category has a minimum threshold to prevent lopsided comps
(e.g. great amenities but terrible financials).

Usage:
  python comp_scorer.py \\
    --subject '{"title": "Luxurious Chalet", "amenities": ["hot_tub"], "adr": 850, "bedrooms": 4, "max_guests": 12}' \\
    --comps '[{...}, {...}]' \\
    --top 6
"""

import argparse
import json
import math
import re
import sys
from typing import Optional


# ── Comp admission floors ──────────────────────────────────────────────────
# A comparable must be a real, currently-operating rental. These are HARD
# gates, not score penalties: a dormant listing is not a cheap comp, it is
# evidence about a failed listing and does not belong in a client report.
MIN_ADJUSTED_OCCUPANCY = 20.0   # % of OPEN nights that must actually book
MIN_REVIEWS = 3                 # below this AND unrated = no usable history
MIN_NIGHTS_LISTED = 180         # a property blocked over half the year is a
                                # part-time rental, not a comparable operator.
                                # Adjusted occupancy flatters these badly: one
                                # live comp books 233 of 236 open nights (98.7%
                                # adjusted) while sitting blocked 129 days.
                                # Costs 2% of the live corpus.


# ── Amenity keyword maps ───────────────────────────────────────────────────

AMENITY_SIGNALS = {
    "hot_tub":    ["hot tub", "hot-tub", "hottub", "jacuzzi", "jetted tub"],
    "pool":       ["pool", "swimming pool", "indoor pool", "outdoor pool"],
    "ski_in_out": ["ski-in", "ski in", "ski out", "ski-out", "ski access", "ski-access"],
    "sauna":      ["sauna"],
    "games_room": ["game room", "games room", "game-room", "billiard", "pool table",
                   "foosball", "ping pong", "arcade"],
    "views":      ["mountain view", "lake view", "ocean view", "valley view",
                   "panoramic view", "scenic view", "overlook", "viewpoint"],
    "village":    ["village", "central", "downtown", "village core", "village access",
                   "walk to village", "steps from village"],
    # NOTE: "sauna" intentionally excluded — it has its own dedicated signal
    # above with a higher weight (+2). Including it here would double-count.
    "wellness":   ["cold plunge", "steam room", "yoga", "wellness", "spa"],
    "ev_charger": ["ev charger", "electric vehicle", "tesla charger", "ev charging"],
    "gym":        ["gym", "fitness center", "exercise room", "workout room", "home gym"],
    "fireplace":  ["fireplace", "fire place", "wood stove", "gas fireplace"],
}

# Signal -> exact amenity-vocabulary members. Matching is EXACT SET MEMBERSHIP
# against property_details.amenities, never a substring scan of name+description.
# The old scan awarded "+2 Pool match" to a listing whose description read
# "we do not have a gym, pool nor roof deck", and matched "Pool table"/"Pool view"
# as a pool and the near-universal "Fire extinguisher" as a fireplace.
AMENITY_VOCAB_SIGNALS: dict[str, tuple[str, ...]] = {
    "hot_tub":    ("Hot tub",),
    "pool":       ("Pool",),
    "ski_in_out": ("Ski-in/Ski-out",),
    "games_room": ("Pool table", "Game console", "Arcade games", "Life size games", "Board games"),
    "gym":        ("Gym", "Exercise equipment"),
    "fireplace":  ("Indoor fireplace", "Fire pit"),
    "views":      ("Ocean view", "River view", "Pool view", "Garden view", "Waterfront"),
    "water":      ("Beach access", "Lake access", "Waterfront"),
    "ev_charger": ("EV charger",),
    "pets":       ("Pets allowed",),
    "resort":     ("Resort access",),
}

# Signals with NO amenity-vocabulary key -- these must come from free text.
# "sauna" is here because it appears in 0 of the 136 real amenity strings.
TEXT_ONLY_SIGNALS: dict[str, tuple[str, ...]] = {
    "sauna":    ("sauna", "cold plunge", "steam room"),
    "village":  ("village", "downtown", "walk to village", "steps from village"),
}


LUXURY_KEYWORDS = [
    "luxurious", "luxury", "premium", "high-end", "upscale",
    "designer", "executive", "boutique", "elite", "grand",
]

PROFESSIONAL_MGMT_KEYWORDS = [
    "managed by", "hosted by", "property management", "vacation rental company",
    "professional host", "premier host", "management group", "hospitality",
    "rental agency", "guest services",
]

# Review sentiment keywords for subject quality detection
POSITIVE_REVIEW_KEYWORDS = [
    "spotless", "immaculate", "pristine", "stunning", "amazing", "incredible",
    "beautiful", "gorgeous", "phenomenal", "exceptional", "outstanding", "perfect",
    "luxurious", "world-class", "top-notch", "five star", "5 star", "best stay",
    "above and beyond", "exceeded expectations", "blown away",
]

NEGATIVE_REVIEW_KEYWORDS = [
    "dirty", "filthy", "unclean", "stained", "mold", "mould", "broken",
    "outdated", "worn", "noisy", "loud", "disappointing", "misleading",
    "overpriced", "not worth", "wouldn't recommend", "avoid", "terrible",
    "worst", "bug", "cockroach", "mice", "rodent", "smell", "odor",
]


# ── Subject quality signal detection ──────────────────────────────────────

def detect_subject_signals(subject: dict) -> dict:
    """
    Detect quality signals from subject property data.
    Returns a dict of signal_name -> True/False plus quality_tier.
    """
    searchable = " ".join([
        subject.get("title", ""),
        subject.get("description", ""),
        " ".join(subject.get("amenities", [])),
        subject.get("configuration", ""),
    ]).lower()

    signals = {}

    # Luxury flag
    signals["luxury"] = any(kw in searchable for kw in LUXURY_KEYWORDS)

    # Professional management flag
    signals["professional_mgmt"] = any(kw in searchable for kw in PROFESSIONAL_MGMT_KEYWORDS)

    # Amenity signals. The subject's own amenity list uses the same AirROI
    # vocabulary when it came from a listing lookup; fall back to text otherwise.
    subject_amenities = set(subject.get("amenities") or [])
    for signal, vocab in AMENITY_VOCAB_SIGNALS.items():
        signals[signal] = bool(subject_amenities & set(vocab)) or any(
            kw in searchable for kw in AMENITY_SIGNALS.get(signal, [])
        )
    for signal, keywords in TEXT_ONLY_SIGNALS.items():
        signals[signal] = any(kw in searchable for kw in keywords)

    # Review sentiment analysis (from Apify subject reviews)
    reviews = subject.get("reviews", [])
    if reviews:
        all_review_text = " ".join(
            (r.get("text", "") if isinstance(r, dict) else str(r))
            for r in reviews
        ).lower()

        pos_hits = sum(1 for kw in POSITIVE_REVIEW_KEYWORDS if kw in all_review_text)
        neg_hits = sum(1 for kw in NEGATIVE_REVIEW_KEYWORDS if kw in all_review_text)

        signals["review_sentiment_positive"] = pos_hits
        signals["review_sentiment_negative"] = neg_hits

        # Derive quality tier from sentiment
        if pos_hits >= 5 and neg_hits <= 1:
            signals["quality_tier"] = "elite"
        elif pos_hits >= 3 and neg_hits <= 2:
            signals["quality_tier"] = "premium"
        elif neg_hits >= 4:
            signals["quality_tier"] = "underperformer"
        else:
            signals["quality_tier"] = "standard"
    else:
        signals["review_sentiment_positive"] = 0
        signals["review_sentiment_negative"] = 0
        signals["quality_tier"] = "unknown"

    # Superhost signal
    host = subject.get("host", {})
    if isinstance(host, dict):
        signals["superhost"] = host.get("is_superhost", False)
    else:
        signals["superhost"] = False

    return signals


# ── Comp text builder ──────────────────────────────────────────────────────

def comp_searchable_text(comp: dict) -> str:
    """Build a single searchable string from all comp fields."""
    parts = [
        comp.get("name", ""),
        comp.get("description", ""),
        " ".join(comp.get("amenities", [])),
        " ".join(comp.get("features", [])),
    ]
    return " ".join(parts).lower()


# ── Category scoring functions ─────────────────────────────────────────────

def _score_physical_match(
    comp_bedrooms: Optional[int],
    comp_sleeps: Optional[int],
    subject_bedrooms: Optional[int],
    subject_guests: Optional[int],
) -> tuple:
    """Score physical property match. Returns (points, breakdown_lines)."""
    score = 0
    breakdown = []

    # Bedroom proximity
    if subject_bedrooms and comp_bedrooms:
        if comp_bedrooms == subject_bedrooms:
            score += 3
            breakdown.append(f"+3 Exact bedroom match ({comp_bedrooms}BR)")
        else:
            score += 1
            breakdown.append(f"+1 Close bedroom match ({comp_bedrooms}BR vs {subject_bedrooms}BR)")

    # Guest capacity proximity
    if subject_guests and comp_sleeps:
        guest_diff = abs(comp_sleeps - subject_guests)
        if guest_diff == 0:
            score += 2
            breakdown.append(f"+2 Exact guest capacity ({comp_sleeps})")
        elif guest_diff <= 2:
            score += 1
            breakdown.append(f"+1 Close guest capacity ({comp_sleeps} vs {subject_guests})")

    return score, breakdown


def _score_financial_match(
    comp_adr: float,
    comp_occ: Optional[float],
    comp_revpar: Optional[float],
    comp_revenue_potential: Optional[float],
    comp_annual_revenue: Optional[float],
    subject_adr: Optional[float],
) -> tuple:
    """Score financial similarity. Returns (points, breakdown_lines)."""
    score = 0
    breakdown = []

    # ADR proximity (tighter tiers)
    if subject_adr and comp_adr:
        adr_diff_pct = abs(comp_adr - subject_adr) / subject_adr
        if adr_diff_pct <= 0.10:
            score += 3
            breakdown.append(f"+3 ADR within 10% of subject (CA${comp_adr:.0f})")
        elif adr_diff_pct <= 0.20:
            score += 2
            breakdown.append(f"+2 ADR within 20% of subject (CA${comp_adr:.0f})")
        elif adr_diff_pct <= 0.35:
            score += 1
            breakdown.append(f"+1 ADR within 35% of subject (CA${comp_adr:.0f})")

    # RevPAR — use AirROI's own ttm_revpar. The previous hand-rolled
    # "annual_revenue / days_available" divided revenue by UNSOLD nights and
    # overstated by up to 5x.
    if comp_revpar is not None and subject_adr:
        revpar_ratio = comp_revpar / subject_adr
        if revpar_ratio >= 0.55:
            score += 3
            breakdown.append(f"+3 Strong RevPAR ({comp_revpar:.0f}/night — high yield)")
        elif revpar_ratio >= 0.40:
            score += 2
            breakdown.append(f"+2 Solid RevPAR ({comp_revpar:.0f}/night)")
        elif revpar_ratio >= 0.25:
            score += 1
            breakdown.append(f"+1 Moderate RevPAR ({comp_revpar:.0f}/night)")
        elif revpar_ratio < 0.15:
            score -= 1
            breakdown.append(f"-1 Weak RevPAR ({comp_revpar:.0f}/night — low yield)")

    # Revenue efficiency — actual vs potential
    if comp_revenue_potential and comp_annual_revenue and comp_revenue_potential > 0:
        efficiency = min(1.0, comp_annual_revenue / comp_revenue_potential)
        # Bands recalibrated against the corrected, market-relative potential.
        # "Potential" is now revenue at this market's p75 occupancy, so an
        # efficiency of 1.0 means "performs like a top-quartile operator here".
        # The old bands were tuned against an inverted denominator.
        if efficiency >= 0.90:
            score += 3
            breakdown.append(f"+3 Top-quartile revenue efficiency ({efficiency:.0%} of market potential)")
        elif efficiency >= 0.70:
            score += 2
            breakdown.append(f"+2 Strong revenue efficiency ({efficiency:.0%} of market potential)")
        elif efficiency >= 0.50:
            score += 1
            breakdown.append(f"+1 Moderate efficiency ({efficiency:.0%} of market potential)")
        elif efficiency < 0.25:
            score -= 1
            breakdown.append(f"-1 Low efficiency ({efficiency:.0%} of market potential — underperforming)")

    return score, breakdown


def _score_quality_match(
    comp_rating: Optional[float],
    comp_reviews: int,
    comp_occ: Optional[float],
    subject_signals: dict,
    comp: Optional[dict] = None,
) -> tuple:
    """Score quality/performance signals. Returns (points, breakdown_lines)."""
    score = 0
    breakdown = []

    # Review volume (reliability signal)
    if comp_reviews >= 100:
        score += 4
        breakdown.append(f"+4 High review volume ({comp_reviews} reviews — very reliable)")
    elif comp_reviews >= 50:
        score += 3
        breakdown.append(f"+3 Strong review volume ({comp_reviews} reviews)")
    elif comp_reviews >= 20:
        score += 2
        breakdown.append(f"+2 Solid review volume ({comp_reviews} reviews)")
    elif comp_reviews >= 10:
        score += 1
        breakdown.append(f"+1 Moderate review volume ({comp_reviews} reviews)")
    elif comp_reviews < 5:
        score -= 2
        breakdown.append(f"-2 Very few reviews ({comp_reviews}) — unreliable data point")

    # Rating quality.
    # comp_rating is None when AirROI reports rating_overall == 0.0, which is a
    # "too few reviews" SENTINEL, not a real zero. An unrated comp must not
    # collect rating points, and must not collect the low-rating penalty either.
    if comp_rating is None:
        score -= 1
        breakdown.append("-1 Unrated (too few reviews for a rating) — low confidence")
    elif comp_rating >= 4.9 and comp_reviews >= 10:
        score += 3
        breakdown.append(f"+3 Elite rating ({comp_rating} with {comp_reviews} reviews)")
    elif comp_rating >= 4.8 and comp_reviews >= 10:
        score += 2
        breakdown.append(f"+2 Excellent rating ({comp_rating})")
    elif comp_rating >= 4.7:
        score += 1
        breakdown.append(f"+1 Good rating ({comp_rating})")
    elif comp_rating < 4.3:
        score -= 2
        breakdown.append(f"-2 Low rating ({comp_rating}) — likely underperformer")

    # Subject quality tier matching (skipped entirely when the comp is unrated)
    quality_tier = subject_signals.get("quality_tier", "unknown")
    if comp_rating is not None:
        if quality_tier == "elite" and comp_rating >= 4.9 and comp_reviews >= 20:
            score += 2
            breakdown.append("+2 Quality tier match (elite subject, elite comp)")
        elif quality_tier == "premium" and comp_rating >= 4.8:
            score += 1
            breakdown.append("+1 Quality tier match (premium subject, high-rated comp)")
        elif quality_tier == "underperformer" and comp_rating < 4.5:
            score += 1
            breakdown.append("+1 Quality tier match (both lower-performing)")

    # Superhost preference — use the comp's OWN superhost flag (AirROI gives it
    # to us, 0% null) rather than inferring it from rating + review count.
    if comp is not None and subject_signals.get("superhost") and comp.get("superhost"):
        score += 1
        breakdown.append("+1 Superhost comp (subject is Superhost)")

    # Objective operator-quality signals. These replace guessing "luxury" and
    # "professionally managed" from host-written marketing adjectives.
    if comp is not None:
        if comp.get("professional_management"):
            score += 2
            breakdown.append("+2 Professionally managed (AirROI host flag)")
        if comp.get("guest_favorite"):
            score += 2
            breakdown.append("+2 Airbnb Guest Favorite")

    # Occupancy performance (adjusted: booked / open nights)
    if comp_occ is not None:
        if comp_occ >= 65:
            score += 3
            breakdown.append(f"+3 Strong occupancy ({comp_occ:.0f}%) — proven performer")
        elif comp_occ >= 50:
            score += 2
            breakdown.append(f"+2 Healthy occupancy ({comp_occ:.0f}%)")
        elif comp_occ >= 35:
            score += 1
            breakdown.append(f"+1 Moderate occupancy ({comp_occ:.0f}%)")
        elif comp_occ < 25:
            score -= 2
            breakdown.append(f"-2 Very low occupancy ({comp_occ:.0f}%) — may be inactive or new")

    return score, breakdown


_AMENITY_POINTS = {
    "hot_tub": ("Hot Tub match", 2),
    "pool": ("Pool match", 2),
    "ski_in_out": ("Ski-in/out match", 3),
    "games_room": ("Games room match", 1),
    "views": ("Views match", 1),
    "gym": ("Gym match", 1),
    "fireplace": ("Fireplace match", 1),
    "water": ("Waterfront/beach match", 2),
    "ev_charger": ("EV charger match", 1),
    "pets": ("Pet-friendly match", 1),
    "resort": ("Resort access match", 1),
}

_TEXT_POINTS = {
    "sauna": ("Sauna match", 2),
    "village": ("Village/central match", 1),
}


def _score_amenity_match(text: str, subject_signals: dict,
                         comp_amenities: Optional[list] = None) -> tuple:
    """Score amenity alignment against the subject.

    Structured amenities are matched by EXACT set membership against AirROI's
    vocabulary. Only signals with no vocabulary key fall back to free text, and
    those are word-boundary matched with a negation guard.
    """
    score = 0
    breakdown = []
    have = set(comp_amenities or [])

    for signal, (label, points) in _AMENITY_POINTS.items():
        if not subject_signals.get(signal):
            continue
        if have & set(AMENITY_VOCAB_SIGNALS.get(signal, ())):
            score += points
            breakdown.append(f"+{points} {label}")

    for signal, (label, points) in _TEXT_POINTS.items():
        if not subject_signals.get(signal):
            continue
        for kw in TEXT_ONLY_SIGNALS.get(signal, ()):
            if not re.search(r"\b" + re.escape(kw) + r"\b", text):
                continue
            # Negation guard: "no sauna", "we do not have a sauna"
            window = text[max(0, text.find(kw) - 40):text.find(kw)]
            if re.search(r"\b(no|not|without|dont|don't|lacks?)\b", window):
                continue
            score += points
            breakdown.append(f"+{points} {label}")
            break

    return score, breakdown


def _score_data_reliability(comp: dict, comp_booked: Optional[int]) -> tuple:
    """Score data completeness and listing availability. Returns (points, breakdown_lines)."""
    score = 0
    breakdown = []

    # Data completeness
    financial_fields = ["revenue_potential", "annual_revenue", "occupancy_pct", "adr", "nights_booked"]
    present_fields = sum(1 for f in financial_fields if comp.get(f))
    if present_fields == len(financial_fields):
        score += 2
        breakdown.append("+2 Full financial data")
    elif present_fields >= 3:
        score += 1
        breakdown.append(f"+1 Partial financial data ({present_fields}/{len(financial_fields)} fields)")
    elif present_fields <= 1:
        score -= 3
        breakdown.append(f"-3 Missing most financial data ({present_fields}/{len(financial_fields)} fields)")

    # Booked nights — a real operating history, not a listing that sat empty.
    # Bands are on NIGHTS BOOKED (ttm_days_reserved). The old code banded on
    # ttm_available_days (UNSOLD nights) and so paid +2 to dormant listings.
    if comp_booked is not None:
        if comp_booked >= 200:
            score += 2
            breakdown.append(f"+2 Full-time rental ({comp_booked} nights booked)")
        elif comp_booked >= 120:
            score += 1
            breakdown.append(f"+1 Near full-time rental ({comp_booked} nights booked)")
        elif comp_booked < 45:
            score -= 2
            breakdown.append(f"-2 Barely booked ({comp_booked} nights) — not a comparable operator")

    # Freshness — is this comp alive NOW, or is TTM averaging in dead months?
    if comp.get("l90d_nights_booked") is not None:
        l90 = comp["l90d_nights_booked"]
        if l90 == 0:
            score -= 3
            breakdown.append("-3 Dead in the last 90 days (0 nights booked) — stale comp")
        elif l90 < 5:
            score -= 1
            breakdown.append(f"-1 Nearly dormant recently ({l90} nights booked in 90d)")

    return score, breakdown


def _haversine_km(lat1, lon1, lat2, lon2) -> Optional[float]:
    """Great-circle distance in km, or None if any coordinate is missing."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _score_distance(comp: dict, subject: dict) -> tuple:
    """Proximity to the subject. Previously there was NO distance term at all —
    comps could sit anywhere in the metro. Returns (points, breakdown)."""
    d = _haversine_km(subject.get("latitude"), subject.get("longitude"),
                      comp.get("latitude"), comp.get("longitude"))
    comp["distance_km"] = round(d, 2) if d is not None else None
    if d is None:
        return 0, []
    if d <= 1.0:
        return 4, [f"+4 Same immediate area ({d:.1f} km from subject)"]
    if d <= 3.0:
        return 3, [f"+3 Very close ({d:.1f} km)"]
    if d <= 8.0:
        return 2, [f"+2 Same submarket ({d:.1f} km)"]
    if d <= 15.0:
        return 1, [f"+1 Same market ({d:.1f} km)"]
    if d > 25.0:
        return -3, [f"-3 Far from subject ({d:.1f} km) — different submarket"]
    return 0, []


# ── Main scoring function ──────────────────────────────────────────────────

def score_comp(
    comp: dict,
    subject_signals: dict,
    subject_adr: Optional[float],
    subject_bedrooms: Optional[int],
    subject_guests: Optional[int],
    subject: Optional[dict] = None,
) -> dict:
    """
    Score a single comp against subject across 5 weighted categories.

    Returns comp dict with added fields:
      - score: int (total score)
      - category_scores: dict of category -> points
      - score_breakdown: list of strings explaining each point
      - hard_fail: bool
      - hard_fail_reason: str
    """
    breakdown = []
    hard_fail = False
    hard_fail_reason = ""

    text = comp_searchable_text(comp)
    # The rate the comp was actually paid, never AirROI's ttm_avg_rate (off by
    # -13.8% to +18.9% on 30 comps, 2026-09-25). None skips the price checks.
    comp_adr = _parse_adr(comp.get("nightly_rate")) or None
    comp_bedrooms = _parse_int(comp.get("bedrooms"))
    comp_sleeps = _parse_int(comp.get("sleeps")) or _parse_int(comp.get("max_guests"))
    comp_occ = _parse_pct(comp.get("occupancy_pct"))
    comp_reviews = _parse_int(comp.get("reviews")) or _parse_int(comp.get("review_count")) or 0
    comp_rating = None if comp.get("rating_is_unrated") else _parse_float(comp.get("rating"))
    comp_booked = _parse_int(comp.get("nights_booked"))
    comp_revpar = _parse_float(comp.get("revpar"))
    comp_revenue_potential = _parse_currency(comp.get("revenue_potential"))
    comp_annual_revenue = _parse_currency(comp.get("annual_revenue"))

    # ── Hard disqualifiers ─────────────────────────────────────────────────

    # 1. Luxury subject → comp must be in top 60% of ADR range
    if subject_signals.get("luxury") and subject_adr and comp_adr:
        luxury_adr_floor = subject_adr * 0.55
        if comp_adr < luxury_adr_floor:
            hard_fail = True
            hard_fail_reason = (
                f"Luxury subject (ADR CA${subject_adr:.0f}) — "
                f"comp ADR CA${comp_adr:.0f} is below luxury floor CA${luxury_adr_floor:.0f}"
            )

    # 2. Bedroom count mismatch — scale tolerance with property size
    if subject_bedrooms is not None and comp_bedrooms is not None:
        if subject_bedrooms <= 4:
            max_bed_diff = 1
        elif subject_bedrooms <= 7:
            max_bed_diff = 2
        else:
            max_bed_diff = 3  # 8+ BR properties: allow ±3
        bed_diff = abs(comp_bedrooms - subject_bedrooms)
        if bed_diff > max_bed_diff:
            hard_fail = True
            hard_fail_reason = (
                f"Bedroom mismatch: subject has {subject_bedrooms}BR, "
                f"comp has {comp_bedrooms}BR (max ±{max_bed_diff} allowed)"
            )

    # 3. Guest capacity mismatch — scale tolerance with property size
    if subject_guests is not None and comp_sleeps is not None:
        if subject_guests <= 6:
            max_guest_diff = 3
        elif subject_guests <= 10:
            max_guest_diff = 4
        elif subject_guests <= 16:
            max_guest_diff = 6
        else:
            max_guest_diff = 8  # 17+ guest properties: allow ±8
        guest_diff = abs(comp_sleeps - subject_guests)
        if guest_diff > max_guest_diff:
            hard_fail = True
            hard_fail_reason = (
                f"Guest capacity mismatch: subject sleeps {subject_guests}, "
                f"comp sleeps {comp_sleeps} (max ±{max_guest_diff} allowed)"
            )

    # 4. Dormant / dead listings are not comparable operators. A comp that
    #    booked nothing in the trailing 90 days tells you about a failed
    #    listing, not about the market the client is buying into.
    if not hard_fail:
        adj_occ = comp_occ if comp_occ is not None else None
        l90 = comp.get("l90d_nights_booked")
        if adj_occ is not None and adj_occ < MIN_ADJUSTED_OCCUPANCY:
            hard_fail = True
            hard_fail_reason = (
                f"Dormant listing: {adj_occ:.0f}% adjusted occupancy "
                f"(floor {MIN_ADJUSTED_OCCUPANCY}%) — not a comparable operator"
            )
        elif l90 == 0 and comp_booked is not None and comp_booked < 60:
            hard_fail = True
            hard_fail_reason = (
                "Dead listing: 0 nights booked in the last 90 days and "
                f"only {comp_booked} in the trailing year"
            )
        elif (comp.get("nights_listed") is not None
              and comp["nights_listed"] < MIN_NIGHTS_LISTED):
            hard_fail = True
            hard_fail_reason = (
                f"Part-time rental: listed only {comp['nights_listed']} of 365 nights "
                f"(floor {MIN_NIGHTS_LISTED}) — occupancy is not comparable"
            )
        elif comp_reviews < MIN_REVIEWS and comp.get("rating_is_unrated"):
            hard_fail = True
            hard_fail_reason = (
                f"Insufficient history: {comp_reviews} reviews and no rating — "
                "unreliable data point"
            )

    if hard_fail:
        comp["score"] = 0
        comp["category_scores"] = {}
        comp["score_breakdown"] = []
        comp["hard_fail"] = True
        comp["hard_fail_reason"] = hard_fail_reason
        return comp

    # ── Category scoring ───────────────────────────────────────────────────

    category_scores = {}

    # Category 1: Physical match
    pts, lines = _score_physical_match(comp_bedrooms, comp_sleeps, subject_bedrooms, subject_guests)
    category_scores["physical"] = pts
    breakdown.extend(lines)

    # Category 2: Financial match
    pts, lines = _score_financial_match(
        comp_adr, comp_occ, comp_revpar,
        comp_revenue_potential, comp_annual_revenue,
        subject_adr,
    )
    category_scores["financial"] = pts
    breakdown.extend(lines)

    # Category 3: Quality match
    pts, lines = _score_quality_match(comp_rating, comp_reviews, comp_occ, subject_signals, comp)
    category_scores["quality"] = pts
    breakdown.extend(lines)

    # Category 4: Amenity match
    pts, lines = _score_amenity_match(text, subject_signals, comp.get("amenities_raw"))
    # A premium feature the subject LACKS (hot tub, pool) lets the comp earn
    # what the subject cannot. The caller sets extra_features only when too
    # few comps without it existed to drop these (comp_filters.select_comp_pool),
    # so this is what keeps the ones without ranked first.
    for feature in comp.get("extra_features") or []:
        pts -= 3
        lines.append(f"-3 Has a {feature.replace('_', ' ')} the subject lacks")
    category_scores["amenity"] = pts
    breakdown.extend(lines)

    # Category 5: Data reliability
    pts, lines = _score_data_reliability(comp, comp_booked)
    category_scores["reliability"] = pts
    breakdown.extend(lines)

    # Category 6: Proximity — the strongest comparability signal there is,
    # and previously absent entirely.
    pts, lines = _score_distance(comp, subject or {})
    category_scores["distance"] = pts
    breakdown.extend(lines)

    # ── Category minimum thresholds ────────────────────────────────────────

    total_score = sum(category_scores.values())

    if category_scores.get("financial", 0) <= -2:
        total_score -= 3
        breakdown.append("-3 PENALTY: Weak financial profile across multiple metrics")

    if category_scores.get("quality", 0) <= -2:
        total_score -= 3
        breakdown.append("-3 PENALTY: Weak quality profile (low rating + few reviews + low occupancy)")

    if category_scores.get("reliability", 0) <= -3:
        total_score -= 2
        breakdown.append("-2 PENALTY: Unreliable data (missing financials + part-time listing)")

    comp["score"] = total_score
    comp["category_scores"] = category_scores
    comp["score_breakdown"] = breakdown
    comp["hard_fail"] = False
    comp["hard_fail_reason"] = ""
    # adr_raw is left as AirROI's ttm_avg_rate. It used to be written back
    # here, which was harmless while comp_adr WAS that field; comp_adr is now
    # the rate paid and must not overwrite the raw value.

    return comp


# ── Parsing helpers ───────────────────────────────────────────────────────

def _parse_adr(adr_str) -> float:
    """Parse 'CA$929' or 929 or '929' into float."""
    if isinstance(adr_str, (int, float)):
        return float(adr_str)
    if not adr_str:
        return 0.0
    cleaned = str(adr_str).replace("CA$", "").replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_currency(val) -> Optional[float]:
    """Parse 'CA$54.2K' or 'CA$54,200' or 54200 into float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = str(val).replace("CA$", "").replace("$", "").replace(",", "").strip()
    if cleaned.upper().endswith("K"):
        try:
            return float(cleaned[:-1]) * 1000
        except ValueError:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(val) -> Optional[int]:
    """Parse various formats into int, or None."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_float(val) -> Optional[float]:
    """Parse various formats into float, or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_pct(val) -> Optional[float]:
    """Parse '76%' or 76 into a 0-100 float.

    Does NOT guess units. The adapter normalises occupancy to 0-100 before it
    reaches here; the old `v if v > 1 else v * 100` heuristic turned a real
    1.0% occupancy into 100% and scored it "+3 proven performer".
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ── Main ranking function ──────────────────────────────────────────────────

def rank_comps(subject: dict, comps: list, top_n: int = 6) -> dict:
    """
    Score all comps, filter hard fails, return top_n by score.
    """
    subject_signals = detect_subject_signals(subject)
    if subject.get("latitude") is None and subject.get("lat") is not None:
        subject["latitude"] = subject.get("lat")
    if subject.get("longitude") is None and subject.get("lng") is not None:
        subject["longitude"] = subject.get("lng")
    subject_adr = _parse_adr(subject.get("adr") or subject.get("airdna_adr", 0))
    subject_bedrooms = _parse_int(subject.get("bedrooms"))
    subject_guests = _parse_int(subject.get("max_guests") or subject.get("guests"))

    quality_tier = subject_signals.get("quality_tier", "unknown")
    pos_sent = subject_signals.get("review_sentiment_positive", 0)
    neg_sent = subject_signals.get("review_sentiment_negative", 0)

    print(f"[scorer] Subject signals: {[k for k, v in subject_signals.items() if v and v is not True or v is True]}", file=sys.stderr)
    print(f"[scorer] Subject ADR: CA${subject_adr:.0f}", file=sys.stderr)
    print(f"[scorer] Subject config: {subject_bedrooms}BR / sleeps {subject_guests}", file=sys.stderr)
    print(f"[scorer] Subject quality tier: {quality_tier} (sentiment: +{pos_sent}/-{neg_sent})", file=sys.stderr)
    print(f"[scorer] Subject superhost: {subject_signals.get('superhost', False)}", file=sys.stderr)
    print(f"[scorer] Scoring {len(comps)} candidates across 5 categories...", file=sys.stderr)

    scored = [
        score_comp(c, subject_signals, subject_adr, subject_bedrooms,
                   subject_guests, subject)
        for c in comps
    ]

    hard_fails = [c for c in scored if c["hard_fail"]]
    passing    = [c for c in scored if not c["hard_fail"]]

    # Deterministic ordering. Score ties were previously resolved by input
    # order, so 18 of 20 input shuffles changed the delivered comp set.
    # Tie-break: closer rate paid to subject, then closer distance, then more
    # reviews. The rate paid, not adr_raw (ttm_avg_rate, off by up to 19%).
    def _tiebreak(c):
        adr_gap = abs((c.get("nightly_rate") or 0) - (subject_adr or 0)) / max(subject_adr or 1, 1)
        dist = c.get("distance_km")
        return (
            -c["score"],
            round(adr_gap, 4),
            dist if dist is not None else 9999.0,
            -(c.get("reviews") or 0),
            str(c.get("name") or ""),
        )
    passing.sort(key=_tiebreak)

    selected = passing[:top_n]

    # Log scoring for transparency
    for i, c in enumerate(passing[:top_n + 3]):
        flag = "SELECTED" if i < top_n else "not selected"
        cats = c.get("category_scores", {})
        print(
            f"[scorer] [{flag}] {c.get('name', 'Unknown')[:40]} "
            f"total={c['score']} "
            f"[phys={cats.get('physical', 0)} fin={cats.get('financial', 0)} "
            f"qual={cats.get('quality', 0)} amen={cats.get('amenity', 0)} "
            f"reli={cats.get('reliability', 0)} dist={cats.get('distance', 0)}]",
            file=sys.stderr,
        )

    for c in hard_fails:
        print(f"[scorer] [DISQUALIFIED] {c.get('name', 'Unknown')[:40]} — {c['hard_fail_reason']}", file=sys.stderr)

    # Compute averages from selected comps
    averages = {}
    if selected:
        def _avg(key, parse_fn=float):
            vals = []
            for c in selected:
                v = c.get(key)
                if v is not None:
                    try:
                        vals.append(parse_fn(v))
                    except (ValueError, TypeError):
                        pass
            return round(sum(vals) / len(vals), 2) if vals else None

        averages = {
            "nights_booked":     _avg("nights_booked"),
            "nightly_rate":      _avg("nightly_rate"),   # rate paid, not ttm_avg_rate
            "occupancy_pct":     _avg("occupancy_pct", lambda x: float(str(x).replace("%", ""))),
            "revenue_potential": _avg("revenue_potential_raw", float),
            "annual_revenue":    _avg("annual_revenue_raw", float),
        }

    scores = [c["score"] for c in passing] if passing else [0]
    score_range = {"min": min(scores), "max": max(scores)}

    return {
        "subject_signals":   subject_signals,
        "subject_adr":       subject_adr,
        "subject_bedrooms":  subject_bedrooms,
        "subject_guests":    subject_guests,
        "total_candidates":  len(comps),
        "hard_fails":        hard_fails,
        "ranked":            passing,
        "selected":          selected,
        "averages":          averages,
        "score_range":       score_range,
        "needs_more_comps":  len(selected) < top_n,
    }


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="STR Comp Scorer")
    parser.add_argument("--subject", required=True, help="Subject property JSON string")
    parser.add_argument("--comps",   required=True, help="JSON array of comp candidates")
    parser.add_argument("--top",     type=int, default=6, help="Number of comps to select")
    args = parser.parse_args()

    try:
        subject = json.loads(args.subject)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid subject JSON: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        comps = json.loads(args.comps)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid comps JSON: {e}", file=sys.stderr)
        sys.exit(1)

    result = rank_comps(subject, comps, top_n=args.top)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
