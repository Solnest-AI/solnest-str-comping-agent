"""Regression tests for the 2026-09-28 codebase review of the comp core
(comp_scorer, comp_filters, comp_similarity, adapters/airroi_to_comp, schema).

Each test pins one behaviour that was wrong. Real-data evidence where there is
some: on the captured Sun Peaks listing 0 the subject was flagged "luxury" by
"the grand windows of the Burfield runs", and on Gatlinburg listing 15 by
"vix premium" (a streaming service). That flag hard-fails every comp priced
under 55% of the subject.
"""

from __future__ import annotations

import pytest

import comp_filters as cf
import comp_similarity as sim
from adapters.airroi_to_comp import (
    _amenities_to_badges,
    map_batch_for_scorer,
    map_for_scorer,
    market_occupancy_ceiling,
)
from comp_scorer import (
    _score_amenity_match,
    _score_physical_match,
    detect_subject_signals,
    rank_comps,
    score_comp,
)
from schema import SubjectPerformance

# Five AirROI-vocabulary strings: enough for amenity_list_is_authoritative().
AIRROI_LIST = ["Wifi", "Kitchen", "Heating", "TV", "Washer", "Dryer"]


# ── 1. Luxury flag: whole words, title and description only ───────────────

@pytest.mark.parametrize("description", [
    "Home on Grandview Dr with room for the grandkids.",
    "Premium finishes throughout, sleeps 8.",
    "A grand old farmhouse with executive desk and designer lamps.",
    "Streaming: Netflix, HBO Max, VIX Premium.",
])
def test_ordinary_host_copy_is_not_a_luxury_claim(description):
    signals = detect_subject_signals({"title": "Cozy home", "description": description,
                                      "amenities": AIRROI_LIST})
    assert signals["luxury"] is False


@pytest.mark.parametrize("title, description", [
    ("Luxurious 4BR chalet", ""),
    ("Cabin", "A luxury retreat."),
    ("Cabin", "Upscale, high-end finishes."),
])
def test_unambiguous_luxury_claims_still_flag(title, description):
    assert detect_subject_signals({"title": title, "description": description})["luxury"] is True


def test_amenity_list_and_address_never_arm_the_luxury_flag():
    signals = detect_subject_signals({
        "title": "Cabin", "description": "Cabin 12 Grand Ave, Sun Peaks",
        "amenities": AIRROI_LIST + ["Luxury linens"],
    })
    assert signals["luxury"] is False


def _comp(**over):
    base = {"name": "Comp", "description": "", "amenities_raw": list(AIRROI_LIST),
            "bedrooms": 3, "sleeps": 8, "nightly_rate": 300.0, "rating": 4.8,
            "reviews": 40, "nights_booked": 200, "nights_listed": 340,
            "occupancy_pct": 55.0, "annual_revenue": 60000.0,
            "revenue_potential": 80000.0, "adr": 300.0,
            "latitude": 50.88, "longitude": -119.89}
    base.update(over)
    return base


def test_luxury_fail_reason_is_currency_neutral():
    subject = {"title": "Luxurious chalet", "adr": 850, "bedrooms": 3, "max_guests": 8}
    scored = score_comp(_comp(nightly_rate=200.0), detect_subject_signals(subject),
                        850, 3, 8, subject)
    assert scored["hard_fail"] and "luxury" in scored["hard_fail_reason"].lower()
    assert "CA$" not in scored["hard_fail_reason"]


# ── 2. Amenity signals: exact membership when the list is authoritative ───

def test_fireplace_guards_is_not_a_subject_fireplace():
    subject = {"title": "Cabin", "description": "Cabin with a gas fireplace and a gym.",
               "amenities": AIRROI_LIST + ["Fireplace guards"]}
    assert cf.amenity_list_is_authoritative(subject["amenities"])
    signals = detect_subject_signals(subject)
    assert signals["fireplace"] is False
    assert signals["gym"] is False


def test_real_members_still_set_the_signal():
    signals = detect_subject_signals({"title": "Cabin", "description": "",
                                      "amenities": AIRROI_LIST + ["Indoor fireplace", "Gym"]})
    assert signals["fireplace"] is True and signals["gym"] is True


def test_unstructured_subject_falls_back_to_negation_guarded_text():
    yes = detect_subject_signals({"title": "Cabin", "description": "Cozy gas fireplace, home gym."})
    assert yes["fireplace"] is True and yes["gym"] is True
    no = detect_subject_signals({"title": "Cabin", "description": "There is no gym and no fireplace."})
    assert no["fireplace"] is False and no["gym"] is False


# ── 3. Negation: not "non-smoking", "never", "won't" ──────────────────────

@pytest.mark.parametrize("text", [
    "Non-smoking cabin with a hot tub",
    "You will never want to leave the hot tub",
    "You won't want to leave the hot tub",
    "Not only a pool but a hot tub too",
    "Not to mention the hot tub",
    "Guests can't wait to try the hot tub",
])
def test_positive_phrases_are_not_negated(text):
    assert cf._is_negated(text, text.index("hot tub")) is False
    assert cf.has_feature("x", text, [], "hot_tub") is True


