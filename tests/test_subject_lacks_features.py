"""Comps must not carry premium features the subject does not have.

Found 2026-09-25 on 1131 Tanrac Trl, Gatlinburg (for sale, no Airbnb listing):
all 6 comps had a hot tub and 5 of 6 a pool, while the house's own listing
names neither (its features: screened-in porch, lower deck, seasonal views,
basement suite, whole-home generator, no HOA). The feature gate only ever ran
one way, requiring comps to have what the subject has, and an address subject
arrived with zero amenities, so the calculator's rate came straight from six
hot-tub cabins.

The rule: a feature the subject LACKS is one the same negation-guarded check
that decides "has" says it does not have, and it is only trusted when there is
something to read (an AirROI-shaped amenity list, a real description, or a
scraped features list). Comps with it are dropped when enough remain, and
scored down otherwise. Either way the report says which.
"""

from __future__ import annotations

import pytest

from comp_filters import detect_lacking_features, select_comp_pool
from comp_scorer import detect_subject_signals, score_comp
from generators.methodology import build_methodology
from schema import CompProperty, PropertyBasics
from scrapers.property_search import _parse_firecrawl_result

GATLINBURG_DESCRIPTION = (
    "Awesome quiet Gatlinburg retreat perfect for short-term rental or permanent "
    "residence with a rare 4.5% assumable mortgage! All kitchen appliances and whole "
    "home generator convey to buyer! This well-maintained home features: new roof "
    "installed in May, LVP flooring upstairs, paint, and updated walk-in shower! "
    "Additional features include: main house upstairs with a basement mother-in-law "
    "suite accessible from inside or via private exterior entrance and driveway! "
    "Outside enjoy screened-in porch, lower deck, and seasonal views of Mount Le Conte! "
    "Located minutes from downtown Gatlinburg with NO HOA, call us today to schedule "
    "your private showing and start earning rental income before someone else does!"
)
AIRROI_BASICS = ["Wifi", "Kitchen", "TV", "Heating", "Washer", "Dryer", "Air conditioning"]


# ── Detecting what the subject lacks ──

def test_gatlinburg_listing_lacks_pool_and_hot_tub():
    assert detect_lacking_features("House for sale", GATLINBURG_DESCRIPTION, []) == ["pool", "hot_tub"]


def test_airroi_amenity_list_silence_is_a_no():
    assert detect_lacking_features("Cabin", "", AIRROI_BASICS + ["Pool"]) == ["hot_tub"]


def test_nothing_to_read_means_unknown_not_lacking():
    assert detect_lacking_features("House for sale", "", []) == []
    assert detect_lacking_features("House for sale", "Nice house.", []) == []


def test_a_mentioned_hot_tub_is_not_lacking():
    desc = GATLINBURG_DESCRIPTION + " Relax in the private hot tub on the deck!"
    assert detect_lacking_features("House", desc, []) == ["pool"]


def test_scraped_features_list_counts_as_something_to_read():
    feats = ["Screened-in porch", "Lower deck", "Whole home generator", "Mountain views", "Garage"]
    assert detect_lacking_features("House", "", feats) == ["pool", "hot_tub"]


def test_features_the_operator_requires_are_never_lacking():
    assert detect_lacking_features("House", GATLINBURG_DESCRIPTION, [],
                                   exclude=["hot_tub"]) == ["pool"]


# ── Selecting the pool ──

def _listing(name: str, extra: list[str]) -> dict:
    return {"listing_info": {"listing_id": name, "listing_name": name},
            "property_details": {"amenities": AIRROI_BASICS + extra}}


def _names(comps) -> list[str]:
    return [c["listing_info"]["listing_name"] for c in comps]


def test_comps_with_a_lacking_feature_are_dropped_when_enough_remain():
    """Ten must remain: the scorer and liveness probe cut further, and a pool
    trimmed to exactly six would block the report."""
    pool = [_listing(f"plain{i}", []) for i in range(10)] + \
           [_listing(f"tub{i}", ["Hot tub"]) for i in range(3)]
    sel = select_comp_pool(pool, lacking_features=["hot_tub"])
    assert _names(sel.kept) == [f"plain{i}" for i in range(10)]
    assert [n for n, _ in sel.lacking_dropped] == ["tub0", "tub1", "tub2"]
    assert sel.lacking_relaxed == []


