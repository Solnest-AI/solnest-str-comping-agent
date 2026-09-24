"""Airbnb listing scraper — extract property data from a listing URL."""

import json
import re

import httpx
from bs4 import BeautifulSoup

from schema import PropertyBasics

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _extract_from_next_data(html: str) -> dict | None:
    """Try to find and parse the __NEXT_DATA__ JSON blob."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            return json.loads(script.string)
        except json.JSONDecodeError:
            pass
    return None


def _extract_from_ld_json(html: str) -> dict | None:
    """Try to find structured data in ld+json script tags."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        if script.string:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") in (
                    "LodgingBusiness",
                    "VacationRental",
                    "Product",
                    "Place",
                ):
                    return data
            except json.JSONDecodeError:
                continue
    return None


def _extract_meta(html: str) -> dict:
    """Extract Open Graph and basic meta tags."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    og_image = soup.find("meta", property="og:image")
    if og_image:
        meta["image"] = og_image.get("content", "")
    og_title = soup.find("meta", property="og:title")
    if og_title:
        meta["title"] = og_title.get("content", "")
    og_desc = soup.find("meta", property="og:description")
    if og_desc:
        meta["description"] = og_desc.get("content", "")
    return meta


# Region tokens that trail a city name in free text. Deliberately duplicated
# from agent.py rather than imported: agent.py imports this module at the top
# of the file, so `from agent import _US_STATES` is a circular import.
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
_CA_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT",
}
_REGION_ABBR = _US_STATES | _CA_PROVINCES

_REGION_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "nova scotia", "northwest territories",
    "nunavut", "ontario", "prince edward island", "quebec", "saskatchewan",
    "yukon",
}

# Curated Canadian resort/leisure markets. These exist to NORMALIZE a messy
# capture ("Sun Peaks Mountain Resort" -> "Sun Peaks"), not to define the
# universe of supported markets — the generic "City, ST" parse below runs
# first precisely so US markets are not all reported as "Unknown Market".
_KNOWN_MARKETS = [
    # Ski resorts (BC + AB + ON + QC)
    "Sun Peaks", "Whistler", "Big White", "Silver Star", "Revelstoke",
    "Fernie", "Panorama", "Kicking Horse", "Red Mountain",
    "Canmore", "Banff", "Lake Louise", "Jasper",
    "Blue Mountain", "Mont-Tremblant", "Mont Tremblant", "Mont Sainte-Anne",
    # Okanagan + Interior BC (lake/wine markets)
    "West Kelowna", "Kelowna", "Lake Country", "Vernon", "Penticton",
    "Peachland", "Summerland", "Naramata", "Osoyoos", "Oliver",
    "Kamloops", "Salmon Arm",
    # Coastal BC
    "North Vancouver", "West Vancouver", "Vancouver", "Squamish", "Victoria",
    "Tofino", "Ucluelet", "Sidney", "Nanaimo", "Parksville", "Sooke",
    # Other major markets
    "Calgary", "Edmonton", "Toronto", "Niagara-on-the-Lake", "Collingwood",
    "Mont-Sutton",
]

# Words that, sitting immediately before a known market name, mean the real
# place is a DIFFERENT one: "Mount Vernon" is not Vernon BC, "North Bay" is
# not Bay. Substring/word-boundary matching cannot see this on its own.
_PLACE_PREFIXES = {
    "mount", "mt", "north", "south", "east", "west", "new", "old", "upper",
    "lower", "little", "big", "grand", "port", "fort", "ft", "lake", "saint",
    "st", "ste", "sainte", "la", "le", "los", "las", "san", "santa",
}
# Same idea trailing the name: "Vernon Hills" (IL) is not Vernon (BC).
_PLACE_SUFFIXES = {
    "hills", "heights", "park", "beach", "city", "township", "falls",
    "springs", "junction", "harbor", "harbour", "island", "ridge", "creek",
    "county", "borough",
}


def _is_region_token(token: str) -> bool:
    t = (token or "").strip().strip(".,")
    if not t:
        return False
    if len(t) == 2 and t.upper() in _REGION_ABBR:
        return True
    return t.lower() in _REGION_NAMES


def _match_known_market(text: str) -> str:
    """Word-boundary match against the curated market list, rejecting hits
    that are only part of a longer place name."""
    if not text:
        return ""
    for market in _KNOWN_MARKETS:
        for m in re.finditer(rf"\b{re.escape(market)}\b", text, re.I):
            before = re.findall(r"[A-Za-z\-']+", text[:m.start()])
            after = re.findall(r"[A-Za-z\-']+", text[m.end():])
            if before and before[-1].lower() in _PLACE_PREFIXES:
                continue    # "Mount Vernon" / "North Vancouver"
            if after and after[0].lower() in _PLACE_SUFFIXES:
                continue    # "Vernon Hills" / "Panorama City"
            return market
    return ""


_GENERIC_LISTING_WORDS = {
    "entire", "private", "shared", "home", "house", "cabin", "condo",
    "chalet", "villa", "apartment", "apt", "cottage", "suite", "room",
    "place", "stay", "rental", "loft", "guesthouse", "bungalow", "tiny",
    "the", "a", "an", "in", "at", "near", "by", "hosted",
}

# Trailing city-name run immediately before a region token.
_CITY_RUN = re.compile(
    r"([A-Z][A-Za-z'\u2019\-\.]*(?:[ \-][A-Z][A-Za-z'\u2019\-\.]*){0,3})\s*,?\s*$"
)
# Region tokens. Abbreviations are matched case-SENSITIVELY: lowercase "in",
# "or", "me", "hi" are ordinary English words, uppercase they are states.
_REGION_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_REGION_ABBR)) + r")\b"
    r"|\b(?:" + "|".join(sorted(_REGION_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _strip_generic_words(city: str) -> str:
    tokens = city.split()
    while tokens and tokens[0].lower().strip(".,") in _GENERIC_LISTING_WORDS:
        tokens.pop(0)
    return " ".join(tokens).strip(" ,.-")


_STREET_SUFFIXES = {
    "drive", "dr", "street", "st", "road", "rd", "avenue", "ave", "lane", "ln",
    "boulevard", "blvd", "court", "ct", "place", "pl", "way", "trail", "trl",
    "circle", "cir", "highway", "hwy", "parkway", "pkwy", "terrace", "loop",
    "station", "house", "lodge", "cabin", "chalet", "villa", "retreat", "resort",
    "inn", "hotel", "suite", "suites", "condo", "apartment", "apt", "unit",
}


def _looks_like_a_place(name: str) -> bool:
    """Reject street addresses and venue names masquerading as markets.

    A fabricated market is worse than "Unknown Market": it flows into
    PropertyBasics.market, into the report copy, AND into the AirROI address
    query, so the whole report gets built for the wrong place.
    """
    if not name:
        return False
    tokens = name.split()
    if tokens and tokens[-1].lower().strip(".") in _STREET_SUFFIXES:
        return False
    # A leading house number means this is a street address, not a city.
    if tokens and tokens[0].rstrip(".").isdigit():
        return False
    return True


def _parse_city_region(text: str) -> str:
    """Generic '<City>, <ST>' / '<City> <Province>' / '<City>, <State name>'
    parse. Runs before the curated list so US markets resolve at all.

    Works backwards from each region token rather than forwards from a city
    candidate, so a street name in front of the city ("812 Ski Mountain Rd,
    Gatlinburg, TN") does not swallow the match.
    """
    if not text:
        return ""
    for m in _REGION_RE.finditer(text):
        token = m.group(0)
        # Two-letter forms must be uppercase to count as a state/province.
        if len(token) == 2 and token != token.upper():
            continue
        head = text[:m.start()]
        run = _CITY_RUN.search(head)
        if not run:
            continue
        city = _strip_generic_words(run.group(1).strip(" ,.-"))
        if city and not _is_region_token(city) and _looks_like_a_place(city):
            return city
    return ""


def _clean_place(text: str) -> str:
    """Reduce a captured place string to a bare city name."""
    if not text:
        return ""
    head = text.split(",")[0].strip(" ,.-\u00b7")
    tokens = head.split()
    while tokens and _is_region_token(tokens[-1]):
        tokens.pop()
    cleaned = " ".join(tokens).strip(" ,.-")
    # Reject obvious non-places (numbers, single letters, over-long captures)
    if not cleaned or len(cleaned) < 2 or len(cleaned.split()) > 5:
        return ""
    if not re.search(r"[A-Za-z]", cleaned):
        return ""
    return cleaned


def _guess_market_from_text(text: str, hint: str = "",
                            allow_prose_parse: bool = True) -> str:
    """Extract a market/city name from listing text.

    Resolution order:
      1. `hint` — the og:title "<Type> in <City>" capture, Airbnb's own city
         label and by far the most reliable signal on the page.
      2. Generic "<City>, <ST>" parse — works for every US state and Canadian
         province, so Gatlinburg/Destin/Scottsdale resolve instead of falling
         through to "Unknown Market".
      3. Curated Canadian resort list, word-boundary matched with prefix and
         suffix guards so "Mount Vernon" no longer resolves to "Vernon".
    """
    if hint:
        known = _match_known_market(hint)
        if known:
            return known          # normalise "Sun Peaks Mountain" -> "Sun Peaks"
        city = _parse_city_region(hint) or _clean_place(hint)
        if city:
            return city

    # Only run the generic "<City>, <ST>" parse over STRUCTURED location text.
    # Over free description prose it produced markets like "Maple Drive",
    # "Union Station" and "Cedar House" -- worse than "Unknown Market", because
    # a fabricated market silently redirects the entire report to another town.
    if allow_prose_parse:
        city = _parse_city_region(text)
        if city:
            known = _match_known_market(city)
            return known or city

    known = _match_known_market(text)
    if known:
        return known

    # Last resort: "<Type> in <City>" at the very start of the string (the
    # og:title / listing-title shape). Anchored so prose cannot trigger it.
    type_city = re.match(r"[A-Za-z\- ]{2,30}?\s+in\s+([^\u00b7,\n]{2,40})", text or "")
    if type_city:
        city = _strip_generic_words(_clean_place(type_city.group(1)))
        # Must read as a proper noun, or prose like "home in the mountains"
        # becomes a market name.
        if city and city[:1].isupper() and _looks_like_a_place(city):
            return _match_known_market(city) or city

    return "Unknown Market"


async def scrape_airbnb_listing(url: str) -> PropertyBasics:
    """Scrape an Airbnb listing page and return property basics.

    Raises ValueError if critical data cannot be extracted.
    """
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    return parse_airbnb_html(html, url)


def parse_airbnb_html(html: str, url: str) -> PropertyBasics:
    """Turn a fetched Airbnb listing page into PropertyBasics. Pure: no network.

    Split from the fetch so the strategies below can be tested against a page
    on disk. Raises ValueError if critical data cannot be extracted.
    """
    # Try multiple extraction strategies
    title = ""
    image_url = ""
    bedrooms = 0
    bathrooms = 0.0
    max_guests = 0
    description = ""
    rating = None
    review_count = None
    location_text = ""
    og_city = ""

    # Strategy 1: __NEXT_DATA__
    next_data = _extract_from_next_data(html)
    if next_data:
        try:
            # Navigate the nested Airbnb data structure
            props = next_data.get("props", {}).get("pageProps", {})
            listing = (
                props.get("listingData", {}).get("listing", {})
                or props.get("listing", {})
            )
            if listing:
                title = listing.get("name", "")
                bedrooms = listing.get("bedrooms", 0)
                bathrooms = listing.get("bathrooms", 0)
                max_guests = listing.get("personCapacity", 0) or listing.get("guestCapacity", 0)
                rating = listing.get("avgRating")
                review_count = listing.get("reviewsCount")
                location_text = listing.get("locationTitle", "") or listing.get("city", "")

                photos = listing.get("photos", [])
                if photos:
                    image_url = photos[0].get("large", "") or photos[0].get("picture", "")
        except (KeyError, TypeError, IndexError):
            pass

    # Strategy 2: ld+json
    if not title:
        ld = _extract_from_ld_json(html)
        if ld:
            title = ld.get("name", "")
            description = ld.get("description", "")
            image_url = image_url or (ld.get("image", [""])[0] if isinstance(ld.get("image"), list) else ld.get("image", ""))
            addr = ld.get("address", {})
            if isinstance(addr, dict):
                location_text = f"{addr.get('addressLocality', '')}, {addr.get('addressRegion', '')}"

    # Strategy 3: Meta tags fallback
    meta = _extract_meta(html)
    image_url = image_url or meta.get("image", "")
    description = description or meta.get("description", "")
    og_title = meta.get("title", "")

    # Strategy 3a: Airbnb's og:title is the most reliable source for the
    # property config. Format is consistent:
    #   "<PropertyType> in <City> · ★<rating> · <N> bedroom · <N> bed · <N> bath"
    # All numbers are present and non-truncated. Always parse this when present.
    property_type = ""
    if og_title:
        # Property type + city: "Cabin in Peachland" / "Condo in Sun Peaks Mountain"
        type_city = re.match(r"([A-Za-z\- ]+?)\s+in\s+([^·]+?)\s*·", og_title)
        if type_city:
            # Airbnb's own type label ("Farm stay", "Cabin", "Condo"). This used
            # to be parsed and discarded, so the subject fell to the schema
            # default and the scorer read that default's adjective as a claim.
            property_type = type_city.group(1).strip()
            # Airbnb's own city label — the most reliable locality on the page.
            og_city = type_city.group(2).strip()
            if not location_text:
                location_text = og_city

        # Rating: "★4.96"
        if rating is None:
            rating_match = re.search(r"★\s*(\d+\.\d+)", og_title)
            if rating_match:
                rating = float(rating_match.group(1))

        # Bedrooms: "1 bedroom" / "3 bedrooms" (treat "studio" as 0/1)
        if not bedrooms:
            bed_match = re.search(r"(\d+)\s*bedroom", og_title, re.I)
            if bed_match:
                bedrooms = int(bed_match.group(1))
            elif "studio" in og_title.lower():
                bedrooms = 0   # studio = 0 bedrooms by convention

        # Bathrooms: "1 private bath" / "1 shared bath" / "1 half-bath" / "2.5 baths"
        # Allow any optional adjective(s) between the number and "bath".
        if not bathrooms:
            bath_match = re.search(
                r"(\d+(?:\.\d+)?)\s+(?:[a-z\-]+\s+){0,2}bath",
                og_title, re.I,
            )
            if bath_match:
                bathrooms = float(bath_match.group(1))

    title = title or og_title

    # Strategy 3b: Max guests is NOT in og:title. First trust the host's own
    # copy: "Sleeps 13" in the title or og:description is the listing's claim
    # about itself. The page-body frequency guess below landed on the BED
    # count on a live page ("8 beds" appeared 12 times, "13 guests" twice).
    if not max_guests:
        for text in (og_title, meta.get("description", ""), title, description):
            sleeps = re.search(r"\bsleeps\s+(\d{1,2})\b", text or "", re.I)
            if sleeps and 1 <= int(sleeps.group(1)) <= 30:
                max_guests = int(sleeps.group(1))
                break
    # Then the page body: the most common "X guests" occurrence.
    if not max_guests:
        guest_matches = re.findall(r"(\d+)\s*guests?\b", html, re.I)
        if guest_matches:
            from collections import Counter
            # Take the modal value of guest counts mentioned (capacity, not "1 guest" reviews)
            counts = Counter(int(g) for g in guest_matches if 1 <= int(g) <= 30)
            if counts:
                max_guests = counts.most_common(1)[0][0]

    # Final fallback: derive max_guests from bedrooms (Airbnb default ratio)
    if not max_guests and bedrooms:
        max_guests = bedrooms * 2 + (2 if bedrooms <= 2 else 0)

    # Strategy 3c: Review count fallback — Airbnb shows "N reviews" in
    # several places on the page. Parse the most common pattern.
    if review_count is None:
        review_matches = re.findall(r"(\d+)\s+reviews?\b", html, re.I)
        if review_matches:
            # Take the largest plausible count (listing-level, not per-section)
            candidates = [int(r) for r in review_matches if 1 <= int(r) <= 5000]
            if candidates:
                review_count = max(candidates)

    # Strategy 3d: Superhost detection — Airbnb marks Superhosts prominently
    is_superhost = bool(re.search(r"superhost", html, re.I))

    # Strategy 3e: Latitude/longitude — Airbnb embeds these in the page even
    # though it hides the exact street address. AirROI accepts lat/lng directly,
    # which is the cleanest geocode path for Airbnb-URL inputs.
    latitude: float | None = None
    longitude: float | None = None
    coord_match = re.search(
        r'"lat"\s*:\s*([-\d.]+)\s*,\s*"lng"\s*:\s*([-\d.]+)', html
    )
    if not coord_match:
        coord_match = re.search(
            r'"latitude"\s*:\s*([-\d.]+)[^}]{0,80}?"longitude"\s*:\s*([-\d.]+)', html
        )
    if coord_match:
        try:
            latitude = float(coord_match.group(1))
            longitude = float(coord_match.group(2))
        except (TypeError, ValueError):
            pass

    # Determine market. The og:title city is passed as the preferred hint —
    # it is Airbnb's own label for the listing's town.
    # location_text and title are STRUCTURED (Airbnb's own location label and
    # listing title). description is free prose, where the generic "<City>, <ST>"
    # parse fabricates markets out of street names and landmarks -- so the
    # generic parse is disabled when we are down to the description.
    _structured = location_text or title
    market = _guess_market_from_text(
        _structured or description,
        hint=og_city,
        allow_prose_parse=bool(_structured),
    )

    # Build address from available info
    address = location_text or market
    short_address = address

    if not title:
        raise ValueError(
            f"Could not extract listing data from {url}. "
            "Airbnb may be blocking automated requests. "
            "Try providing the address manually with --input 'address' --beds N --baths N --guests N"
        )

    return PropertyBasics(
        address=address,
        short_address=short_address,
        market=market,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        max_guests=max_guests,
        property_type=property_type or "Property",
        hero_image_url=image_url,
        airbnb_url=url,
        title=title,
        rating=rating,
        review_count=review_count,
        is_superhost=is_superhost,
        description=description,
        latitude=latitude,
        longitude=longitude,
    )
