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


def _guess_market_from_text(text: str) -> str:
    """Try to extract a market/city name from listing text."""
    known_markets = [
        # Canada — Ski resorts (BC + AB + ON + QC)
        "Sun Peaks", "Whistler", "Big White", "Silver Star", "Revelstoke",
        "Fernie", "Panorama", "Kicking Horse", "Red Mountain",
        "Canmore", "Banff", "Lake Louise", "Jasper",
        "Blue Mountain", "Mont-Tremblant", "Mont Tremblant", "Mont Sainte-Anne",
        # Canada — Okanagan + Interior BC (lake/wine markets)
        "Kelowna", "West Kelowna", "Lake Country", "Vernon", "Penticton",
        "Peachland", "Summerland", "Naramata", "Osoyoos", "Oliver",
        "Kamloops", "Salmon Arm",
        # Canada — Coastal BC
        "Vancouver", "Squamish", "Victoria", "Tofino", "Ucluelet", "Sidney",
        "Nanaimo", "Parksville", "Sooke",
        # Canada — Other major markets
        "Calgary", "Edmonton", "Toronto", "Niagara-on-the-Lake", "Collingwood",
        "Mont-Sutton",
        # US — Gulf Coast / Florida Panhandle
        "Panama City Beach", "Destin", "Fort Walton Beach", "Pensacola",
        "Gulf Shores", "Orange Beach",
        # US — Florida
        "Kissimmee", "Orlando", "Miami", "Fort Lauderdale", "Key West",
        "Naples", "Sarasota", "Clearwater", "Tampa", "St. Petersburg",
        "Anna Maria Island", "Siesta Key", "Marco Island",
        # US — Southeast Coast
        "Myrtle Beach", "Hilton Head", "Charleston", "Savannah",
        "Virginia Beach", "Tybee Island", "Folly Beach",
        # US — Smoky Mountains / Tennessee
        "Pigeon Forge", "Gatlinburg", "Sevierville", "Nashville", "Memphis",
        # US — Outer Banks / NC Coast
        "Outer Banks", "Duck", "Corolla", "Nags Head", "Kill Devil Hills",
        # US — California
        "San Diego", "Los Angeles", "Palm Springs", "Big Bear",
        "Joshua Tree", "Paso Robles", "Sonoma", "Napa",
        "Mammoth Lakes", "Lake Arrowhead",
        # US — Mountain / Ski
        "Lake Tahoe", "South Lake Tahoe", "Breckenridge", "Vail", "Aspen",
        "Steamboat Springs", "Park City", "Big Sky", "Jackson Hole",
        "Telluride",
        # US — Southwest / Desert
        "Scottsdale", "Sedona", "Flagstaff", "Tucson", "Moab",
        "St. George", "Zion",
        # US — Texas
        "Austin", "San Antonio", "Fredericksburg", "South Padre Island",
        "Galveston",
        # US — Louisiana
        "New Orleans",
        # US — Hawaii
        "Maui", "Kauai", "Honolulu", "Big Island", "Kona", "Waikiki",
        # US — Northeast / New England
        "Martha's Vineyard", "Cape Cod", "Nantucket", "Lake George",
        "Poconos", "Bar Harbor", "Kennebunkport",
        # US — Midwest / Other
        "Branson", "Wisconsin Dells", "Traverse City", "Lake of the Ozarks",
        "Put-in-Bay",
    ]
    for market in known_markets:
        if market.lower() in text.lower():
            return market
    # Fallback: try to find "City, State/Province" pattern
    match = re.search(
        r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*"
        r"(?:BC|AB|ON|QC|MB|NB|NL|NS|NT|NU|PE|SK|YT|"
        r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
        r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|"
        r"SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
        text,
    )
    if match:
        return match.group(1)
    return "Unknown Market"


async def scrape_airbnb_listing(url: str) -> PropertyBasics:
    """Scrape an Airbnb listing page and return property basics.

    Raises ValueError if critical data cannot be extracted.
    """
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

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
    if og_title:
        # Property type + city: "Cabin in Peachland" / "Condo in Sun Peaks Mountain"
        type_city = re.match(r"([A-Za-z\- ]+?)\s+in\s+([^·]+?)\s*·", og_title)
        if type_city and not location_text:
            location_text = type_city.group(2).strip()

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

    # Strategy 3b: Max guests is NOT in og:title — parse the page body for it.
    # Airbnb shows "X guests" prominently; the most common occurrence is the
    # listing's actual capacity.
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

    # Determine market
    market = _guess_market_from_text(location_text or title or description)

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