def test_nine_without_is_not_enough_to_drop_the_rest():
    pool = [_listing(f"plain{i}", []) for i in range(9)] + [_listing("tub0", ["Hot tub"])]
    sel = select_comp_pool(pool, lacking_features=["hot_tub"])
    assert len(sel.kept) == 10 and sel.lacking_relaxed == ["hot_tub"]


def test_too_few_without_the_feature_keeps_them_and_says_so():
    pool = [_listing(f"plain{i}", []) for i in range(4)] + \
           [_listing(f"tub{i}", ["Hot tub"]) for i in range(6)]
    sel = select_comp_pool(pool, lacking_features=["hot_tub"])
    assert len(sel.kept) == 10
    assert sel.lacking_relaxed == ["hot_tub"]


def test_operator_exclusions_still_apply_when_lacking_is_relaxed():
    pool = [_listing(f"plain{i}", []) for i in range(4)] + \
           [_listing(f"tub{i}", ["Hot tub"]) for i in range(6)]
    sel = select_comp_pool(pool, lacking_features=["hot_tub"], exclude_terms=["tub5"])
    assert "tub5" not in _names(sel.kept)


def test_each_feature_is_judged_on_its_own():
    """Gatlinburg: 0 of 25 comps lacked a hot tub, 14 lacked a pool. Treating
    the two together meant no comp lacked BOTH, so neither was filtered."""
    pool = ([_listing(f"tub_only{i}", ["Hot tub"]) for i in range(11)]
            + [_listing(f"tub_pool{i}", ["Hot tub", "Pool"]) for i in range(8)])
    sel = select_comp_pool(pool, lacking_features=["pool", "hot_tub"])
    assert _names(sel.kept) == [f"tub_only{i}" for i in range(11)]
    assert {n for n, ex in sel.lacking_dropped} == {f"tub_pool{i}" for i in range(8)}
    assert sel.lacking_relaxed == ["hot_tub"]


# ── Scoring when relaxed: comps without the extra rank first ──

def test_scorer_marks_down_a_comp_with_a_feature_the_subject_lacks():
    subject = {"title": "House", "amenities": [], "bedrooms": 3, "max_guests": 8, "adr": 300}
    base = {"name": "C", "bedrooms": 3, "sleeps": 8, "occupancy_pct": 60, "reviews": 80,
            "rating": 4.8, "nights_booked": 200, "nights_listed": 350, "nightly_rate": 300,
            "annual_revenue": 70_000, "revenue_potential": 90_000, "revpar": 160,
            "amenities_raw": []}
    plain = score_comp(dict(base), detect_subject_signals(subject), 300, 3, 8)
    extra = score_comp(dict(base, extra_features=["hot_tub"]), detect_subject_signals(subject), 300, 3, 8)
    assert extra["score"] < plain["score"]
    assert any("hot tub" in line.lower() for line in extra["score_breakdown"])


# ── Reading the listing's features ──

def test_scraped_features_exclude_none_and_not_listed_entries():
    raw = {"bedrooms": 3, "features": ["Screened-in porch", "Pool: None", "Spa: Not listed",
                                       "Lower deck", "No HOA", "n/a"]}
    out = _parse_firecrawl_result(raw, "https://www.realtor.com/x")
    assert out["features"] == ["Screened-in porch", "Lower deck"]


# ── The report says what it did ──

def _comps(n_with_tub: int) -> list[CompProperty]:
    return [CompProperty(name=f"C{i}", sleeps=8, bedrooms=3, bathrooms=2,
                         feature_badges=(["Hot Tub"] if i < n_with_tub else []))
            for i in range(6)]


def _prop() -> PropertyBasics:
    return PropertyBasics(address="a", short_address="a", market="Gatlinburg",
                          bedrooms=3, bathrooms=3, max_guests=8)


