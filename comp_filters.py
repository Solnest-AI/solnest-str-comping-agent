"""Comparable-pool filters — water proximity and must-have features.

Two drop-gates that run over the raw comp pool BEFORE comp_scorer ranks it:

  1. **Water proximity.** For an INLAND subject, an oceanfront comp inflates the
     projection. `classify_water_proximity` labels a listing `on_water` /
     `near_water` / `inland` so the caller can drop on-water comps.
  2. **Must-have features.** If the subject has a pool, comps without one drag
     the projection down. `has_feature` decides per listing;
     `detect_required_features` turns the subject into the list to require.

WHY THIS MODULE EXISTS (the bug it replaces)
--------------------------------------------
The original implementation was a bare substring scan of name+description plus
snake_case amenity keys (`"hot_tub"`, `"waterfront"`, `"beach_access"`). AirROI
returns Title Case DISPLAY strings and ZERO of the 136 real strings contain an
underscore, so the amenity half NEVER FIRED -- every verdict came from
unanchored substring matching of host marketing copy. Measured over the 150
live fixtures in tests/fixtures/ (ground truth = AirROI's own amenity list):

    hot_tub   recall 90.9%   precision 83.3%   (12 false positives, 6 misses)
    pool      recall 100.0%  precision 76.2%   (19 false positives)

Real failures that produced those numbers:
  * "*Summer seasonal community pool*" and "minutes walk to the pool" -> pool
  * "Whirlpool tub, no pool"                                          -> pool
  * "Cabin with pool table"                                           -> pool
  * a Destin resort listing naming the resort's pool in its blurb     -> hot tub

The rules here, in order of authority:

  * `property_details.amenities` is the authoritative structured field (0% null
    across 200 live records). Match it by EXACT SET MEMBERSHIP, case-normalized.
    Never substring-match it: "Pool" is a substring of "Pool table" and
    "Pool view"; "fire" of the near-universal "Fire extinguisher".
  * When that list is AirROI-shaped, its silence is a NEGATIVE ANSWER. Host
    marketing copy does not get to overrule it. This is the single change that
    kills every false positive above.
  * Free text is used only where the vocabulary has no key for the concept
    ("sauna" appears in 0 of the 136 strings; so does "oceanfront"), or where
    the amenity list is not AirROI-shaped -- e.g. a Firecrawl-scraped subject
    whose amenities read ["Private pool", "Spa"].
  * Text matching is HTML-stripped, word-boundary anchored, trap-guarded
    ("pool table"/"pool view" are not a pool), and negation-guarded
    ("no pool", "we do not have a hot tub").

The feature -> amenity mapping is REUSED from `comp_scorer.AMENITY_VOCAB_SIGNALS`
and `comp_scorer.TEXT_ONLY_SIGNALS` rather than restated here, so there is one
place to change when AirROI's vocabulary moves. tests/test_comp_filters.py
fails if the two drift apart or if any vocabulary string stops being real.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from comp_scorer import AMENITY_VOCAB_SIGNALS, TEXT_ONLY_SIGNALS

# ── The real AirROI amenity vocabulary ────────────────────────────────────
#
# All 136 distinct `property_details.amenities` display strings observed across
# the 200-record live corpus. Used ONLY to answer "is this amenity list
# AirROI-shaped, i.e. is its silence trustworthy?" -- never for feature
# detection, which goes through the reused signal maps below.
#
# tests/test_comp_filters.py asserts this set is byte-identical to
# tests/fixtures/amenity_vocab.json and to the live corpus, so it cannot drift.
AIRROI_AMENITY_VOCAB: frozenset[str] = frozenset({
    "Air conditioning", "Arcade games", "BBQ grill", "Baby bath",
    "Baby safety gates", "Babysitter recommendations", "Backyard",
    "Baking sheet", "Barbecue utensils", "Bathtub", "Beach access",
    "Beach essentials", "Bed linens", "Bidet", "Bikes", "Blender",
    "Board games", "Boat slip", "Body soap", "Books and reading material",
    "Bread maker", "Building staff", "Cable TV", "Carbon monoxide alarm",
    "Ceiling fan", "Children's playroom", "Children’s bikes",
    "Children’s books and toys", "Children’s dinnerware",
    "Cleaning before checkout", "Cleaning products", "Climbing wall",
    "Clothing storage", "Coffee", "Coffee maker", "Conditioner",
    "Cooking basics", "Crib", "Dedicated workspace", "Dining table",
    "Dishes and silverware", "Dishwasher", "Dryer", "Drying rack for clothing",
    "EV charger", "Elevator", "Essentials", "Ethernet connection",
    "Exercise equipment", "Exterior security cameras on property",
    "Extra pillows and blankets", "Fire extinguisher", "Fire pit",
    "Fireplace guards", "First aid kit", "Free parking on premises",
    "Free street parking", "Freezer", "Game console", "Garden view", "Gym",
    "Hair dryer", "Hammock", "Hangers", "Heating", "High chair", "Hockey rink",
    "Hot tub", "Hot water", "Hot water kettle", "Indoor fireplace", "Iron",
    "Kayak", "Keypad", "Kitchen", "Lake access", "Laundromat nearby",
    "Life size games", "Lockbox", "Long term stays allowed",
    "Luggage dropoff allowed", "Microwave", "Mini fridge", "Mini golf",
    "Mosquito net", "Movie theater", "Noise decibel monitors on property",
    "Ocean view", "Outdoor dining area", "Outdoor furniture",
    "Outdoor kitchen", "Outdoor playground", "Outdoor shower", "Outlet covers",
    "Oven", "Pack ’n play/Travel crib", "Paid parking off premises",
    "Paid parking on premises", "Patio or balcony", "Pets allowed", "Piano",
    "Ping pong table", "Pocket wifi", "Pool", "Pool table", "Pool view",
    "Portable fans", "Private entrance", "Record player", "Refrigerator",
    "Resort access", "Rice maker", "River view", "Room-darkening shades",
    "Safe", "Self check-in", "Shampoo", "Shower gel", "Single level home",
    "Ski-in/Ski-out", "Smart lock", "Smoke alarm", "Smoking allowed",
    "Sound system", "Stove", "Sun loungers", "TV", "Table corner guards",
    "Theme room", "Toaster", "Trash compactor", "Washer", "Waterfront",
    "Wifi", "Window guards", "Wine glasses",
})

_VOCAB_LOWER: frozenset[str] = frozenset(a.lower() for a in AIRROI_AMENITY_VOCAB)


# ── Water proximity ───────────────────────────────────────────────────────

WATER_ON = "on_water"
WATER_NEAR = "near_water"
WATER_INLAND = "inland"

# Exact amenity members that put a listing ON the water. "Waterfront" is
# AirROI's own flag; "Boat slip" means a private dock, which is the same claim.
# Both are members of AMENITY_VOCAB_SIGNALS["water"]/["views"] upstream.
ON_WATER_AMENITIES: tuple[str, ...] = ("Waterfront", "Boat slip")

# Exact amenity members that put a listing NEAR the water but not on it.
# "Beach essentials" is deliberately absent: it is a bag of towels, and the old
# substring scan on "beach" swept it in.
NEAR_WATER_AMENITIES: tuple[str, ...] = (
    "Beach access", "Lake access", "Ocean view", "River view",
)

# Free text carries the rest. There is no "oceanfront"/"beachfront"/"lakefront"
# amenity in the vocabulary at all, so unlike the feature detector the text arm
# here always runs -- the structured field cannot express the concept.
#
# The text arm is split in two because of a measured failure mode. Scanning the
# whole description for the `*-front` family produced these live false
# positives, none of which is a waterfront property:
#     "rooftop with WATERFRONT panoramic views of the city"  (Nashville loft)
#     "steps from Broadway, the Ryman, and RIVERFRONT PARK"  (Nashville loft)
#     "three resort pools, including a GULF-FRONT infinity-edge pool" (Destin)
#     "gated BEACHFRONT ACCESS with limited access"          (Destin)
#     "3000+ feet of PRIVATE BEACH access"                   (Destin)
# In every case the waterfront thing is a park, a pool, or an access point --
# not the stay. So:
#
#   _ON_WATER_TEXT_STRONG  -- phrases that can only describe the stay itself.
#                             Scanned in name AND description.
#   _ON_WATER_TEXT_TITLE   -- the `*-front` family and "private beach". Scanned
#                             in the listing NAME only, where a host has ~50
#                             characters and is claiming it about the property.
#                             Also scanned in the description when the amenity
#                             list is NOT AirROI-shaped (a scraped subject),
#                             because then noisy text is all there is.
_ON_WATER_TEXT_STRONG: tuple[str, ...] = (
    r"\btoes\s+(?:in|on)\s+the\s+sand\b",
    r"\bsteps\s+to\s+(?:the\s+)?sand\b",
    r"\bdirectly\s+on\s+the\s+(?:beach|water|lake|ocean|gulf|canal|bay|bayou|river)\b",
    r"\bright\s+on\s+(?:the\s+)?(?:beach|ocean|lake|water|gulf|canal|bay|bayou|river)\b",
    r"\bbeach\s+at\s+(?:your|the)\s+door\w*\b",
    r"\b(?:ocean|water|lake)'?s\s+edge\b",
    r"\bdirect\s+(?:beach|ocean|gulf|water|lake)\s+access\b",
)
_ON_WATER_TEXT_TITLE: tuple[str, ...] = (
    r"\b(?:ocean|beach|lake|gulf|river|water|bay|bayou|canal|creek|harbou?r|"
    r"sea|surf|dock)\s*-?\s*front\b",
    # "Private Beach Access" is beach ACCESS, not a private beach. The old
    # classifier read it as on-water and dropped good comps.
    r"\bprivate\s+beach\b(?!\s*access)",
    r"\bon\s+the\s+(?:beach|ocean|lake|water|sand|gulf|canal|bayou)\b",
    r"\bslope\s*-?\s*to\s*-?\s*surf\b",
)
_NEAR_WATER_TEXT: tuple[str, ...] = (
    r"\bbeach\s+access\b", r"\blake\s+access\b", r"\bocean\s+access\b",
    r"\bwalk\s+to\s+(?:the\s+)?(?:beach|lake|water)\b",
    r"\bshort\s+walk\s+to\s+(?:the\s+)?(?:beach|lake)\b",
    r"\b(?:minutes|mins?|blocks?)\s+(?:to|from)\s+(?:the\s+)?(?:beach|lake|ocean)\b",
    r"\bclose\s+to\s+(?:the\s+)?(?:beach|lake|ocean)\b",
    r"\bnear\s+(?:the\s+)?(?:beach|lake|ocean)\b",
    r"\bsteps\s+from\s+(?:the\s+)?(?:beach|lake)\b",
    r"\b(?:ocean|gulf|lake|water|bay|river)\s+views?\b",
)


# ── Must-have features ────────────────────────────────────────────────────

# feature -> the comp_scorer signal whose exact-vocabulary tuple answers it.
# REUSED, not restated: comp_scorer.AMENITY_VOCAB_SIGNALS is the single source.
_FEATURE_TO_SIGNAL: dict[str, str] = {
    "pool": "pool",
    "hot_tub": "hot_tub",
    "ski_in_out": "ski_in_out",
    "ev_charger": "ev_charger",
    # "sauna" has NO signal here on purpose -- it is text-only (below).
}

# feature -> the comp_scorer TEXT_ONLY_SIGNALS key. Only for concepts with no
# amenity-vocabulary member at all. "sauna" appears in 0 of the 136 strings.
_FEATURE_TO_TEXT_ONLY_SIGNAL: dict[str, str] = {
    "sauna": "sauna",
}

SUPPORTED_FEATURES: tuple[str, ...] = (
    "pool", "hot_tub", "ski_in_out", "sauna", "ev_charger",
)

# Features checked on the subject by default when the caller does not name any.
DEFAULT_REQUIRED_CANDIDATES: tuple[str, ...] = ("pool", "hot_tub")

# Caller-friendly spellings -> canonical feature key.
_FEATURE_ALIASES: dict[str, str] = {
    "pool": "pool", "swimming pool": "pool", "private pool": "pool",
    "hot_tub": "hot_tub", "hot tub": "hot_tub", "hottub": "hot_tub",
    "hot-tub": "hot_tub", "jacuzzi": "hot_tub",
    "ski_in_out": "ski_in_out", "ski in out": "ski_in_out",
    "ski-in/out": "ski_in_out", "ski-in/ski-out": "ski_in_out",
    "ski_in_ski_out": "ski_in_out", "ski": "ski_in_out",
    "sauna": "sauna",
    "ev_charger": "ev_charger", "ev charger": "ev_charger",
    "ev": "ev_charger", "ev_charging": "ev_charger",
}

# Text patterns used ONLY as the fallback arm (see `has_feature`).
#
# "pool" carries a negative lookahead because the two live traps -- "Pool table"
# and "Pool view" -- are real amenity strings and also appear verbatim in
# descriptions. Word boundaries alone already handle "whirlpool"/"carpool":
# there is no boundary between "whirl" and "pool".
_FEATURE_TEXT_PATTERNS: dict[str, tuple[str, ...]] = {
    "pool": (
        r"\bpools?\b(?!\s*(?:table|tables|view|views|hall|halls|room|rooms|"
        r"noodle|noodles|cue|cues|stick|sticks))",
        r"\bswimming\s+pool\b", r"\bplunge\s+pool\b", r"\blap\s+pool\b",
    ),
    "hot_tub": (
        r"\bhot\s*-?\s*tubs?\b", r"\bhottubs?\b", r"\bjacuzzis?\b",
        r"\bjetted\s+tub\b", r"\bspa\s+tub\b",
    ),
    "ski_in_out": (
        r"\bski\s*-?\s*in\b", r"\bski\s*-?\s*out\b",
        r"\bslope\s*-?\s*side\b", r"\bski\s+access\b",
    ),
    # sauna keywords come from comp_scorer.TEXT_ONLY_SIGNALS -- see
    # _text_patterns_for(). Nothing is restated here.
    "ev_charger": (
        r"\bev\s+charg\w*\b", r"\belectric\s+vehicle\s+charg\w*\b",
        r"\btesla\s+charg\w*\b", r"\blevel\s*2\s+charg\w*\b",
    ),
}


# ── Text normalization ────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: Optional[str]) -> str:
    """Drop HTML tags and entities, collapse whitespace, lowercase.

    138 of the 150 live descriptions contain markup. Without this,
    `space</b><br` matched a bare "spa" token and `pool</b>` defeated a
    trailing word-boundary assertion.
    """
    if not text:
        return ""
    plain = _TAG_RE.sub(" ", str(text))
    plain = html.unescape(plain)
    return _WS_RE.sub(" ", plain).strip().lower()


# How far back from a match to look for a negator, and the clause boundaries
# that stop the search. Commas count as boundaries so that the very common
# "No smoking, no pets, pool and hot tub included" is NOT read as a negation.
_NEGATION_WINDOW = 45
_NEGATION_RE = re.compile(
    r"(?:\b(?:no|not|non|without|lack|lacks|lacking|nor|never|none|cannot|"
    r"exclude|excludes|excluding|unfortunately|sadly|sorry)\b|\w+n['’]t\b)"
    r"[^.;,!?]{0,%d}$" % _NEGATION_WINDOW
)


def _is_negated(text: str, start: int) -> bool:
    """True if the match at `start` sits in a negated clause.

    Kills "we do not have a hot tub", "there is no pool", "Whirlpool tub, no
    pool". Bounded to the current clause so a negation two sentences earlier
    does not suppress a real amenity.
    """
    window = text[max(0, start - _NEGATION_WINDOW - 24):start]
    return bool(_NEGATION_RE.search(window))


def _text_matches(text: str, patterns: Iterable[str]) -> bool:
    """Any un-negated word-boundary match of `patterns` in `text`."""
    for pat in patterns:
        for m in re.finditer(pat, text):
            if not _is_negated(text, m.start()):
                return True
    return False


# ── Amenity handling ──────────────────────────────────────────────────────

def _amenity_set(amenities) -> set[str]:
    """Case-normalized set of amenity strings.

    Case-normalized so the flat shape produced by
    `adapters.airroi_to_comp.map_for_scorer` (which lowercases its
    `amenities` list) is matched by the same EXACT membership test as the raw
    Title Case list. This is still exact membership, never substring.
    """
    if not isinstance(amenities, (list, tuple, set, frozenset)):
        return set()
    return {a.strip().lower() for a in amenities
            if isinstance(a, str) and a.strip()}


# An AirROI amenity list is 7-89 entries long (median 45) and every entry is a
# vocabulary member. A Firecrawl-scraped subject's list is short, free-form and
# hits almost none. These thresholds separate the two so that only a genuine
# AirROI list gets to answer "no" on its own.
_AUTHORITATIVE_MIN_HITS = 5
_AUTHORITATIVE_MIN_RATIO = 0.6


def amenity_list_is_authoritative(amenities) -> bool:
    """True when this amenity list is AirROI-shaped, so its SILENCE means "no".

    A Zillow/Firecrawl subject whose amenities read ["Private pool", "Spa"]
    fails this test, and `has_feature` falls through to the text arm for it.
    """
    items = _amenity_set(amenities)
    if len(items) < _AUTHORITATIVE_MIN_HITS:
        return False
    hits = sum(1 for a in items if a in _VOCAB_LOWER)
    return (hits >= _AUTHORITATIVE_MIN_HITS
            and hits >= _AUTHORITATIVE_MIN_RATIO * len(items))


# ── Shape handling: nested AirROI comp OR flat mapped dict ────────────────

def extract_listing_fields(obj) -> tuple[str, str, list]:
    """Return (name, description, amenities) from either supported shape.

    Nested AirROI: {"listing_info": {...}, "property_details": {"amenities": []}}
    Flat mapped:   {"name": ..., "description": ..., "amenities_raw": [...]}

    `amenities_raw` is preferred over `amenities` on the flat shape because
    map_for_scorer() lowercases and keyword-expands the latter.
    """
    if not isinstance(obj, dict):
        return "", "", []

    li = obj.get("listing_info") or {}
    pd = obj.get("property_details") or {}

    name = li.get("listing_name") or obj.get("name") or ""
    description = li.get("description") or obj.get("description") or ""

    amenities = pd.get("amenities")
    if amenities is None:
        amenities = obj.get("amenities_raw")
    if amenities is None:
        amenities = obj.get("amenities")

    # Listing type / room type are useful free-text context and cost nothing.
    extra = " ".join(str(li.get(k) or "") for k in ("listing_type", "room_type"))
    if extra.strip():
        description = f"{description} {extra}"

    return str(name or ""), str(description or ""), list(amenities or [])


# ── Public: water proximity ───────────────────────────────────────────────

def classify_water_proximity(
    name: Optional[str],
    description: Optional[str],
    amenities=None,
) -> str:
    """Return WATER_ON / WATER_NEAR / WATER_INLAND for one listing.

    Authority order, same as `has_feature`:
      1. Exact amenity membership (`Waterfront`, `Boat slip` -> on water;
         `Beach access`, `Lake access`, `Ocean view`, `River view` -> near).
      2. Free text, because the vocabulary has no word for "oceanfront". The
         `*-front` family is read from the listing NAME only when the amenity
         list is AirROI-shaped -- in a 4000-character description it lands on
         waterfront PARKS, gulf-front POOLS and beachfront ACCESS far more
         often than on the stay. When the list is not AirROI-shaped (a scraped
         subject), the description is all there is, so it is scanned too.
    """
    amen = _amenity_set(amenities)
    name_text = strip_html(name)
    body_text = strip_html(f"{name or ''} . {description or ''}")
    authoritative = amenity_list_is_authoritative(amenities)
    title_scope = name_text if authoritative else body_text

    if amen & {a.lower() for a in ON_WATER_AMENITIES}:
        return WATER_ON
    if _text_matches(body_text, _ON_WATER_TEXT_STRONG):
        return WATER_ON
    if _text_matches(title_scope, _ON_WATER_TEXT_TITLE):
        return WATER_ON
    if amen & {a.lower() for a in NEAR_WATER_AMENITIES}:
        return WATER_NEAR
    if _text_matches(body_text, _NEAR_WATER_TEXT):
        return WATER_NEAR
    return WATER_INLAND


def classify_comp_water_proximity(comp) -> str:
    """`classify_water_proximity` for a nested AirROI comp or a flat mapped dict."""
    name, description, amenities = extract_listing_fields(comp)
    return classify_water_proximity(name, description, amenities)


# ── Public: must-have features ────────────────────────────────────────────

def normalize_feature(feature: str) -> str:
    """Canonicalize a caller-supplied feature name. Raises on an unknown one.

    Raising beats returning False: a typo in `--require hottub` silently
    dropping every comp is a far worse failure than a loud error.
    """
    key = str(feature or "").strip().lower().replace("_", " ")
    key = _WS_RE.sub(" ", key)
    canon = _FEATURE_ALIASES.get(key) or _FEATURE_ALIASES.get(key.replace(" ", "_"))
    if canon is None:
        raise ValueError(
            f"unknown feature {feature!r}; supported: {', '.join(SUPPORTED_FEATURES)}"
        )
    return canon


def _vocab_for(feature: str) -> tuple[str, ...]:
    """Exact amenity members answering `feature`, from comp_scorer's map."""
    signal = _FEATURE_TO_SIGNAL.get(feature)
    return tuple(AMENITY_VOCAB_SIGNALS.get(signal, ())) if signal else ()


