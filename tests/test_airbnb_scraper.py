"""The Airbnb HTML fallback runs for every listing AirROI has not indexed.

Found live on 2026-09-21 with airbnb.ca/rooms/1754671150601227373, a Langley
farm stay whose og:title reads "Farm stay in Langley · ★New · 6 bedrooms ·
8 beds · 4.5 baths" and whose og:description reads "... · Sleeps 13":

  * max_guests came back 8: the page-body "most common '(N) guests'" heuristic
    landed on the BED count, and the two comps that actually resemble the
    property (sleeps 14 and 16) were disqualified against it.
  * property_type came back "Luxury Chalet", the PropertyBasics DEFAULT,
    because the scraper parsed the type out of og:title and threw it away.
    That word armed comp_scorer's luxury hard-gate on a listing with zero
    luxury words, and 24 of 24 candidates failed.

Hermetic: the parse step is exercised on an inline page, no network.
"""

from __future__ import annotations

from adapters.airroi_to_comp import subject_for_scorer
from comp_scorer import detect_subject_signals
from schema import PropertyBasics
from scrapers.airbnb import parse_airbnb_html

URL = "https://www.airbnb.ca/rooms/1754671150601227373"

# The strings the live page carried, at the counts it carried them.
PAGE = """<html><head>
<meta property="og:title" content="Farm stay in Langley · ★New · 6 bedrooms · 8 beds · 4.5 baths">
<meta property="og:description" content="66 Acres · Barn Lounge · Fire Pits · Sleeps 13">
<meta property="og:image" content="https://a0.muscache.com/im/pictures/hosting/x/original.jpeg">
</head><body>
<script>{"lat":49.1334,"lng":-122.5281}</script>
<h1>66 Acres · Barn Lounge · Fire Pits · Sleeps 13</h1>
<p>8 guests</p><p>8 guests</p><p>8 guests</p><p>13 guests</p><p>13 guests</p>
<p>8 beds</p><p>8 beds</p><p>8 beds</p><p>8 beds</p>
<p>Superhost</p>
</body></html>"""


def test_sleeps_n_in_the_listing_copy_beats_the_page_body_guess():
    prop = parse_airbnb_html(PAGE, URL)
    assert prop.max_guests == 13
    assert prop.bedrooms == 6
    assert prop.bathrooms == 4.5


def test_property_type_is_read_from_og_title_not_defaulted():
    prop = parse_airbnb_html(PAGE, URL)
    assert prop.property_type == "Farm stay"


def test_scraped_farm_stay_does_not_arm_the_luxury_gate():
    prop = parse_airbnb_html(PAGE, URL)
    signals = detect_subject_signals(subject_for_scorer(prop, {"average_daily_rate": 627}))
    assert signals["luxury"] is False


def test_schema_default_type_is_not_a_luxury_claim():
    """Nothing that reaches the scorer may carry a marketing adjective the
    listing never made. The old default was 'Luxury Chalet'."""
    prop = PropertyBasics(address="x", short_address="x", market="x",
                          bedrooms=3, bathrooms=2, max_guests=6)
    signals = detect_subject_signals(subject_for_scorer(prop, {"average_daily_rate": 300}))
    assert signals["luxury"] is False


def test_page_body_guess_still_runs_when_the_copy_has_no_sleeps():
    page = PAGE.replace("Sleeps 13", "Big place")
    prop = parse_airbnb_html(page, URL)
    assert prop.max_guests == 8, "the old heuristic is the fallback, not gone"
