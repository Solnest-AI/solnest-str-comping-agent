"""Compare a comp with the subject on everything AirROI reports about both.

Added 2026-09-26. Before this the scorer compared bedrooms, guests, rate,
quality, distance and a dozen amenity signals, and only ever REWARDED a comp
for sharing one of the subject's features. A comp with a feature the subject
lacks lost nothing (outside the pool/hot-tub filter), bathrooms and property
type were never looked at, and ~130 of the ~143 amenity strings AirROI
reports were ignored. A detached log cabin in Sun Peaks (no ski access) was
comped against six ski-in/ski-out townhomes.

What is compared, and where each weight comes from:

* Premium features (PREMIUM) in BOTH directions: a comp is marked down for
  having one the subject lacks and for lacking one the subject has. Points
  are the scorer's existing per-feature values (_AMENITY_POINTS), which are
  judgment, not measurement: within-pool revenue lift on 375 listings could
  not separate a hot tub from "Hangers" (hosts who tick every box also run
  better listings), so no measured weight is claimed for them.
* Every other amenity by weighted overlap, each amenity weighted by rarity,
  -ln(share of listings that have it), from data/amenity_prevalence.json.
  Wifi (99.7% of listings) is worth almost nothing; a sauna (1.8%) a lot.
* Property type: same type, same kind (detached vs attached), or different.
* Bathrooms, and minimum stay (an 11-night minimum is a different business).
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Optional

import comp_filters

PREVALENCE_PATH = Path(__file__).parent / "data" / "amenity_prevalence.json"

# key -> (points, phrase, exact AirROI amenity strings, comp_filters text feature)
# Keys match comp_scorer's subject signal names so a subject_signals dict
# drives both. The text feature (negation-guarded, billiards-trapped) is used
# only where comp_filters supports one.
PREMIUM: dict[str, tuple[int, str, tuple[str, ...], Optional[str]]] = {
    "ski_in_out": (3, "ski-in/ski-out access", ("Ski-in/Ski-out",), "ski_in_out"),
    "pool":       (2, "a pool", ("Pool",), "pool"),
    "hot_tub":    (2, "a hot tub", ("Hot tub",), "hot_tub"),
    "water":      (2, "waterfront or beach/lake access",
                   ("Waterfront", "Beach access", "Lake access"), None),
    "sauna":      (2, "a sauna", ("Sauna",), "sauna"),
    "games_room": (1, "a games room",
                   ("Pool table", "Game console", "Arcade games", "Life size games",
                    "Ping pong table"), None),
    "gym":        (1, "a gym", ("Gym", "Exercise equipment"), None),
    "fireplace":  (1, "an indoor fireplace", ("Indoor fireplace",), None),
    "ev_charger": (1, "an EV charger", ("EV charger",), "ev_charger"),
    "pets":       (1, "a pets-allowed policy", ("Pets allowed",), None),
}
PREMIUM_VOCAB: frozenset[str] = frozenset(a for _, _, vocab, _ in PREMIUM.values() for a in vocab)
# Features whose description mention is enough to count as HAVING one. A
# sauna is on 1.8% of AirROI amenity lists, so hosts mostly only say it in
# the text; it was always scored from text and still is.
TEXT_IS_ENOUGH: frozenset[str] = frozenset({"sauna"})


# ── Premium features, both directions ─────────────────────────────────────

def comp_premium(comp: dict) -> tuple[set[str], set[str]]:
    """(has, mentions) for a comp. `has` is the authoritative amenity list;
    `mentions` adds negation-guarded description text ("Outdoor pool" in the
    blurb with no "Pool" amenity). A comp is credited with a match only on
    `has`, and treated as carrying a feature the subject lacks on `mentions`,
    the same split comp_filters uses for required vs lacking features."""
    amenities = set(comp.get("amenities_raw") or comp.get("amenities") or [])
    has, mentions = set(), set()
    for key, (_, _, vocab, text_feature) in PREMIUM.items():
        if amenities & set(vocab):
            has.add(key)
            mentions.add(key)
        elif text_feature and comp_filters.comp_mentions_feature(comp, text_feature):
            mentions.add(key)
            if key in TEXT_IS_ENOUGH:
                has.add(key)
    return has, mentions


def score_premium(subject_signals: dict, comp_has: set[str], comp_mentions: set[str],
                  subject_readable: bool = True) -> tuple[int, list[str]]:
    """Symmetric premium-feature comparison. When the subject's features could
    not be read, absence on the subject is unknown, not "lacks", so only
    matches are scored."""
    pts, lines = 0, []
    for key, (points, phrase, _, _) in PREMIUM.items():
        subject_has = bool(subject_signals.get(key))
        if subject_has and key in comp_has:
            pts += points
            lines.append(f"+{points} Both have {phrase}")
        elif subject_has and key not in comp_mentions:
            pts -= points
            lines.append(f"-{points} Lacks {phrase}, which the subject has")
        elif not subject_has and subject_readable and key in comp_mentions:
            pts -= points
            lines.append(f"-{points} Has {phrase}, which the subject lacks")
    return pts, lines


# ── Every other amenity, weighted by rarity ───────────────────────────────

@lru_cache(maxsize=1)
def _prevalence() -> tuple[dict[str, float], int]:
    try:
        table = json.loads(PREVALENCE_PATH.read_text(encoding="utf-8"))
        return dict(table["prevalence"]), int(table["n_listings"])
    except (OSError, ValueError, KeyError):
        return {}, 0


def amenity_weight(amenity: str) -> float:
    """-ln(prevalence), capped at 3. Unseen strings are treated as rare."""
    table, n = _prevalence()
    floor = 1.0 / max(n, 20)
    share = table.get(amenity, floor)
    return min(3.0, -math.log(max(share, floor)))


def amenity_overlap(subject_amenities, comp_amenities) -> Optional[float]:
    """Rarity-weighted Jaccard overlap of the non-premium amenity lists,
    0 (nothing shared) to 1 (identical). None when either list is empty,
    e.g. an address subject whose features came from a portal scrape."""
    s = set(subject_amenities or []) - PREMIUM_VOCAB
    c = set(comp_amenities or []) - PREMIUM_VOCAB
    if not s or not c:
        return None
    # Summed in sorted order: set order changes with the hash seed, and float
    # addition in a different order can move the last bit, so the same pair
    # would otherwise score 1.0 on one run and 1.0000000000000002 on another.
    union = sum(amenity_weight(a) for a in sorted(s | c))
    return sum(amenity_weight(a) for a in sorted(s & c)) / union if union else None


def score_overlap(overlap: Optional[float], pool_median: Optional[float]) -> tuple[int, list[str]]:
    """Points relative to the rest of the pool, so the scale is the same in a
    market where every listing ticks 60 boxes and one where they tick 20."""
    if overlap is None or pool_median is None:
        return 0, []
    pts = max(-3, min(3, round(8 * (overlap - pool_median))))
    if pts > 0:
        return pts, [f"+{pts} Amenity list closer to the subject's than most "
                     f"({overlap:.0%} weighted overlap, pool median {pool_median:.0%})"]
    if pts < 0:
        return pts, [f"{pts} Amenity list further from the subject's than most "
                     f"({overlap:.0%} weighted overlap, pool median {pool_median:.0%})"]
    return 0, []


# ── Targeted search: when AirROI's comparables are the wrong kind ─────────

# comp_filters feature -> AirROI search amenity id (snake_case of the display
# string; every one of these appears in the published OpenAPI spec).
SEARCH_AMENITY_IDS = {"pool": "pool", "hot_tub": "hot_tub", "ski_in_out": "ski_in_ski_out",
                      "sauna": "sauna", "ev_charger": "ev_charger"}
TARGETED_RADIUS_MILES = 10


def required_shortfall(pool: list, required, minimum: int = 10) -> list[str]:
    """Required features (the subject HAS them) that fewer than `minimum` comps
    in the pool have, judged the way the required filter judges them (the
    authoritative amenity list). `minimum` matches comp_filters'
    min_without_extras, the same bar the lacking direction uses.

    Miami, 2026-09-26: 4 of AirROI's 25 comparables had a hot tub, only 3
    survived filtering, and the filters relaxed, dropping the hot-tub
    requirement. The targeted search only ran because pool was also short;
    this is the reason that should have fired on its own."""
    canon = [comp_filters.normalize_feature(f) for f in required]
    return [f for f in canon
            if sum(1 for c in pool if comp_filters.comp_has_feature(c, f)) < minimum]


def targeted_search_reasons(lacking_relaxed, kept: list, subject_type: Optional[str],
                            min_comps: int = 6, required_short=()) -> tuple[list[str], bool]:
    """Why AirROI's own comparables are not enough, and whether property type
    is one of the reasons. Sunburst (Sun Peaks, 2026-09-26): 24 of AirROI's
    25 comparables were ski-in/ski-out for a cabin with no ski access, so no
    amount of ranking could produce a like-for-like set."""
    required_short = [comp_filters.normalize_feature(f) for f in required_short]
    lacking_relaxed = [comp_filters.normalize_feature(f) for f in lacking_relaxed]
    reasons = [f"too few comparables with {PREMIUM.get(f, (0, f.replace('_', ' ')))[1]}"
               for f in required_short]
    reasons += [f"too few comparables without {PREMIUM.get(f, (0, f.replace('_', ' ')))[1]}"
                for f in lacking_relaxed]
    by_type = False
    s_class = property_class(subject_type)
    if s_class:
        same = sum(1 for c in kept if property_class(
            (c.get("listing_info") or {}).get("listing_type") or c.get("listing_type")) == s_class)
        if same < min_comps:
            by_type = True
            reasons.append(f"only {same} comparables are {s_class} like the subject")
    return reasons, by_type


def targeted_search_filter(*, bedrooms: int, bed_tolerance: int, required=(), lacking=(),
                           listing_type: Optional[str] = None) -> dict:
    """The /listings/search/radius filter for listings like the subject:
    same size band, its must-have features, none of the ones it lacks, and
    actually operating (the scorer's own 20% occupancy and 3-review floors).
    No revenue filter and no revenue sort: picking by earnings would bias the
    comp set upward.

    `required` / `lacking` may be raw --require spellings ("hot tub", "Pool");
    they are normalized here, since an unrecognised key used to be dropped
    from the search without a word. A studio (0 bedrooms) searches from 0."""
    required = [comp_filters.normalize_feature(f) for f in required]
    lacking = [comp_filters.normalize_feature(f) for f in lacking]
    floor = 0 if bedrooms == 0 else 1
    flt: dict = {
        "room_type": {"eq": "entire_home"},
        "bedrooms": {"range": [max(floor, bedrooms - bed_tolerance), bedrooms + bed_tolerance]},
        "ttm_adjusted_occupancy": {"gte": 0.2},
        "num_reviews": {"gte": 3},
    }
    amen: dict = {}
    must = [SEARCH_AMENITY_IDS[f] for f in required if f in SEARCH_AMENITY_IDS]
    never = [SEARCH_AMENITY_IDS[f] for f in lacking if f in SEARCH_AMENITY_IDS]
    if must:
        amen["all"] = must
    if never:
        amen["none"] = never
    if amen:
        flt["amenities"] = amen
    if listing_type:
        flt["listing_type"] = {"eq": listing_type}
    return flt


# ── Property type, bathrooms, minimum stay ────────────────────────────────

_ROOM = ("private room", "shared room", "room in")
_HOTEL = ("hotel", "hostel", "aparthotel")
# Checked before _DETACHED: "townhouse" contains "house".
_ATTACHED = ("condo", "townhouse", "town house", "loft", "rental unit", "apartment",
             "guest suite", "serviced", "duplex", "triplex", "multi-family", "flat")
_DETACHED = ("home", "house", "cabin", "chalet", "cottage", "villa", "bungalow",
             "lodge", "farm stay", "barn", "dome", "treehouse", "yurt", "tiny home",
             "single family", "single-family", "detached", "castle", "mobile",
             "manufactured")


def property_class(listing_type: Optional[str]) -> Optional[str]:
    t = (listing_type or "").lower().strip()
    if not t or t == "property":
        return None
    for label, words in (("room", _ROOM), ("hotel", _HOTEL),
                         ("attached", _ATTACHED), ("detached", _DETACHED)):
        if any(w in t for w in words):
            return label
    return None


def _type_name(listing_type: str) -> str:
    t = listing_type.strip()
    return t[len("Entire "):] if t.lower().startswith("entire ") else t


def score_property_type(subject_type: Optional[str], comp_type: Optional[str]) -> tuple[int, list[str]]:
    s_class, c_class = property_class(subject_type), property_class(comp_type)
    if not s_class or not c_class:
        return 0, []
    s_name, c_name = _type_name(subject_type).lower(), _type_name(comp_type).lower()
    if s_name == c_name:
        return 2, [f"+2 Same property type ({c_name})"]
    if s_class == c_class:
        return 1, [f"+1 Same kind of property ({c_name} vs {s_name}, both {s_class})"]
    return -3, [f"-3 Different kind of property: {c_name} ({c_class}) vs the "
                f"subject's {s_name} ({s_class})"]


def score_bathrooms(subject_baths, comp_baths) -> tuple[int, list[str]]:
    try:
        s, c = float(subject_baths), float(comp_baths)
    except (TypeError, ValueError):
        return 0, []
    if s <= 0 or c <= 0:
        return 0, []
    diff = abs(s - c)
    if diff <= 0.5:
        return 1, [f"+1 Bathrooms match ({c:g} vs {s:g})"]
    if diff >= 1.5:
        return -1, [f"-1 Bathrooms differ ({c:g} vs {s:g})"]
    return 0, []


def score_min_nights(subject_min, comp_min) -> tuple[int, list[str]]:
    """A long minimum stay is a different booking pattern: one Sun Peaks comp
    required 11 nights. Compared with the subject's own minimum when known,
    otherwise only a monthly-style minimum (14+) is marked down."""
    try:
        c = int(comp_min)
    except (TypeError, ValueError):
        return 0, []
    try:
        s = int(subject_min)
    except (TypeError, ValueError):
        s = 0
    if s > 0:
        if c >= max(7, 3 * s):
            return -2, [f"-2 Long minimum stay ({c} nights vs the subject's {s}), "
                        "a different booking pattern"]
        return 0, []
    if c >= 14:
        return -2, [f"-2 Long minimum stay ({c} nights), a monthly-style rental"]
    return 0, []