@pytest.mark.parametrize("text", [
    "There is no hot tub",
    "We do not have a hot tub",
    "The unit doesn't have a hot tub",
    "There isn't a hot tub",
    "Sorry, no hot tub here",
    "This cabin lacks a hot tub",
    "No smoking. No pets. Whirlpool tub, no hot tub",
])
def test_true_negations_still_negate(text):
    assert cf._is_negated(text, text.rindex("hot tub")) is True
    assert cf.has_feature("x", text, [], "hot_tub") is False


def test_comma_still_ends_a_negation():
    assert cf.has_feature("x", "No smoking, no pets, pool and hot tub included.", [], "hot_tub")


# ── 4. distance_km is set for hard-failed comps ───────────────────────────

def test_hard_failed_comp_still_gets_a_distance():
    subject = {"title": "Cabin", "bedrooms": 3, "max_guests": 8, "adr": 300,
               "latitude": 50.88, "longitude": -119.89}
    far_size = _comp(name="Too big", bedrooms=9, latitude=50.90, longitude=-119.90)
    result = rank_comps(subject, [far_size, _comp(name="Fine")], top_n=6)
    assert [c["name"] for c in result["hard_fails"]] == ["Too big"]
    assert result["hard_fails"][0]["distance_km"] is not None
    assert result["hard_fails"][0]["distance_km"] > 0


# ── 5. Targeted search normalizes raw --require strings ───────────────────

def test_targeted_filter_normalizes_raw_feature_strings():
    flt = sim.targeted_search_filter(bedrooms=3, bed_tolerance=1,
                                     required=["hot tub", "Pool"], lacking=["Ski-in/Out"])
    assert flt["amenities"] == {"all": ["hot_tub", "pool"], "none": ["ski_in_ski_out"]}


def test_targeted_reasons_and_shortfall_speak_canonical_features():
    short = sim.required_shortfall([], ["hot tub"])
    assert short == ["hot_tub"]
    reasons, _ = sim.targeted_search_reasons(["Ski-in/Out"], [], None,
                                             required_short=["Hot tub"])
    assert reasons == ["too few comparables with a hot tub",
                       "too few comparables without ski-in/ski-out access"]


def test_targeted_filter_still_rejects_unknown_features():
    with pytest.raises(ValueError):
        sim.targeted_search_filter(bedrooms=3, bed_tolerance=1, required=["helipad"])


# ── 6. Missing night accounting is missing, not invented ──────────────────

def _airroi(pm):
    return {"listing_info": {"listing_name": "Chalet"}, "performance_metrics": pm,
            "property_details": {"bedrooms": 3, "guests": 8, "amenities": []},
            "ratings": {"rating_overall": 4.8, "num_reviews": 30}}


def test_missing_night_fields_stay_none():
    m = map_for_scorer(_airroi({"ttm_revenue": 90000, "ttm_revpar": 200,
                                "ttm_adjusted_occupancy": 0.5}))
    assert m["nights_booked"] is None
    assert m["nights_listed"] is None
    assert m["room_revenue"] is None
    assert m["nightly_rate"] is None


def test_present_night_fields_still_map():
    m = map_for_scorer(_airroi({"ttm_total_days": 365, "ttm_blocked_days": 15,
                                "ttm_days_reserved": 180, "ttm_revpar": 200}))
    assert (m["nights_booked"], m["nights_listed"]) == (180, 350)
    assert m["room_revenue"] == pytest.approx(73000)


def test_scorer_awards_no_night_points_for_unknown_nights():
    subject = {"title": "Cabin", "bedrooms": 3, "max_guests": 8, "adr": 300}
    signals = detect_subject_signals(subject)
    scored = score_comp(_comp(nights_booked=None, nights_listed=None), signals, 300, 3, 8, subject)
    assert not scored["hard_fail"]
    lines = " ".join(scored["score_breakdown"])
    assert "Open year-round" not in lines and "Open most of the year" not in lines
    assert "Barely booked" not in lines


# ── 7. Water: a *front word naming a place is not a waterfront stay ───────

@pytest.mark.parametrize("name", [
    "Waterfront Park Studio steps away",
    "Riverfront District Loft",
    "Harbourfront Market Suite",
    "Downtown near Waterfront Plaza",
    "Lakefront Trail Cabin",
    "Waterfront Street Bungalow",
])
def test_front_word_naming_a_place_is_not_on_water(name):
    assert cf.classify_water_proximity(name, "", AIRROI_LIST) == cf.WATER_INLAND


@pytest.mark.parametrize("name", ["Oceanfront Villa", "Waterfront Cottage with dock",
                                  "Beach-front condo"])
def test_real_waterfront_titles_still_on_water(name):
    assert cf.classify_water_proximity(name, "", AIRROI_LIST) == cf.WATER_ON


# ── 8. Card badges obey the same negation guard as the filter ─────────────

def test_negated_sauna_and_ski_text_earn_no_badge():
    labels, _ = _amenities_to_badges([], text_context="No sauna. Not ski-in/ski-out.")
    assert "Sauna" not in labels and "Ski-in/Out" not in labels


