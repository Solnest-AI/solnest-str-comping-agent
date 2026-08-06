#!/usr/bin/env python3
"""
STR Comping Agent — Comp Scorer v2
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
import sys
from typing import Optional


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

# ── Must-match features ──────────────────────────────────────────────────
# These are high-impact property attributes where subject and comp MUST
# match. Mismatches in either direction get a heavy penalty; matches get
# a strong bonus. This prevents e.g. oceanfront comps inflating projections
# for an inland property, or a non-pool property being comped against pools.

MUST_MATCH_FEATURES = {
    "waterfront": [
        # Truly ON the water — not just nearby
        "waterfront", "oceanfront", "beachfront", "ocean front", "beach front",
        "gulf front", "on the beach", "direct beach", "direct ocean",
        "on the ocean", "on the water", "on the gulf",
        "ocean view", "sea view", "gulf view",
        "canal front", "canal-front", "canalfront", "on the canal",
        "boat dock", "private dock", "deep water", "deep-water",
        "bayfront", "bay front", "on the bay", "harbourfront", "harborfront",
        "riverfront", "river front", "on the river",
    ],
    "lakefront": [
        "lakefront", "lake front", "on the lake", "lakeshore",
        "lake view", "lakeside", "lake house", "lakehouse",
    ],
    "pool": [
        "pool", "swimming pool", "indoor pool", "outdoor pool",
        "private pool", "heated pool", "plunge pool", "splash pool",
    ],
    "hot_tub": [
        "hot tub", "hottub", "hot-tub", "jacuzzi", "jetted tub",
    ],
    "ski_access": [
        "ski-in", "ski in", "ski out", "ski-out", "ski access", "ski-access",
        "slope side", "slopeside", "ski trail",
    ],
}

# Points awarded/penalized for must-match features
MUST_MATCH_BONUS = 8    # both have the feature
MUST_MATCH_PENALTY = -10  # one has it, the other doesn't


LUXURY_KEYWORDS = [
    "luxurious", "luxury", "premium", "high-end", "upscale",
    "designer", "executive", "boutique", "elite", "grand",
]

# ── Property type detection ──────────────────────────────────────────────
# Standalone homes and attached units compete in different segments.
# A 5BR chalet shouldn't be comped against a 4BR townhome.

STANDALONE_KEYWORDS = [
    "house", "home", "chalet", "cabin", "cottage", "villa", "bungalow",
    "estate", "lodge", "retreat", "single family", "single-family",
    "detached", "stand-alone", "standalone",
]

ATTACHED_KEYWORDS = [
    "condo", "condominium", "townhouse", "townhome", "town house",
    "apartment", "apt", "suite", "unit", "penthouse", "loft",
    "duplex", "triplex",
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

    # Property type: standalone vs attached
    is_standalone = any(kw in searchable for kw in STANDALONE_KEYWORDS)
    is_attached = any(kw in searchable for kw in ATTACHED_KEYWORDS)
    # If both match (e.g., "townhouse" + "home"), attached wins (more specific)
    if is_attached:
        signals["property_type_standalone"] = False
    elif is_standalone:
        signals["property_type_standalone"] = True
    else:
        signals["property_type_standalone"] = None  # unknown — no penalty

    # Professional management flag
    signals["professional_mgmt"] = any(kw in searchable for kw in PROFESSIONAL_MGMT_KEYWORDS)

    # Amenity signals
    for signal, keywords in AMENITY_SIGNALS.items():
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

    # Must-match feature detection
    for feature, keywords in MUST_MATCH_FEATURES.items():
        hits = sum(1 for kw in keywords if kw in searchable)
        if feature in ("waterfront", "lakefront"):
            # Location features need 2+ keyword hits to activate — a single
            # match like "Beachfront Village" (community name) or "lake access"
            # isn't enough to classify the property as truly on the water.
            signals[f"must_match_{feature}"] = hits >= 2
        else:
            signals[f"must_match_{feature}"] = hits >= 1

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
    comp_days: Optional[int],
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
            breakdown.append(f"+3 ADR within 10% of subject (${comp_adr:.0f})")
        elif adr_diff_pct <= 0.20:
            score += 2
            breakdown.append(f"+2 ADR within 20% of subject (${comp_adr:.0f})")
        elif adr_diff_pct <= 0.35:
            score += 1
            breakdown.append(f"+1 ADR within 35% of subject (${comp_adr:.0f})")

    # RevPAN — Revenue Per Available Night
    if comp_annual_revenue and comp_days and comp_days > 0:
        revpan = comp_annual_revenue / comp_days
        if subject_adr:
            revpan_ratio = revpan / subject_adr
            if revpan_ratio >= 0.55:
                score += 3
                breakdown.append(f"+3 Strong RevPAN (${revpan:.0f}/night — high yield)")
            elif revpan_ratio >= 0.40:
                score += 2
                breakdown.append(f"+2 Solid RevPAN (${revpan:.0f}/night)")
            elif revpan_ratio >= 0.25:
                score += 1
                breakdown.append(f"+1 Moderate RevPAN (${revpan:.0f}/night)")
            elif revpan_ratio < 0.15:
                score -= 1
                breakdown.append(f"-1 Weak RevPAN (${revpan:.0f}/night — low yield)")

    # Revenue efficiency — actual vs potential
    if comp_revenue_potential and comp_annual_revenue and comp_revenue_potential > 0:
        efficiency = comp_annual_revenue / comp_revenue_potential
        if efficiency >= 0.85:
            score += 3
            breakdown.append(f"+3 High revenue efficiency ({efficiency:.0%} of potential — well managed)")
        elif efficiency >= 0.70:
            score += 2
            breakdown.append(f"+2 Good revenue efficiency ({efficiency:.0%} of potential)")
        elif efficiency >= 0.50:
            score += 1
            breakdown.append(f"+1 Moderate efficiency ({efficiency:.0%} of potential)")
        elif efficiency < 0.35:
            score -= 1
            breakdown.append(f"-1 Low efficiency ({efficiency:.0%} of potential — underperforming)")

    return score, breakdown


def _score_quality_match(
    comp_rating: float,
    comp_reviews: int,
    comp_occ: Optional[float],
    subject_signals: dict,
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

    # Rating quality
    if comp_rating >= 4.9 and comp_reviews >= 10:
        score += 3
        breakdown.append(f"+3 Elite rating ({comp_rating} with {comp_reviews} reviews)")
    elif comp_rating >= 4.8 and comp_reviews >= 10:
        score += 2
        breakdown.append(f"+2 Excellent rating ({comp_rating})")
    elif comp_rating >= 4.7:
        score += 1
        breakdown.append(f"+1 Good rating ({comp_rating})")
    elif comp_rating > 0 and comp_rating < 4.3:
        score -= 2
        breakdown.append(f"-2 Low rating ({comp_rating}) — likely underperformer")

    # Subject quality tier matching
    quality_tier = subject_signals.get("quality_tier", "unknown")
    if quality_tier == "elite" and comp_rating >= 4.9 and comp_reviews >= 20:
        score += 2
        breakdown.append("+2 Quality tier match (elite subject, elite comp)")
    elif quality_tier == "premium" and comp_rating >= 4.8:
        score += 1
        breakdown.append("+1 Quality tier match (premium subject, high-rated comp)")
    elif quality_tier == "underperformer" and comp_rating < 4.5:
        score += 1
        breakdown.append("+1 Quality tier match (both lower-performing)")

    # Superhost preference
    if subject_signals.get("superhost"):
        if comp_rating >= 4.8 and comp_reviews >= 20:
            score += 1
            breakdown.append("+1 Superhost-caliber comp (subject is Superhost)")

    # Occupancy performance
    if comp_occ:
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

    # Professional management signal matching
    if subject_signals.get("professional_mgmt"):
        if comp_occ and comp_occ >= 55 and comp_rating >= 4.7 and comp_reviews >= 20:
            score += 1
            breakdown.append("+1 Likely professionally managed (high metrics)")

    return score, breakdown


def _score_amenity_match(text: str, subject_signals: dict) -> tuple:
    """Score amenity/feature alignment. Returns (points, breakdown_lines)."""
    score = 0
    breakdown = []

    amenity_score_map = {
        "hot_tub":    ("Hot Tub match", 2),
        "pool":       ("Pool match", 2),
        "ski_in_out": ("Ski-in/out match", 3),
        "sauna":      ("Sauna match", 2),
        "games_room": ("Games room match", 1),
        "views":      ("Views match", 1),
        "village":    ("Village/central match", 1),
        "wellness":   ("Wellness match", 1),
        "gym":        ("Gym match", 1),
        "fireplace":  ("Fireplace match", 1),
    }

    for signal, (label, points) in amenity_score_map.items():
        if subject_signals.get(signal):
            keywords = AMENITY_SIGNALS.get(signal, [])
            if any(kw in text for kw in keywords):
                score += points
                breakdown.append(f"+{points} {label}")

    # Luxury match bonus
    if subject_signals.get("luxury"):
        if any(kw in text for kw in LUXURY_KEYWORDS):
            score += 3
            breakdown.append("+3 Luxury descriptor match")

    return score, breakdown


def _score_data_reliability(comp: dict, comp_days: Optional[int]) -> tuple:
    """Score data completeness and listing availability. Returns (points, breakdown_lines)."""
    score = 0
    breakdown = []

    # Data completeness
    financial_fields = ["revenue_potential", "annual_revenue", "occupancy_pct", "adr", "days_available"]
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

    # Days available — full-time vs part-time rental
    if comp_days:
        if comp_days >= 300:
            score += 2
            breakdown.append(f"+2 Full-time rental ({comp_days} days available)")
        elif comp_days >= 200:
            score += 1
            breakdown.append(f"+1 Near full-time rental ({comp_days} days)")
        elif comp_days < 120:
            score -= 2
            breakdown.append(f"-2 Part-time rental ({comp_days} days) — not comparable to full-time")

    return score, breakdown


def _score_must_match(text: str, subject_signals: dict) -> tuple:
    """Score must-match feature alignment. Returns (points, breakdown_lines).

    Both subject and comp are scanned for high-impact features (waterfront,
    pool, hot tub, etc.). Matches get a strong bonus; mismatches in EITHER
    direction get a heavy penalty.
    """
    score = 0
    breakdown = []

    feature_labels = {
        "waterfront": "Waterfront/Ocean",
        "lakefront": "Lakefront",
        "pool": "Pool",
        "hot_tub": "Hot Tub",
        "ski_access": "Ski-in/out",
    }

    for feature, keywords in MUST_MATCH_FEATURES.items():
        subject_has = subject_signals.get(f"must_match_{feature}", False)
        comp_has = any(kw in text for kw in keywords)
        label = feature_labels.get(feature, feature)

        if subject_has and comp_has:
            score += MUST_MATCH_BONUS
            breakdown.append(f"+{MUST_MATCH_BONUS} {label} match (both have it)")
        elif subject_has and not comp_has:
            score += MUST_MATCH_PENALTY
            breakdown.append(f"{MUST_MATCH_PENALTY} {label} mismatch (subject has, comp missing)")
        elif not subject_has and comp_has:
            score += MUST_MATCH_PENALTY
            breakdown.append(f"{MUST_MATCH_PENALTY} {label} mismatch (comp has, subject missing)")
        # Both missing → 0, no log

    # Property type match: standalone home vs condo/townhouse
    subject_standalone = subject_signals.get("property_type_standalone")
    if subject_standalone is not None:
        comp_is_standalone = any(kw in text for kw in STANDALONE_KEYWORDS)
        comp_is_attached = any(kw in text for kw in ATTACHED_KEYWORDS)
        # Determine comp type (attached is more specific, wins if both match)
        if comp_is_attached:
            comp_standalone = False
        elif comp_is_standalone:
            comp_standalone = True
        else:
            comp_standalone = None

        if comp_standalone is not None:
            if subject_standalone == comp_standalone:
                score += 6
                type_label = "standalone home" if subject_standalone else "condo/townhome"
                breakdown.append(f"+6 Property type match (both {type_label})")
            else:
                score -= 8
                subj_label = "standalone home" if subject_standalone else "condo/townhome"
                comp_label = "condo/townhome" if subject_standalone else "standalone home"
                breakdown.append(f"-8 Property type mismatch (subject is {subj_label}, comp is {comp_label})")

    return score, breakdown


# ── Main scoring function ──────────────────────────────────────────────────

def score_comp(
    comp: dict,
    subject_signals: dict,
    subject_adr: Optional[float],
    subject_bedrooms: Optional[int],
    subject_guests: Optional[int],
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
    comp_adr = comp.get("adr_raw", 0) or _parse_adr(comp.get("adr", "0"))
    comp_bedrooms = _parse_int(comp.get("bedrooms"))
    comp_sleeps = _parse_int(comp.get("sleeps")) or _parse_int(comp.get("max_guests"))
    comp_occ = _parse_pct(comp.get("occupancy_pct"))
    comp_reviews = _parse_int(comp.get("reviews")) or _parse_int(comp.get("review_count")) or 0
    comp_rating = _parse_float(comp.get("rating")) or 0.0
    comp_days = _parse_int(comp.get("days_available"))
    comp_revenue_potential = _parse_currency(comp.get("revenue_potential"))
    comp_annual_revenue = _parse_currency(comp.get("annual_revenue"))

    # ── Hard disqualifiers ─────────────────────────────────────────────────

    # 1. Luxury subject → comp must be within range of subject ADR.
    #    Scale the floor with ADR tier: ultra-luxury ($1500+) properties
    #    have thinner comp pools, so the floor is more lenient.
    if subject_signals.get("luxury") and subject_adr and comp_adr:
        if subject_adr >= 1500:
            luxury_adr_floor = subject_adr * 0.35
        elif subject_adr >= 800:
            luxury_adr_floor = subject_adr * 0.45
        else:
            luxury_adr_floor = subject_adr * 0.55
        if comp_adr < luxury_adr_floor:
            hard_fail = True
            hard_fail_reason = (
                f"Luxury subject (ADR ${subject_adr:.0f}) — "
                f"comp ADR ${comp_adr:.0f} is below luxury floor ${luxury_adr_floor:.0f}"
            )

    # 2. Bedroom count mismatch — tight tolerance for credible comps
    if subject_bedrooms and comp_bedrooms:
        if subject_bedrooms <= 7:
            max_bed_diff = 1  # ±1 for 1-7BR — comps must be close
        else:
            max_bed_diff = 2  # 8+ BR properties: allow ±2 (thin markets)
        bed_diff = abs(comp_bedrooms - subject_bedrooms)
        if bed_diff > max_bed_diff:
            hard_fail = True
            hard_fail_reason = (
                f"Bedroom mismatch: subject has {subject_bedrooms}BR, "
                f"comp has {comp_bedrooms}BR (max ±{max_bed_diff} allowed)"
            )

    # 3. Guest capacity mismatch — scale tolerance with property size
    if subject_guests and comp_sleeps:
        if subject_guests <= 6:
            max_guest_diff = 2
        elif subject_guests <= 10:
            max_guest_diff = 3
        elif subject_guests <= 16:
            max_guest_diff = 4
        else:
            max_guest_diff = 6  # 17+ guest properties: allow ±6
        guest_diff = abs(comp_sleeps - subject_guests)
        if guest_diff > max_guest_diff:
            hard_fail = True
            hard_fail_reason = (
                f"Guest capacity mismatch: subject sleeps {subject_guests}, "
                f"comp sleeps {comp_sleeps} (max ±{max_guest_diff} allowed)"
            )

    if hard_fail:
        comp["score"] = 0
        comp["category_scores"] = {}
        comp["score_breakdown"] = []
        comp["hard_fail"] = True
        comp["hard_fail_reason"] = hard_fail_reason
        comp["adr_raw"] = comp_adr
        return comp

    # ── Category scoring ───────────────────────────────────────────────────

    category_scores = {}

    # Category 1: Physical match
    pts, lines = _score_physical_match(comp_bedrooms, comp_sleeps, subject_bedrooms, subject_guests)
    category_scores["physical"] = pts
    breakdown.extend(lines)

    # Category 2: Financial match
    pts, lines = _score_financial_match(
        comp_adr, comp_occ, comp_days,
        comp_revenue_potential, comp_annual_revenue,
        subject_adr,
    )
    category_scores["financial"] = pts
    breakdown.extend(lines)

    # Category 3: Quality match
    pts, lines = _score_quality_match(comp_rating, comp_reviews, comp_occ, subject_signals)
    category_scores["quality"] = pts
    breakdown.extend(lines)

    # Category 4: Amenity match
    pts, lines = _score_amenity_match(text, subject_signals)
    category_scores["amenity"] = pts
    breakdown.extend(lines)

    # Category 5: Data reliability
    pts, lines = _score_data_reliability(comp, comp_days)
    category_scores["reliability"] = pts
    breakdown.extend(lines)

    # Category 6: Must-match features (waterfront, pool, hot tub, etc.)
    pts, lines = _score_must_match(text, subject_signals)
    category_scores["must_match"] = pts
    breakdown.extend(lines)

    # ── Category minimum thresholds ────────────────────────────────────────

    total_score = sum(category_scores.values())

    # Cross-category penalties. Tracked in category_scores under "penalty" so
    # sum(category_scores.values()) stays consistent with comp["score"].
    penalty = 0
    if category_scores.get("financial", 0) <= -2:
        penalty -= 3
        breakdown.append("-3 PENALTY: Weak financial profile across multiple metrics")

    if category_scores.get("quality", 0) <= -2:
        penalty -= 3
        breakdown.append("-3 PENALTY: Weak quality profile (low rating + few reviews + low occupancy)")

    if category_scores.get("reliability", 0) <= -3:
        penalty -= 2
        breakdown.append("-2 PENALTY: Unreliable data (missing financials + part-time listing)")

    if penalty:
        category_scores["penalty"] = penalty
    total_score += penalty

    comp["score"] = total_score
    comp["category_scores"] = category_scores
    comp["score_breakdown"] = breakdown
    comp["hard_fail"] = False
    comp["hard_fail_reason"] = ""
    comp["adr_raw"] = comp_adr

    return comp


# ── Parsing helpers ───────────────────────────────────────────────────────

def _parse_adr(adr_str) -> float:
    """Parse 'CA$929' or '$929' or 929 or '929' into float."""
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
    """Parse 'CA$54.2K' or '$54,200' or 54200 into float."""
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
    """Parse '76%' or 76 or 0.76 into float percentage."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v if v > 1 else v * 100
    try:
        cleaned = str(val).replace("%", "").replace(",", "").strip()
        v = float(cleaned)
        return v if v > 1 else v * 100
    except (ValueError, TypeError):
        return None


