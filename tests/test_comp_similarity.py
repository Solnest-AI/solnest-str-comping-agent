"""The scorer compares everything AirROI reports, in both directions.

Found 2026-09-26 on Sunburst (airbnb 1523418129850069170): a detached log
cabin in Sun Peaks with no ski access was comped against six ski-in/ski-out
townhomes. Nothing compared ski access (only pool and hot tub), property type,
bathrooms, or the ~130 other amenity strings, and a comp was never marked
down for a feature the subject lacks.
"""

from __future__ import annotations

import comp_filters
import comp_similarity as sim
from comp_scorer import detect_subject_signals, rank_comps, score_comp


# ── Property type ─────────────────────────────────────────────────────────

def test_property_classes():
    assert sim.property_class("Entire cabin") == "detached"
    assert sim.property_class("Entire home") == "detached"
    assert sim.property_class("Entire chalet") == "detached"
    assert sim.property_class("Entire townhouse") == "attached"   # contains "house"
    assert sim.property_class("Entire condo") == "attached"
    assert sim.property_class("Entire rental unit") == "attached"
    assert sim.property_class("Room in boutique hotel") == "room"
    assert sim.property_class("Private room in home") == "room"
    assert sim.property_class("Property") is None                 # the unknown default
    assert sim.property_class(None) is None


def test_property_type_points():
    assert sim.score_property_type("Entire cabin", "Entire cabin")[0] == 2
    assert sim.score_property_type("Entire cabin", "Entire home")[0] == 1
    pts, lines = sim.score_property_type("Entire cabin", "Entire townhouse")
    assert pts == -3 and "townhouse" in lines[0] and "cabin" in lines[0]
    assert sim.score_property_type("Property", "Entire condo") == (0, [])


# ── Bathrooms and minimum stay ────────────────────────────────────────────

def test_bathrooms():
    assert sim.score_bathrooms(2.5, 3)[0] == 1
    assert sim.score_bathrooms(2.5, 3.5)[0] == 0
    assert sim.score_bathrooms(2, 4)[0] == -1
    assert sim.score_bathrooms(0, 3) == (0, [])        # subject unknown


def test_min_nights():
    assert sim.score_min_nights(2, 11)[0] == -2       # the Sun Peaks 11-night comp
    assert sim.score_min_nights(2, 3)[0] == 0
    assert sim.score_min_nights(None, 11)[0] == 0     # unknown subject: only monthly-style
    assert sim.score_min_nights(None, 30)[0] == -2
    assert sim.score_min_nights(2, None) == (0, [])


# ── Every other amenity, weighted by rarity ───────────────────────────────

def test_rare_amenities_weigh_more_than_universal_ones():
    assert sim.amenity_weight("Wifi") < 0.1
    assert sim.amenity_weight("Kayak") > 2.5
    assert sim.amenity_weight("Some amenity nobody has seen") == 3.0


def test_overlap_rewards_sharing_rare_amenities():
    subject = ["Wifi", "Kitchen", "Kayak", "Boat slip"]
    shares_rare = ["Wifi", "Kitchen", "Kayak", "Boat slip"]
    shares_common = ["Wifi", "Kitchen", "Hangers", "Iron"]
    assert sim.amenity_overlap(subject, shares_rare) == 1.0
    assert sim.amenity_overlap(subject, shares_common) < 0.2


def test_overlap_ignores_premium_features_and_empty_lists():
    # Premium features are scored separately; counting them here would double up.
    assert sim.amenity_overlap(["Hot tub", "Kayak"], ["Kayak"]) == 1.0
    assert sim.amenity_overlap([], ["Kayak"]) is None


def test_overlap_points_are_relative_to_the_pool():
    assert sim.score_overlap(0.80, 0.50)[0] == 2
    assert sim.score_overlap(0.20, 0.50)[0] == -2
    assert sim.score_overlap(0.95, 0.10)[0] == 3                # capped
    assert sim.score_overlap(None, 0.5) == (0, [])


# ── Premium features, both directions ─────────────────────────────────────

def test_premium_mismatch_costs_points_both_ways():
    sub = {"ski_in_out": False, "hot_tub": True}
    pts, lines = sim.score_premium(sub, {"ski_in_out"}, {"ski_in_out"})
    assert pts == -3 - 2
    assert "-3 Has ski-in/ski-out access, which the subject lacks" in lines
    assert "-2 Lacks a hot tub, which the subject has" in lines


def test_description_mention_counts_as_carrying_a_feature_the_subject_lacks():
    comp = {"name": "Condo", "description": "Heated outdoor pool open all year",
            "amenities_raw": ["Wifi"]}
    has, mentions = sim.comp_premium(comp)
    assert "pool" not in has and "pool" in mentions