def _text_patterns_for(feature: str) -> tuple[str, ...]:
    """Regex patterns for the text arm, incl. comp_scorer's text-only keywords."""
    pats = list(_FEATURE_TEXT_PATTERNS.get(feature, ()))
    signal = _FEATURE_TO_TEXT_ONLY_SIGNAL.get(feature)
    if signal:
        # Optional plural: the reused keyword is "sauna", the copy says "saunas".
        pats += [r"\b" + re.escape(kw) + r"s?\b"
                 for kw in TEXT_ONLY_SIGNALS.get(signal, ())]
    return tuple(pats)


def has_feature(
    name: Optional[str],
    description: Optional[str],
    amenities=None,
    feature: str = "pool",
) -> bool:
    """Does this listing have `feature`?

    Decision order:
      1. Exact (case-normalized) membership in the amenity list  -> True.
      2. Amenity list is AirROI-shaped and does NOT contain it   -> False.
         Host marketing copy does not overrule the structured field. This is
         the rule that removes all 31 false positives the substring scan made
         across the 150 live fixtures.
      3. Otherwise (empty list, or a non-AirROI list such as a scraped
         subject's) -> word-boundary, negation-guarded text match over
         name + description + the amenity strings themselves. This arm is
         deliberately looser: a scraped subject listing "Hot tub cover" is
         read as having a hot tub, because there is no structured field to
         appeal to and a missed subject feature disables the whole gate.

    Features with no vocabulary member at all (`sauna`) skip straight to 3.
    """
    feature = normalize_feature(feature)
    amen = _amenity_set(amenities)
    vocab = {v.lower() for v in _vocab_for(feature)}

    if vocab:
        if amen & vocab:
            return True
        if amenity_list_is_authoritative(amenities):
            return False

    text = strip_html(
        f"{name or ''} . {description or ''} . "
        + " . ".join(a for a in (amenities or []) if isinstance(a, str))
    )
    return _text_matches(text, _text_patterns_for(feature))