def test_methodology_says_comps_with_it_were_excluded():
    m = build_methodology(_prop(), _comps(0), lacking_features=["pool", "hot_tub"],
                          lacking_relaxed=[])
    text = " ".join(m.comp_criteria).lower()
    assert "no pool or hot tub" in text and "excluded" in text


def test_methodology_says_how_many_comps_still_have_it_when_relaxed():
    m = build_methodology(_prop(), _comps(4), lacking_features=["hot_tub"],
                          lacking_relaxed=["hot_tub"])
    text = " ".join(m.comp_criteria).lower()
    assert "4 of 6" in text and "hot tub" in text


def test_methodology_splits_filtered_and_kept_features():
    m = build_methodology(_prop(), _comps(6), lacking_features=["pool", "hot_tub"],
                          lacking_relaxed=["hot_tub"])
    text = " ".join(m.comp_criteria).lower()
    assert "no pool" in text and "excluded" in text
    assert "6 of 6" in text and "hot tub" in text


def test_methodology_is_silent_when_nothing_is_lacking():
    m = build_methodology(_prop(), _comps(6))
    assert "hot tub" not in " ".join(m.comp_criteria).lower()


# ── A comp's own description counts, not just its amenity checkboxes ──
#
# Found on the first Gatlinburg re-run: "Upscale Cabin w/ Views! HOT TUB" had
# 62 AirROI amenities without "Pool", so the authoritative-list rule read it as
# pool-free, while its description says "+ Outdoor pool (Pool is closed during
# winter season October - March)" and its photo shows the pool. For deciding
# that a comp HAS what the subject lacks, the description counts too.

from comp_filters import comp_mentions_feature  # noqa: E402


def _described(name: str, description: str, extra: list[str] | None = None) -> dict:
    return {"listing_info": {"listing_id": name, "listing_name": name,
                             "description": description},
            "property_details": {"amenities": AIRROI_BASICS + (extra or [])}}


UPSCALE = ("This cabin has been completely renovated. Features include:<br />"
           "+ Outdoor pool (Pool is closed during winter season October - March)<br />"
           "+ Game room")


def test_a_pool_in_the_description_counts_when_the_checkbox_is_missing():
    assert comp_mentions_feature(_described("Upscale Cabin", UPSCALE), "pool") is True


def test_resort_pool_access_counts():
    comp = _described("Luxury Cabin", "Three sleeping areas, resort pool access (seasonal).")
    assert comp_mentions_feature(comp, "pool") is True


@pytest.mark.parametrize("billiards", [
    "relax in the private hot tub, play pool in the game room",
    "Enjoy friendly competition with pool 🎱, air hockey and a multicade",
    "play a game of pool or enjoy the arcade games",
    "shoot pool downstairs",
    "Game room with a pool table and foosball",
])
def test_billiards_is_not_a_pool(billiards):
    assert comp_mentions_feature(_described("Cabin", billiards), "pool") is False


def test_a_described_pool_comp_is_dropped_for_a_subject_without_one():
    pool = ([_listing(f"plain{i}", []) for i in range(10)]
            + [_described("Upscale Cabin", UPSCALE)])
    sel = select_comp_pool(pool, lacking_features=["pool"])
    assert "Upscale Cabin" not in _names(sel.kept)


def test_subject_with_a_described_pool_does_not_lack_one():
    desc = "Brand new cabin with a heated private pool and mountain views. " * 4
    assert "pool" not in detect_lacking_features("Cabin", desc, AIRROI_BASICS)


def test_same_named_listings_are_counted_separately():
    """Nashville, 2026-09-26: ten distinct units titled "Downtown Music City
    Loft living ~Steps to Broadway" collapsed into one entry reading
    "has: pool, pool, pool..." and the log said 2 comps dropped, not 11."""
    twins = [{"listing_info": {"listing_id": f"twin{i}", "listing_name": "Same Loft Name"},
              "property_details": {"amenities": AIRROI_BASICS + ["Pool"]}} for i in range(3)]
    pool = [_listing(f"plain{i}", []) for i in range(10)] + twins
    sel = select_comp_pool(pool, lacking_features=["pool"])
    assert sel.lacking_dropped == [("Same Loft Name", ["pool"])] * 3
