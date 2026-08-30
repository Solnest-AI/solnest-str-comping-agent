"""
Amenity vocabulary guards.

AirROI returns amenities as Title Case DISPLAY STRINGS. 136 distinct strings
appear across the 200-record live corpus and ZERO of them contain an
underscore, so every snake_case key map (`hot_tub`, `indoor_fireplace`,
`free_parking_on_premises`) matched nothing at all: badge Pass 2 never fired
and the skip-list never fired, which is why comp cards advertised "Hair Dryer"
and "Hot Water" as headline features.

Substring matching against that vocabulary is unsafe. Confirmed traps:
    'pool'  -> Pool, Pool table, Pool view
    'fire'  -> Fire extinguisher (near-universal), Fire pit, Fireplace guards
    'beach' -> Beach access, Beach essentials (towels, not beachfront)
    'sauna' -> nothing at all; no sauna amenity exists in the vocabulary
    'spa'   -> space / spacious / workspace

The rule these tests enforce: structured amenity matching is EXACT SET
MEMBERSHIP against the vocabulary. Free text is only for signals with no
vocabulary key, and then only word-boundary matched.

Run: pytest tests/test_amenities.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from _fixtures import all_live_listings, amenity_vocabulary

from adapters.airroi_to_comp import (
    _AMENITY_MAP, _BADGE_PRIORITY, _TEXT_SIGNAL_PATTERNS, _amenities_to_badges,
)
from comp_scorer import (
    AMENITY_VOCAB_SIGNALS, TEXT_ONLY_SIGNALS, _score_amenity_match,
)

VOCAB = amenity_vocabulary()


# ── The vocabulary itself ────────────────────────────────────────────────

def test_vocabulary_matches_the_live_corpus():
    """amenity_vocab.json must be exactly the strings AirROI actually returns."""
    observed = set()
    for listing in all_live_listings():
        observed.update((listing.get("property_details") or {}).get("amenities") or [])
    assert observed == VOCAB, (
        f"only in corpus: {sorted(observed - VOCAB)[:10]}; "
        f"only in vocab file: {sorted(VOCAB - observed)[:10]}"
    )


def test_vocabulary_contains_no_snake_case():
    """Zero of the 136 real strings contain an underscore."""
    assert [a for a in VOCAB if "_" in a] == []


def test_sauna_is_not_in_the_vocabulary():
    """'sauna' matches NOTHING in the amenity list — it must stay a text signal.

    A vocabulary-based sauna rule would silently never fire.
    """
    assert not any("sauna" in a.lower() for a in VOCAB)
    assert "sauna" in TEXT_ONLY_SIGNALS


# ── (e) every key we map must be a real vocabulary member ────────────────

def test_amenity_map_keys_are_vocabulary_members():
    """_AMENITY_MAP keys are matched by EXACT membership, so a typo = dead rule."""
    unknown = sorted(set(_AMENITY_MAP) - VOCAB)
    assert unknown == [], f"not real AirROI amenity strings: {unknown}"


def test_badge_priority_keys_are_vocabulary_members():
    unknown = sorted(set(_BADGE_PRIORITY) - VOCAB)
    assert unknown == [], f"not real AirROI amenity strings: {unknown}"


def test_badge_priority_and_amenity_map_agree():
    """Every prioritized badge must have a label/emoji, or it silently drops."""
    missing = sorted(set(_BADGE_PRIORITY) - set(_AMENITY_MAP))
    assert missing == [], f"in _BADGE_PRIORITY with no _AMENITY_MAP entry: {missing}"


def test_combined_amenity_keys_are_a_subset_of_the_vocabulary():
    """The headline assertion: map ∪ badges ⊆ amenity_vocab.json."""
    combined = set(_AMENITY_MAP) | set(_BADGE_PRIORITY)
    assert combined <= VOCAB, sorted(combined - VOCAB)


def test_every_scorer_signal_resolves_to_at_least_one_vocabulary_member():
    """A signal whose members are all fictional can never score. Silent no-op."""
    dead = {}
    for signal, members in AMENITY_VOCAB_SIGNALS.items():
        resolved = [m for m in members if m in VOCAB]
        if not resolved:
            dead[signal] = list(members)
    assert dead == {}, f"signals that can never fire: {dead}"


def test_scorer_signals_are_exercised_by_the_live_corpus():
    """Beyond being spelled right, each signal must actually occur in the wild."""
    observed = set()
    for listing in all_live_listings():
        observed.update((listing.get("property_details") or {}).get("amenities") or [])
    never_seen = {
        s: list(m) for s, m in AMENITY_VOCAB_SIGNALS.items()
        if not (set(m) & observed)
    }
    assert never_seen == {}, f"signals no live listing can trigger: {never_seen}"


# ── (f) negative matching — the substring traps ──────────────────────────

_POOL_SUBJECT = {"pool": True, "quality_tier": "unknown"}
_FIRE_SUBJECT = {"fireplace": True, "quality_tier": "unknown"}


def test_pool_table_is_not_a_pool_for_scoring():
    """A cabin with a billiards table must not score as a cabin with a pool."""
    pts, lines = _score_amenity_match(
        text="cozy cabin with a pool table in the basement",
        subject_signals=_POOL_SUBJECT,
        comp_amenities=["Pool table", "Fire extinguisher"],
    )
    assert pts == 0, lines
    assert not any("Pool match" in l for l in lines), lines


def test_pool_view_is_not_a_pool_for_scoring():
    """'Pool view' means you can SEE a pool, not that you have one."""
    pts, lines = _score_amenity_match(
        text="condo with pool view",
        subject_signals=_POOL_SUBJECT,
        comp_amenities=["Pool view"],
    )
    assert not any("Pool match" in l for l in lines), lines


def test_a_real_pool_still_scores():
    """The negative guards must not have disabled the positive case."""
    pts, lines = _score_amenity_match(
        text="villa with private pool",
        subject_signals=_POOL_SUBJECT,
        comp_amenities=["Pool"],
    )
    assert pts == 2 and any("Pool match" in l for l in lines), lines


def test_pool_table_yields_a_games_room_badge_not_a_pool_badge():
    labels, _ = _amenities_to_badges(
        amenity_list=["Pool table"], limit=3, text_context="Cabin with a pool table",
    )
    assert "Pool" not in labels, labels
    assert labels == ["Games Room"], labels


def test_fire_extinguisher_is_not_a_fireplace():
    """'Fire extinguisher' is near-universal. Substring 'fire' made it a hearth."""
    assert "Fire extinguisher" in VOCAB
    pts, lines = _score_amenity_match(
        text="safe home with a fire extinguisher and smoke alarm",
        subject_signals=_FIRE_SUBJECT,
        comp_amenities=["Fire extinguisher", "Fireplace guards"],
    )
    assert pts == 0, lines
    assert not any("Fireplace" in l for l in lines), lines

    labels, _ = _amenities_to_badges(
        amenity_list=["Fire extinguisher", "Fireplace guards"], limit=3,
        text_context="Safe home with a fire extinguisher",
    )
    assert "Fireplace" not in labels and "Fire Pit" not in labels, labels


def test_a_real_fireplace_still_scores():
    pts, lines = _score_amenity_match(
        text="chalet with an indoor fireplace",
        subject_signals=_FIRE_SUBJECT,
        comp_amenities=["Indoor fireplace", "Fire extinguisher"],
    )
    assert pts == 1 and any("Fireplace match" in l for l in lines), lines


def test_beach_essentials_is_not_beach_access():
    """'Beach essentials' is a bag of towels, not beachfront."""
    assert {"Beach essentials", "Beach access"} <= VOCAB
    assert "Beach essentials" not in AMENITY_VOCAB_SIGNALS["water"]
    assert "Beach essentials" not in _AMENITY_MAP
    labels, _ = _amenities_to_badges(
        amenity_list=["Beach essentials"], limit=3, text_context="Cottage near the coast",
    )
    assert "Beach Access" not in labels, labels


def test_spacious_does_not_trigger_a_wellness_or_sauna_badge():
    """The bare 'spa' token matched space / spacious / workspace.

    It stamped a wellness badge on 151 of 200 cards. Text signals are now
    word-boundary matched.
    """
    labels, _ = _amenities_to_badges(
        amenity_list=[], limit=3,
        text_context="A spacious retreat with a dedicated workspace. "
                     "Enjoy the space</b><br> and the view.",
    )
    assert labels == [], labels
    # And there is no live "Wellness" text signal left to reintroduce it.
    assert "Wellness" not in {label for _kws, label, _emoji in _TEXT_SIGNAL_PATTERNS}


def test_a_real_sauna_still_scores_from_text():
    pts, lines = _score_amenity_match(
        text="chalet with a private sauna off the master suite",
        subject_signals={"sauna": True, "quality_tier": "unknown"},
        comp_amenities=[],
    )
    assert pts == 2 and any("Sauna match" in l for l in lines), lines


def test_negated_amenity_text_does_not_score():
    """"we do not have a gym, pool nor roof deck" used to award +2 Pool match."""
    pts, lines = _score_amenity_match(
        text="please note we do not have a sauna on site",
        subject_signals={"sauna": True, "quality_tier": "unknown"},
        comp_amenities=[],
    )
    assert pts == 0, lines


# ── Badges on real data ──────────────────────────────────────────────────

def test_no_live_comp_earns_a_toiletry_badge():
    """Badges are an allowlist. Nothing outside _AMENITY_MAP may reach a card."""
    allowed = {label for _kw, label, _emoji in _AMENITY_MAP.values()}
    allowed |= {label for _kws, label, _emoji in _TEXT_SIGNAL_PATTERNS}
    offenders = []
    for listing in all_live_listings():
        li = listing.get("listing_info") or {}
        labels, emojis = _amenities_to_badges(
            (listing.get("property_details") or {}).get("amenities") or [],
            text_context=f"{li.get('listing_name', '')} {li.get('description', '')}",
        )
        assert len(labels) == len(emojis)
        for label in labels:
            if label not in allowed:
                offenders.append((li.get("listing_id"), label))
    assert offenders == [], offenders[:10]


@pytest.mark.parametrize("banned", ["Shampoo", "Hair Dryer", "Hot Water", "Bathtub", "Iron"])
def test_specific_junk_badges_never_appear_on_live_data(banned):
    for listing in all_live_listings():
        li = listing.get("listing_info") or {}
        labels, _ = _amenities_to_badges(
            (listing.get("property_details") or {}).get("amenities") or [],
            text_context=f"{li.get('listing_name', '')} {li.get('description', '')}",
        )
        assert banned not in labels, (li.get("listing_id"), labels)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