def comp_has_feature(comp, feature: str) -> bool:
    """`has_feature` for a nested AirROI comp or a flat mapped dict."""
    name, description, amenities = extract_listing_fields(comp)
    return has_feature(name, description, amenities, feature)


def detect_required_features(
    name: Optional[str],
    description: Optional[str],
    amenities=None,
    candidates: Sequence[str] = DEFAULT_REQUIRED_CANDIDATES,
) -> list[str]:
    """Which of `candidates` the SUBJECT has, i.e. what comps must also have."""
    return [f for f in (normalize_feature(c) for c in candidates)
            if has_feature(name, description, amenities, f)]


# ── Public: apply the gates to a pool ─────────────────────────────────────

@dataclass
class FilterReport:
    """What `apply_comp_filters` dropped and why. The caller does the printing."""

    kept: int = 0
    water_dropped: list[str] = field(default_factory=list)
    feature_dropped: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return len(self.water_dropped) + len(self.feature_dropped)

    def lines(self) -> list[str]:
        """Human-readable summary lines, ready to print."""
        out: list[str] = []
        if self.water_dropped:
            out.append(f"Dropped {len(self.water_dropped)} on-water comps:")
            out += [f"    - {n}" for n in self.water_dropped]
        if self.feature_dropped:
            out.append(f"Dropped {len(self.feature_dropped)} comps missing "
                       f"required features:")
            out += [f"    - {n}  (missing: {', '.join(m)})"
                    for n, m in self.feature_dropped]
        return out


def apply_comp_filters(
    comps: Iterable,
    *,
    drop_on_water: bool = False,
    required_features: Sequence[str] = (),
) -> tuple[list, FilterReport]:
    """Drop on-water and/or feature-missing comps. Returns (kept, report).

    `drop_on_water` should be set by the caller only when the SUBJECT is inland;
    an on-water subject wants on-water comps. `near_water` is never dropped --
    it is the honest middle ground and dropping it empties beach-town pools.
    """
    required = [normalize_feature(f) for f in required_features]
    report = FilterReport()
    kept: list = []

    for comp in comps:
        name, _, _ = extract_listing_fields(comp)
        label = name or "(unnamed)"

        if drop_on_water and classify_comp_water_proximity(comp) == WATER_ON:
            report.water_dropped.append(label)
            continue

        if required:
            missing = [f for f in required if not comp_has_feature(comp, f)]
            if missing:
                report.feature_dropped.append((label, missing))
                continue

        kept.append(comp)

    report.kept = len(kept)
    return kept, report
