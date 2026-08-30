"""
comp_filters guards — water proximity and must-have feature detection.

These tests exist because the detector they replace was measured, against
AirROI's own amenity list over the 150 live listings in tests/fixtures/, at:

    hot_tub   recall 90.9%   precision 83.3%   (12 false positives, 6 misses)
    pool      recall 100.0%  precision 76.2%   (19 false positives)

It was a bare substring scan of name+description plus snake_case amenity keys
(`"hot_tub"`, `"beach_access"`) that matched NOTHING, because AirROI returns
Title Case display strings and zero of the 136 real strings contain an
underscore. Every verdict came from host marketing copy.

The rules locked in here:
  * amenities are matched by EXACT (case-normalized) SET MEMBERSHIP, never
    substring -- "Pool" is a substring of "Pool table" and "Pool view";
  * an AirROI-shaped amenity list that omits a feature is a NEGATIVE ANSWER,
    and marketing copy does not overrule it;
  * free text is word-boundary anchored, HTML-stripped and negation-guarded,
    and only runs where the vocabulary has no key or the list is not
    AirROI-shaped (a scraped subject);
  * the `*-front` water family is read from the listing NAME, because in a
    description it lands on waterfront PARKS, gulf-front POOLS and beachfront
    ACCESS more often than on the stay.

Run: pytest tests/test_comp_filters.py -v
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from _fixtures import all_live_listings, amenity_vocabulary, market_listings

import comp_filters as cf
from adapters.airroi_to_comp import _AMENITY_MAP, map_for_scorer
from comp_scorer import AMENITY_VOCAB_SIGNALS, TEXT_ONLY_SIGNALS

VOCAB = amenity_vocabulary()
LIVE = all_live_listings()

# feature -> the amenity string that is ground truth for it.
GROUND_TRUTH = {
    "pool": "Pool",
    "hot_tub": "Hot tub",
    "ski_in_out": "Ski-in/Ski-out",
    "ev_charger": "EV charger",
}


def _fields(listing: dict) -> tuple[str, str, list]:
    li = listing.get("listing_info") or {}
    pd = listing.get("property_details") or {}
    return (li.get("listing_name") or "", li.get("description") or "",
            pd.get("amenities") or [])


# ── The vocabulary cannot drift ───────────────────────────────────────────

def test_shipped_vocabulary_is_the_real_one():
    """AIRROI_AMENITY_VOCAB must equal the corpus, exactly.

    It is shipped in comp_filters (not read from tests/) because the runtime
    needs it to decide whether an amenity list is AirROI-shaped. This test is
    the only thing stopping the two copies from diverging.
    """
    assert set(cf.AIRROI_AMENITY_VOCAB) == VOCAB, (
        f"only in module: {sorted(set(cf.AIRROI_AMENITY_VOCAB) - VOCAB)[:10]}; "
        f"only in fixture: {sorted(VOCAB - set(cf.AIRROI_AMENITY_VOCAB))[:10]}"
    )


def test_shipped_vocabulary_matches_live_corpus():
    observed: set[str] = set()
    for listing in LIVE:
        observed.update((listing.get("property_details") or {}).get("amenities") or [])
    assert observed == set(cf.AIRROI_AMENITY_VOCAB)


def test_no_vocabulary_string_contains_an_underscore():
    """The original bug in one line: snake_case keys match nothing."""
    assert [a for a in cf.AIRROI_AMENITY_VOCAB if "_" in a] == []


# ── Feature -> amenity mapping is REUSED, not restated ────────────────────

def test_feature_vocab_comes_from_comp_scorer():
    for feature, signal in cf._FEATURE_TO_SIGNAL.items():
        assert signal in AMENITY_VOCAB_SIGNALS, f"{feature} -> unknown signal {signal}"
        assert cf._vocab_for(feature) == AMENITY_VOCAB_SIGNALS[signal]


def test_every_feature_vocab_member_is_a_real_amenity():
    for feature in cf._FEATURE_TO_SIGNAL:
        for member in cf._vocab_for(feature):
            assert member in cf.AIRROI_AMENITY_VOCAB, f"{feature}: {member!r} is not real"


def test_feature_vocab_agrees_with_the_adapter_map():
    """comp_filters and adapters must not disagree about what "Pool" means."""
    for feature, signal in cf._FEATURE_TO_SIGNAL.items():
        for member in cf._vocab_for(feature):
            assert member in _AMENITY_MAP, (
                f"{member!r} is a filter vocab member but has no adapter badge entry"
            )


def test_water_amenities_are_real_vocabulary_members():
    for member in cf.ON_WATER_AMENITIES + cf.NEAR_WATER_AMENITIES:
        assert member in cf.AIRROI_AMENITY_VOCAB, f"{member!r} is not a real amenity"


def test_sauna_is_text_only_because_it_has_no_amenity_key():
    """Documented in AIRROI_CONTRACT: 'sauna' matches 0 of the 136 strings."""
    assert not any("sauna" in a.lower() for a in cf.AIRROI_AMENITY_VOCAB)
    assert cf._vocab_for("sauna") == ()
    assert cf._FEATURE_TO_TEXT_ONLY_SIGNAL["sauna"] in TEXT_ONLY_SIGNALS
    assert cf._text_patterns_for("sauna")  # non-empty


def test_beach_essentials_is_not_a_water_signal():
    """'beach' as a substring sweeps in a bag of towels."""
    assert "Beach essentials" in cf.AIRROI_AMENITY_VOCAB
    assert "Beach essentials" not in cf.ON_WATER_AMENITIES
    assert "Beach essentials" not in cf.NEAR_WATER_AMENITIES
    assert cf.classify_water_proximity(
        "Mountain Cabin", "Quiet retreat.", ["Beach essentials"],
    ) == cf.WATER_INLAND


# ── Exact membership, never substring ─────────────────────────────────────

@pytest.mark.parametrize("amenity", ["Pool table", "Pool view"])
def test_pool_substring_traps_are_not_a_pool(amenity):
    assert cf.has_feature("Cabin", "Great stay.", [amenity], "pool") is False


def test_pool_amenity_is_a_pool():
    assert cf.has_feature("Cabin", "", ["Pool"], "pool") is True


def test_amenity_matching_is_case_insensitive_exact():
    """map_for_scorer lowercases its amenity list; the same rule must apply."""
    assert cf.has_feature("x", "", ["hot tub"], "hot_tub") is True
    assert cf.has_feature("x", "", ["HOT TUB"], "hot_tub") is True


def test_membership_is_exact_not_substring_on_an_authoritative_list():
    """"Hot tub cover" is not "Hot tub". On an AirROI-shaped list the answer is
    no -- the loose text arm is not reached."""
    amen = _airroi_shaped("Hot tub cover", "Pool table", "Pool view")
    assert cf.amenity_list_is_authoritative(amen) is True
    assert cf.has_feature("x", "", amen, "hot_tub") is False
    assert cf.has_feature("x", "", amen, "pool") is False


# ── The documented false positives, dead ──────────────────────────────────

def test_pool_table_cabin_is_not_a_pool():
    """`has_feature(pool)` returned True for 'Cabin with pool table'."""
    assert cf.has_feature("Cabin with pool table", "", [], "pool") is False


def test_whirlpool_tub_no_pool_is_not_a_pool():
    """`has_feature(pool)` returned True for 'Whirlpool tub, no pool'."""
    assert cf.has_feature("Lodge", "Whirlpool tub, no pool.", [], "pool") is False


@pytest.mark.parametrize("text", [
    "There is no pool on the property.",
    "no pool",
    "The home does not have a pool.",
    "Sorry, no pool here.",
    "We don't have a pool.",
    "This cabin lacks a pool.",
    "Guests often ask -- unfortunately no pool.",
])
def test_negated_pool_is_not_a_pool(text):
    assert cf.has_feature("Cabin", text, [], "pool") is False


@pytest.mark.parametrize("text", [
    "We do not have a hot tub.",
    "no hot tub",
    "The unit doesn't have a hot tub.",
    "No pool, no hot tub.",
])
def test_negated_hot_tub_is_not_a_hot_tub(text):
    assert cf.has_feature("Cabin", text, [], "hot_tub") is False


def test_negation_guard_does_not_swallow_a_later_clause():
    """'No smoking, no pets, pool and hot tub included' really does have both.

    A negation window that ignored commas would read the 'no pets' as
    negating the pool and drop a perfectly good comp.
    """
    text = "No smoking, no pets, pool and hot tub included."
    assert cf.has_feature("Cabin", text, [], "pool") is True
    assert cf.has_feature("Cabin", text, [], "hot_tub") is True


def test_negation_is_clause_scoped_not_document_scoped():
    text = "There is no elevator. The private pool is heated year round."
    assert cf.has_feature("Villa", text, [], "pool") is True


# ── The authoritative-negative rule (what removed all 31 FPs) ─────────────

def _airroi_shaped(*extra: str) -> list[str]:
    """A realistic AirROI amenity list (median length in the corpus is 45)."""
    base = ["Wifi", "Kitchen", "Heating", "Air conditioning", "Smoke alarm",
            "Carbon monoxide alarm", "Essentials", "Hangers", "Hair dryer",
            "Iron", "Washer", "Dryer", "TV", "Dishes and silverware",
            "Cooking basics", "Bed linens", "Hot water", "Refrigerator",
            "Microwave", "Free parking on premises"]
    return base + list(extra)


def test_marketing_copy_cannot_overrule_an_airroi_amenity_list():
    """The live FP: a cabin whose blurb says '*Summer seasonal community pool*'
    but whose amenity list has no Pool."""
    amen = _airroi_shaped()
    assert cf.amenity_list_is_authoritative(amen) is True
    assert cf.has_feature(
        "Downtown Gatlinburg~ Walking Distance to Strip",
        "No need to drive! *Summer seasonal community pool* and a trolley stop.",
        amen, "pool",
    ) is False


def test_resort_blurb_cannot_invent_a_hot_tub():
    amen = _airroi_shaped("Pool")
    assert cf.has_feature(
        "Palmetto Cottage at Destin Pointe Resort",
        "The resort has three pools and a hot tub for all guests.",
        amen, "hot_tub",
    ) is False


def test_airroi_list_that_does_contain_it_still_says_yes():
    assert cf.has_feature("x", "", _airroi_shaped("Hot tub"), "hot_tub") is True


# ── The text fallback, for listings that are NOT AirROI-shaped ────────────

def test_scraped_subject_amenities_fall_through_to_text():
    """A Firecrawl-scraped subject reads ['Private pool', 'Spa'] -- not AirROI
    vocabulary, so its silence proves nothing and text must decide."""
    amen = ["Private pool", "Spa"]
    assert cf.amenity_list_is_authoritative(amen) is False
    assert cf.has_feature("Desert Villa", "", amen, "pool") is True


def test_empty_amenity_list_falls_through_to_text():
    assert cf.amenity_list_is_authoritative([]) is False
    assert cf.has_feature("Chalet", "Private hot tub on the deck.", [], "hot_tub") is True


def test_short_non_vocab_list_is_not_authoritative():
    assert cf.amenity_list_is_authoritative(["Pool table", "Wifi"]) is False


def test_real_airroi_lists_are_all_authoritative():
    """All 150 live listings carry an AirROI-shaped list, so the text fallback
    never runs on real comp data. Recorded so a future change is noticed."""
    n = sum(1 for l in LIVE
            if cf.amenity_list_is_authoritative(_fields(l)[2]))
    assert n == len(LIVE) == 150


def test_html_is_stripped_before_matching():
    """138 of 150 live descriptions contain markup."""
    assert cf.has_feature("x", "<b>hot&nbsp;tub</b><br />on the deck", [], "hot_tub") is True
    # and the old 'spa' substring trap stays dead
    assert cf.has_feature("x", "a spacious workspace</b><br", [], "sauna") is False


def test_word_boundaries_hold():
    assert cf.has_feature("x", "carpool to the slopes", [], "pool") is False
    assert cf.has_feature("x", "whirlpool bath", [], "pool") is False
    assert cf.has_feature("x", "saunas are available", [], "sauna") is True


def test_ski_and_ev_and_sauna_text_signals():
    assert cf.has_feature("Ski-in/Ski-out Chalet", "", [], "ski_in_out") is True
    assert cf.has_feature("x", "slopeside location", [], "ski_in_out") is True
    assert cf.has_feature("x", "Tesla charger in the garage", [], "ev_charger") is True
    assert cf.has_feature("x", "dry sauna downstairs", [], "sauna") is True
    assert cf.has_feature("x", "no sauna available", [], "sauna") is False


# ── Feature naming ────────────────────────────────────────────────────────

@pytest.mark.parametrize("alias,canon", [
    ("hot tub", "hot_tub"), ("HOT_TUB", "hot_tub"), ("jacuzzi", "hot_tub"),
    ("Pool", "pool"), ("ski-in/out", "ski_in_out"), ("ev charger", "ev_charger"),
])
def test_feature_aliases(alias, canon):
    assert cf.normalize_feature(alias) == canon


def test_hottub_is_an_alias_not_an_error():
    assert cf.normalize_feature("hottub") == "hot_tub"


def test_unknown_feature_raises_rather_than_silently_dropping_everything():
    with pytest.raises(ValueError):
        cf.normalize_feature("hottub_deluxe")
    with pytest.raises(ValueError):
        cf.has_feature("x", "", [], "swimmingpool")


def test_supported_features_are_all_resolvable():
    for f in cf.SUPPORTED_FEATURES:
        assert cf.normalize_feature(f) == f
        assert cf._vocab_for(f) or cf._text_patterns_for(f)


# ── Live-corpus accuracy: the number this module exists to move ───────────

@pytest.mark.parametrize("feature,truth_amenity", sorted(GROUND_TRUTH.items()))
def test_perfect_precision_and_recall_on_the_live_corpus(feature, truth_amenity):
    """Against AirROI's own amenity list over all 150 live listings.

    Baseline (old substring detector): hot_tub 90.9%/83.3%, pool 100%/76.2%.
    """
    tp = fp = fn = 0
    for listing in LIVE:
        name, desc, amen = _fields(listing)
        truth = truth_amenity in set(amen)
        pred = cf.has_feature(name, desc, amen, feature)
        if pred and truth:
            tp += 1
        elif pred:
            fp += 1
        elif truth:
            fn += 1
    assert fp == 0, f"{feature}: {fp} false positives"
    assert fn == 0, f"{feature}: {fn} false negatives"
    assert tp > 0, f"{feature}: never fired -- the detector is dead, not perfect"


def test_the_old_substring_detector_really_was_wrong_here():
    """Anchor the improvement to a specific live listing, not a percentage."""
    target = next(
        l for l in market_listings("gatlinburg")
        if (l.get("listing_info") or {}).get("listing_name")
        == "Downtown Gatlinburg~ Walking Distance to Strip"
    )
    name, desc, amen = _fields(target)
    assert "Pool" not in set(amen)                     # AirROI says no pool
    assert "pool" in f"{name} {desc}".lower()          # the blurb says pool
    assert cf.has_feature(name, desc, amen, "pool") is False


# ── Both input shapes ─────────────────────────────────────────────────────

def test_nested_and_flat_shapes_agree_on_every_live_listing():
    """comp_has_feature must read a raw AirROI comp and a map_for_scorer dict
    identically, or agent.py gets different answers before and after mapping."""
    for listing in LIVE:
        flat = map_for_scorer(listing)
        for feature in GROUND_TRUTH:
            assert cf.comp_has_feature(listing, feature) == cf.comp_has_feature(flat, feature)
        assert (cf.classify_comp_water_proximity(listing)
                == cf.classify_comp_water_proximity(flat))


def test_extract_handles_both_shapes_and_junk():
    nested = {"listing_info": {"listing_name": "N", "description": "D"},
              "property_details": {"amenities": ["Pool"]}}
    assert cf.extract_listing_fields(nested)[0] == "N"
    assert cf.extract_listing_fields(nested)[2] == ["Pool"]
    flat = {"name": "N2", "description": "D2", "amenities_raw": ["Hot tub"],
            "amenities": ["hot tub", "wifi"]}
    n, d, a = cf.extract_listing_fields(flat)
    assert (n, a) == ("N2", ["Hot tub"])      # raw wins over the lowercased list
    assert cf.extract_listing_fields(None) == ("", "", [])
    assert cf.extract_listing_fields({}) == ("", "", [])


def test_none_and_malformed_inputs_do_not_explode():
    assert cf.has_feature(None, None, None, "pool") is False
    assert cf.classify_water_proximity(None, None, None) == cf.WATER_INLAND
    assert cf.has_feature("x", "y", [None, 3, {"a": 1}, "Pool"], "pool") is True
    assert cf.classify_water_proximity("x", "y", "not-a-list") == cf.WATER_INLAND
    assert cf.comp_has_feature("not a dict", "pool") is False


# ── Water proximity ───────────────────────────────────────────────────────

def test_waterfront_and_boat_slip_amenities_are_on_water():
    assert cf.classify_water_proximity("x", "", ["Waterfront"]) == cf.WATER_ON
    assert cf.classify_water_proximity("x", "", ["Boat slip"]) == cf.WATER_ON


def test_beach_access_is_near_not_on():
    """'Private Beach Access' is access, not a private beach. The old
    classifier matched 'private beach' and dropped the comp."""
    assert cf.classify_water_proximity(
        "Southbay by the Gulf 11 w/ Private Beach Access", "",
        _airroi_shaped("Beach access"),
    ) == cf.WATER_NEAR


def test_ocean_and_river_view_amenities_are_near_water():
    assert cf.classify_water_proximity("x", "", ["Ocean view"]) == cf.WATER_NEAR
    assert cf.classify_water_proximity("x", "", ["River view"]) == cf.WATER_NEAR
    assert cf.classify_water_proximity("x", "", ["Lake access"]) == cf.WATER_NEAR


def test_beachfront_in_the_title_is_on_water():
    assert cf.classify_water_proximity(
        "Tropical Beachfront Condo w/ Great Views in Destin", "",
        _airroi_shaped("Waterfront"),
    ) == cf.WATER_ON
    assert cf.classify_water_proximity(
        "Silver Beach 506W Gulf Front-4BR", "", _airroi_shaped(),
    ) == cf.WATER_ON


def test_right_on_canal_is_on_water():
    """The live listing the old classifier missed entirely."""
    assert cf.classify_water_proximity(
        "The Back Porch 4bd/3th right on canal", "", _airroi_shaped(),
    ) == cf.WATER_ON


@pytest.mark.parametrize("desc", [
    "Access to the skydeck - rooftop with waterfront panoramic views of the city.",
    "Steps from Broadway, the Ryman, and riverfront park.",
    "Three resort pools, including a gulf-front infinity-edge pool.",
    "Destin is known for its gated beachfront access with limited access.",
    "Minutes to Old Town Scottsdale, Scottsdale Fashion Square, Scottsdale Waterfront.",
])
def test_a_waterfront_thing_nearby_does_not_make_the_stay_waterfront(desc):
    """Every one of these is a live description that the old classifier read as
    on_water. The waterfront thing is a park, a pool, an access point or a
    shopping district -- never the property."""
    assert cf.classify_water_proximity(
        "Downtown Loft", desc, _airroi_shaped(),
    ) == cf.WATER_INLAND


def test_negated_water_claim_is_inland():
    assert cf.classify_water_proximity(
        "Cabin", "The cabin is not on the beach, it is a 20 minute drive.", [],
    ) != cf.WATER_ON


def test_scraped_subject_gets_the_full_text_scan():
    """A non-AirROI listing has no trustworthy amenity list, so the description
    is all there is and the `*-front` family is read from it."""
    amen = ["Private pool"]
    assert cf.amenity_list_is_authoritative(amen) is False
    assert cf.classify_water_proximity(
        "Coastal Retreat", "A stunning oceanfront home with sweeping views.", amen,
    ) == cf.WATER_ON


def test_live_water_invariants():
    """On the 150 live listings: the structured field is never contradicted."""
    for listing in LIVE:
        amen = set(_fields(listing)[2])
        verdict = cf.classify_comp_water_proximity(listing)
        if amen & set(cf.ON_WATER_AMENITIES):
            assert verdict == cf.WATER_ON, _fields(listing)[0]
        elif amen & set(cf.NEAR_WATER_AMENITIES):
            assert verdict in (cf.WATER_ON, cf.WATER_NEAR), _fields(listing)[0]


def test_live_water_distribution_is_sane_per_market():
    """A beach market must classify as wet, a desert market as dry.

    The old classifier called 4 of 25 Destin listings inland and called a
    Scottsdale desert estate on_water (it matched the 'Scottsdale Waterfront'
    shopping district).
    """
    destin = Counter(cf.classify_comp_water_proximity(l) for l in market_listings("destin"))
    assert destin[cf.WATER_ON] >= 10
    assert destin[cf.WATER_INLAND] == 0

    scottsdale = Counter(cf.classify_comp_water_proximity(l)
                         for l in market_listings("scottsdale"))
    assert scottsdale[cf.WATER_ON] == 0

    sunpeaks = Counter(cf.classify_comp_water_proximity(l)
                       for l in market_listings("sunpeaks"))
    assert sunpeaks[cf.WATER_ON] == 0


# ── detect_required_features ──────────────────────────────────────────────

def test_detect_required_features_from_a_subject():
    assert cf.detect_required_features("Villa", "", ["Pool"]) == ["pool"]
    assert cf.detect_required_features("Chalet", "", ["Hot tub", "Pool"]) == ["pool", "hot_tub"]
    assert cf.detect_required_features("Cabin", "Cosy retreat.", []) == []


def test_detect_required_features_accepts_custom_candidates():
    assert cf.detect_required_features(
        "Chalet", "", ["Ski-in/Ski-out"], candidates=("ski_in_out", "pool"),
    ) == ["ski_in_out"]


# ── apply_comp_filters ────────────────────────────────────────────────────

def _comp(name, amenities, description=""):
    return {"listing_info": {"listing_name": name, "description": description},
            "property_details": {"amenities": _airroi_shaped(*amenities)}}


def test_apply_comp_filters_drops_on_water_only_when_asked():
    pool = [_comp("Oceanfront Condo", ["Waterfront"]),
            _comp("Mountain Cabin", ["Hot tub"])]
    kept, rep = cf.apply_comp_filters(pool, drop_on_water=False)
    assert len(kept) == 2 and rep.dropped == 0
    kept, rep = cf.apply_comp_filters(pool, drop_on_water=True)
    assert [(_fields(c)[0]) for c in kept] == ["Mountain Cabin"]
    assert rep.water_dropped == ["Oceanfront Condo"]
    assert rep.kept == 1


def test_apply_comp_filters_keeps_near_water():
    """near_water is the honest middle ground; dropping it empties beach pools."""
    pool = [_comp("Walk to Beach House", ["Beach access"])]
    kept, rep = cf.apply_comp_filters(pool, drop_on_water=True)
    assert len(kept) == 1 and rep.water_dropped == []


def test_apply_comp_filters_drops_comps_missing_required_features():
    pool = [_comp("With Pool", ["Pool"]),
            _comp("No Pool", []),
            _comp("Pool + Tub", ["Pool", "Hot tub"])]
    kept, rep = cf.apply_comp_filters(pool, required_features=["pool", "hot_tub"])
    assert [_fields(c)[0] for c in kept] == ["Pool + Tub"]
    assert rep.feature_dropped == [("With Pool", ["hot_tub"]),
                                   ("No Pool", ["pool", "hot_tub"])]
    assert any("missing" in line for line in rep.lines())


def test_apply_comp_filters_returns_the_original_objects():
    a = _comp("A", ["Pool"])
    kept, _ = cf.apply_comp_filters([a], required_features=["pool"])
    assert kept[0] is a


def test_apply_comp_filters_no_ops_by_default():
    pool = [_comp("Oceanfront", ["Waterfront"]), _comp("Inland", [])]
    kept, rep = cf.apply_comp_filters(pool)
    assert len(kept) == 2 and rep.dropped == 0 and rep.lines() == []


def test_apply_comp_filters_on_the_live_pool():
    """An inland subject requiring a hot tub, run against real Gatlinburg comps."""
    comps = market_listings("gatlinburg")
    kept, rep = cf.apply_comp_filters(comps, drop_on_water=True,
                                      required_features=["hot_tub"])
    assert rep.kept == len(kept)
    assert rep.kept + rep.dropped == len(comps)
    for c in kept:
        assert cf.comp_has_feature(c, "hot_tub")
        assert cf.classify_comp_water_proximity(c) != cf.WATER_ON
    assert 0 < rep.kept < len(comps), "filter is either inert or eats everything"


def test_apply_comp_filters_rejects_an_unknown_required_feature():
    with pytest.raises(ValueError):
        cf.apply_comp_filters([_comp("A", [])], required_features=["helipad"])