# ── The Sunburst case, end to end through the scorer ──────────────────────

SUNBURST = {
    "title": "Sleeps 12 Log Cabin | Bike Park",
    "description": "Detached 4-bed log cabin, private forest-edge hot tub. Lift Bike "
                   "Park and chairlift 2 min, village 3 min. Pets welcome.",
    "amenities": ["Wifi", "Kitchen", "Hot tub", "Pets allowed", "Indoor fireplace",
                  "Board games", "Hammock", "BBQ grill"],
    "property_type": "Entire cabin", "bedrooms": 4, "bathrooms": 2.5, "max_guests": 12,
    "min_nights": 2, "adr": 800, "latitude": 50.88, "longitude": -119.89,
}


def _comp(name, listing_type, amenities, **kw):
    base = {"name": name, "description": name, "listing_type": listing_type,
            "bedrooms": 4, "bathrooms": 2.5, "sleeps": 12, "nightly_rate": 800,
            "occupancy_pct": 50, "reviews": 60, "rating": 4.9, "nights_booked": 180,
            "nights_listed": 350, "annual_revenue": 150_000, "revenue_potential": 160_000,
            "revpar": 400, "min_nights": 2, "latitude": 50.88, "longitude": -119.89,
            "amenities_raw": amenities}
    base.update(kw)
    return base


def test_sunburst_ranks_a_like_cabin_above_ski_townhomes():
    like = _comp("Log home, hot tub, pets ok", "Entire home",
                 ["Wifi", "Kitchen", "Hot tub", "Pets allowed", "Indoor fireplace", "Hammock"])
    ski_townhome = _comp("Ski-in/Ski-out townhome", "Entire townhouse",
                         ["Wifi", "Kitchen", "Hot tub", "Ski-in/Ski-out"], bathrooms=3.5)
    result = rank_comps(dict(SUNBURST), [ski_townhome, like], top_n=2)
    assert [c["name"] for c in result["selected"]] == [like["name"], ski_townhome["name"]]
    lines = " ".join(ski_townhome["score_breakdown"])
    assert "Has ski-in/ski-out access, which the subject lacks" in lines
    assert "Different kind of property: townhouse" in lines


def test_sunburst_lacks_ski_in_out_and_the_filter_sees_it():
    lacks = comp_filters.detect_lacking_features(
        SUNBURST["title"], SUNBURST["description"], SUNBURST["amenities"])
    assert "ski_in_out" in lacks and "hot_tub" not in lacks


def test_board_games_is_not_a_games_room():
    sig = detect_subject_signals(dict(SUNBURST))
    assert not sig.get("games_room")


def test_an_eleven_night_minimum_is_marked_down():
    sig = detect_subject_signals(dict(SUNBURST))
    long_stay = score_comp(_comp("Monthly", "Entire home", ["Wifi"], min_nights=11),
                           sig, 800, 4, 12, dict(SUNBURST))
    assert any("Long minimum stay (11 nights" in l for l in long_stay["score_breakdown"])


# ── Disclosure only for features that changed the pool ────────────────────

def test_disclosure_skips_features_no_comp_had():
    beach = [{"name": f"Beach condo {i}", "description": "Steps to the sand",
              "amenities_raw": ["Wifi", "Pool"]} for i in range(12)]
    sel = comp_filters.select_comp_pool(beach, lacking_features=["hot_tub", "ski_in_out", "pool"])
    assert sel.lacking_acted == ["pool"]        # no comp had a hot tub or ski access
    assert sel.lacking_relaxed == ["pool"]


# ── Targeted search also fires when too few comps HAVE a required feature ──

def _tub_comp(i, tub):
    return {"name": f"Comp {i}", "description": "House",
            "amenities_raw": ["Wifi", "Hot tub"] if tub else ["Wifi"]}


def test_required_feature_shortfall_triggers_the_targeted_search():
    """Miami, 2026-09-26: 4 of 25 had a hot tub. A hot-tub + pool subject lacks
    nothing, so the lacking trigger alone would never have searched."""
    pool = [_tub_comp(i, tub=i < 4) for i in range(25)]
    short = sim.required_shortfall(pool, ["hot_tub"])
    assert short == ["hot_tub"]
    reasons, by_type = sim.targeted_search_reasons([], pool, None, required_short=short)
    assert reasons == ["too few comparables with a hot tub"] and not by_type


def test_no_search_when_enough_comps_have_the_required_feature():
    pool = [_tub_comp(i, tub=i < 12) for i in range(25)]
    assert sim.required_shortfall(pool, ["hot_tub"]) == []
    assert sim.targeted_search_reasons([], pool, None, required_short=[])[0] == []