# ── Main ranking function ──────────────────────────────────────────────────

def rank_comps(subject: dict, comps: list, top_n: int = 6) -> dict:
    """
    Score all comps, filter hard fails, return top_n by score.
    """
    subject_signals = detect_subject_signals(subject)
    subject_adr = _parse_adr(subject.get("adr", 0))
    subject_bedrooms = _parse_int(subject.get("bedrooms"))
    subject_bathrooms = _parse_float(subject.get("bathrooms"))
    subject_guests = _parse_int(subject.get("max_guests") or subject.get("guests"))

    quality_tier = subject_signals.get("quality_tier", "unknown")
    pos_sent = subject_signals.get("review_sentiment_positive", 0)
    neg_sent = subject_signals.get("review_sentiment_negative", 0)

    print(f"[scorer] Subject signals: {[k for k, v in subject_signals.items() if v and v is not True or v is True]}", file=sys.stderr)
    print(f"[scorer] Subject ADR: ${subject_adr:.0f}", file=sys.stderr)
    print(f"[scorer] Subject config: {subject_bedrooms}BR / sleeps {subject_guests}", file=sys.stderr)
    print(f"[scorer] Subject quality tier: {quality_tier} (sentiment: +{pos_sent}/-{neg_sent})", file=sys.stderr)
    print(f"[scorer] Subject superhost: {subject_signals.get('superhost', False)}", file=sys.stderr)
    must_match_active = [k.replace("must_match_", "") for k, v in subject_signals.items() if k.startswith("must_match_") and v]
    if must_match_active:
        print(f"[scorer] Must-match features: {must_match_active}", file=sys.stderr)
    print(f"[scorer] Scoring {len(comps)} candidates across 6 categories...", file=sys.stderr)

    scored = [
        score_comp(c, subject_signals, subject_adr, subject_bedrooms, subject_guests)
        for c in comps
    ]

    hard_fails = [c for c in scored if c["hard_fail"]]
    passing    = [c for c in scored if not c["hard_fail"]]

    passing.sort(key=lambda c: c["score"], reverse=True)

    # ── Tiered selection: fill comp slots from tightest match first ────
    # Tier 1: Exact bed AND bath match
    # Tier 2: Exact bed match, bath within ±1
    # Tier 3: Bed ±1, any bath
    # Tier 4: Remaining passing comps (already within hard-fail tolerance)
    # Within each tier, comps are ranked by score (already sorted above).

    def _comp_bed_bath(c: dict) -> tuple:
        return (_parse_int(c.get("bedrooms")), _parse_float(c.get("bathrooms")))

    def _match_tier(c: dict) -> int:
        c_beds, c_baths = _comp_bed_bath(c)
        if c_beds is None or c_baths is None:
            return 4
        bed_diff = abs(c_beds - subject_bedrooms) if subject_bedrooms else 99
        bath_diff = abs(c_baths - subject_bathrooms) if subject_bathrooms else 99
        if bed_diff == 0 and bath_diff == 0:
            return 1
        if bed_diff == 0 and bath_diff <= 1:
            return 2
        if bed_diff <= 1:
            return 3
        return 4

    selected: list[dict] = []
    selected_ids: set[str] = set()

    tier_names = {1: "exact bed+bath", 2: "exact bed, bath ±1",
                  3: "bed ±1", 4: "wider"}

    for tier in (1, 2, 3, 4):
        if len(selected) >= top_n:
            break
        tier_comps = [c for c in passing
                      if _match_tier(c) == tier
                      and id(c) not in selected_ids]
        for c in tier_comps:
            if len(selected) >= top_n:
                break
            selected.append(c)
            selected_ids.add(id(c))

    # Log which tiers were used
    tier_counts = {}
    for c in selected:
        t = _match_tier(c)
        tier_counts[t] = tier_counts.get(t, 0) + 1
    tier_summary = ", ".join(f"{tier_names[t]}: {n}" for t, n in sorted(tier_counts.items()))
    if tier_summary:
        print(f"[scorer] Selection tiers: {tier_summary}", file=sys.stderr)

    # Log scoring for transparency
    for i, c in enumerate(passing[:max(len(selected) + 3, top_n + 3)]):
        flag = "SELECTED" if c in selected else "not selected"
        tier = _match_tier(c)
        cats = c.get("category_scores", {})
        print(
            f"[scorer] [{flag}] {c.get('name', 'Unknown')[:40]} "
            f"total={c['score']} tier={tier} "
            f"[phys={cats.get('physical', 0)} fin={cats.get('financial', 0)} "
            f"qual={cats.get('quality', 0)} amen={cats.get('amenity', 0)} "
            f"reli={cats.get('reliability', 0)} mm={cats.get('must_match', 0)}]",
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
            "adr":               _avg("adr_raw"),
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
    parser = argparse.ArgumentParser(description="Solnest Comp Scorer v2")
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