def test_real_sauna_and_ski_text_still_earn_badges():
    labels, _ = _amenities_to_badges([], text_context="Private sauna, ski-in/ski-out access.")
    assert "Sauna" in labels and "Ski-in/Out" in labels


def test_negated_view_text_earns_no_badge():
    labels, _ = _amenities_to_badges([], text_context="Sadly, no mountain view from here.")
    assert "Views" not in labels


# ── 9. p75 is nearest-rank, not one rank high ─────────────────────────────

@pytest.mark.parametrize("occs, expected", [
    ([50.0, 60.0, 70.0, 80.0], 0.70),
    ([50.0], 0.50),
    ([46.0, 50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0], 0.70),
])
def test_market_occupancy_ceiling_is_the_nearest_rank_p75(occs, expected):
    assert market_occupancy_ceiling([{"occupancy_pct": o} for o in occs]) == pytest.approx(expected)


# ── 10. Studios, and an unknown subject rate ──────────────────────────────

def test_studio_bedrooms_are_scored_not_treated_as_missing():
    pts, lines = _score_physical_match(0, 2, 0, 2)
    assert pts == 5 and "+3 Exact bedroom match (0BR)" in lines
    pts, _ = _score_physical_match(0, 2, 1, 2)
    assert pts == 3        # +1 close bedroom, +2 exact guests


def test_targeted_filter_for_a_studio_searches_from_zero_bedrooms():
    assert sim.targeted_search_filter(bedrooms=0, bed_tolerance=1)["bedrooms"]["range"] == [0, 1]
    assert sim.targeted_search_filter(bedrooms=1, bed_tolerance=1)["bedrooms"]["range"] == [1, 2]


def test_unknown_subject_rate_does_not_favour_the_cheapest_comp():
    """Identical score and distance; the old gap was rate / 1 with no subject
    rate, so the $90 comp beat the $900 one with more reviews."""
    subject = {"title": "Cabin", "bedrooms": 3, "max_guests": 8,
               "latitude": 50.88, "longitude": -119.89}
    cheap = _comp(name="Cheap", nightly_rate=90.0, reviews=25)
    pricey = _comp(name="Pricey", nightly_rate=900.0, reviews=45)
    result = rank_comps(subject, [cheap, pricey], top_n=2)
    assert result["subject_adr"] == 0
    assert cheap["score"] == pricey["score"]
    assert [c["name"] for c in result["selected"]] == ["Pricey", "Cheap"]


# ── 11. Village text: negation is judged where the word matched ───────────

def test_village_negated_first_mention_does_not_veto_a_later_real_one():
    signals = {"village": True, "quality_tier": "unknown", "features_readable": True}
    pts, lines = _score_amenity_match(
        "there is no village shuttle. steps from village shops and the lift",
        signals, comp_amenities=[])
    assert "+1 Village/central match" in lines, lines


def test_village_only_negated_mention_scores_nothing():
    signals = {"village": True, "quality_tier": "unknown", "features_readable": True}
    pts, lines = _score_amenity_match("there is no village access at all", signals,
                                      comp_amenities=[])
    assert not any("Village" in l for l in lines), lines


# ── 12. Subject rate per booked night never falls back to adr ─────────────

def test_revenue_per_booked_night_is_zero_not_adr_without_bookings():
    sp = SubjectPerformance(annual_revenue=0, adr=500.0, nights_booked=0)
    assert sp.revenue_per_booked_night == 0.0
    sp = SubjectPerformance(annual_revenue=90000, adr=500.0, nights_booked=200)
    assert sp.revenue_per_booked_night == pytest.approx(450.0)


# ── 13. First failing gate is the reported reason ─────────────────────────

def test_first_failing_gate_wins_the_reason():
    subject = {"title": "Cabin", "bedrooms": 3, "max_guests": 8, "adr": 300}
    signals = detect_subject_signals(subject)
    both = _comp(bedrooms=9, sleeps=30)
    scored = score_comp(both, signals, 300, 3, 8, subject)
    assert scored["hard_fail_reason"].startswith("Bedroom mismatch")

    lux = {"title": "Luxury chalet", "bedrooms": 3, "max_guests": 8, "adr": 800}
    scored = score_comp(_comp(bedrooms=9, nightly_rate=100.0), detect_subject_signals(lux),
                        800, 3, 8, lux)
    assert scored["hard_fail_reason"].startswith("Luxury subject")


def test_dead_code_is_gone():
    import comp_scorer
    assert not hasattr(comp_scorer, "_parse_currency")
    assert not hasattr(comp_scorer, "PROFESSIONAL_MGMT_KEYWORDS")
    assert "professional_mgmt" not in detect_subject_signals({"title": "x"})
    assert "needs_more_comps" not in rank_comps({"title": "x"}, [], top_n=6)


def test_batch_mapping_keeps_unknown_nights_unknown():
    mapped = map_batch_for_scorer([_airroi({"ttm_revenue": 90000, "ttm_revpar": 200})])
    assert mapped[0]["nights_listed"] is None
    assert mapped[0]["revenue_potential"] is None
