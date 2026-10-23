"""STR permitting pre-check — flags markets where short-term rentals are
restricted or prohibited BEFORE the expensive comp pipeline runs.

Every entry here is hand-verified against the governing bylaw/legislation with a
source URL. Anything not in the table returns an honest "unknown — verify"
verdict, so the agent never silently implies a market is STR-legal.

Verified: 2026-06-08.
"""

from __future__ import annotations

from schema import PermittingVerdict

# Applies to any BC/Canada location.
_BC_REGISTRY_NOTE = (
    "BC: provincial STR registration is mandatory (since May 1, 2025), and the "
    "registry number must be displayed on every listing."
)

# Ordered most-specific first. Matching is plain substring on the lowercased
# "<address> <market>" string, so list precise signals (e.g. road/lake names)
# ahead of broader ones (e.g. municipality names).
_RULES: list[dict] = [
    {
        # AQUA Waterfront Residences — 3699 Capozzi Road, Kelowna. The
        # development is purpose-built and marketed with short-term rental as a
        # permitted use (zoned for nightly rental; not subject to the strata/
        # principal-residence blocks that affect ordinary Kelowna condos).
        "match": ["capozzi"],
        "verdict": PermittingVerdict(
            status="permitted",
            jurisdiction="AQUA Waterfront Residences, Kelowna, BC",
            headline="Short-term rental is a permitted use at AQUA.",
            details=[
                "AQUA (3699 Capozzi Road) is zoned and marketed for nightly "
                "short-term rental; the building permits STR operation, so the "
                "Kelowna principal-residence restriction on ordinary condos does "
                "not block a non-resident owner here.",
            ],
            source_url="https://www.oakwyn.com/properties/3699-capozzi-road-unit-ph3-kelowna-british-columbia-v1w0g3",
            registry_note=_BC_REGISTRY_NOTE,
            verify_note="Register the unit under the BC STR registry and confirm the strata's nightly-rental terms.",
        ),
    },
    {
        # Sun Peaks Mountain Resort Municipality — a purpose-built ski resort
        # where nightly visitor accommodation is an established, permitted use.
        "match": ["sun peaks"],
        "verdict": PermittingVerdict(
            status="permitted",
            jurisdiction="Sun Peaks Mountain Resort Municipality, BC",
            headline="Short-term rental is an established, permitted use at Sun Peaks.",
            details=[
                "Sun Peaks is a mountain resort municipality built around nightly "
                "visitor accommodation; ski-in/ski-out and village strata units are "
                "routinely operated as short-term rentals.",
                "As a designated resort region, Sun Peaks is exempt from BC's "
                "provincial principal-residence requirement, so non-resident owners "
                "are not blocked provincially.",
            ],
            source_url="https://www.sunpeaksmunicipality.ca/",
            registry_note=_BC_REGISTRY_NOTE,
            verify_note="Confirm the specific strata's bylaws permit nightly rental and register the unit under the BC STR registry.",
        ),
    },
    {
        # Cosens Bay / Kalamalka Lake east shore — RDNO Electoral Area "B",
        # frequently mislabeled "Coldstream" by mailing city.
        "match": ["cosens bay", "rdno", "electoral area", "north okanagan regional"],
        "verdict": PermittingVerdict(
            status="restricted",
            jurisdiction="RDNO Electoral Area (North Okanagan, unincorporated)",
            headline="RDNO zoning has no whole-home short-term-rental use.",
            details=[
                "RDNO Zoning Bylaw No. 3000 defines no 'short-term/vacation rental' use. "
                "The only nightly accommodation uses are owner-resident Bed & Breakfast "
                "(rooms + breakfast, operator lives on-site) and ALR agri-tourism.",
                "A non-resident, whole-home rental is therefore not a permitted use "
                "as-of-right.",
                "Electoral areas are exempt from the provincial principal-residence "
                "requirement, so a non-resident host is not blocked provincially. "
                "Local zoning must still permit the use, however, and it does not.",
                "Possible path: a Temporary Use Permit via the Electoral Area OCP. "
                "Confirm with RDNO Planning (250-550-3700 / planning@rdno.ca).",
            ],
            source_url="https://www.rdno.ca/regional-government/bylaws/zoning-bylaw-no-3000",
            registry_note=_BC_REGISTRY_NOTE,
            verify_note="Confirm the parcel is RDNO (not an adjacent municipality) via BC Assessment.",
        ),
    },
    {
        "match": ["coldstream"],
        "verdict": PermittingVerdict(
            status="prohibited",
            jurisdiction="District of Coldstream, BC",
            headline="Whole-home STRs are prohibited in Coldstream.",
            details=[
                "The only permitted STR format is an owner-occupied Bed & Breakfast on "
                "RBB (Residential Bed and Breakfast)-zoned land, run by the registered "
                "owner from their principal residence, with a District business licence.",
                "Coldstream is an incorporated municipality (pop. >10k), so the "
                "provincial principal-residence requirement also applies.",
                "Boundary caveat: lakeshore parcels near Cosens Bay / Kalamalka Lake are "
                "often RDNO Electoral Area B (different rules) even though mail reads "
                "'Coldstream'. Verify the exact parcel's jurisdiction.",
            ],
            source_url="https://www.coldstream.ca/node/725",
            registry_note=_BC_REGISTRY_NOTE,
            verify_note="If the parcel is actually RDNO Electoral Area B, the RDNO rules apply instead.",
        ),
    },
    {
        # Township of Langley + City of Langley — both incorporated, both well
        # over 10k population, so the provincial principal-residence requirement
        # applies. Large Township parcels are usually ALR, which layers ALC
        # rules on top (and opens the agri-tourism path). Verified 2026-08-11.
        "match": ["langley"],
        "verdict": PermittingVerdict(
            status="restricted",
            jurisdiction="Township / City of Langley, BC",
            headline="Whole-home STR is not permitted: the principal-residence rule applies, and ALR parcels add ALC limits.",
            details=[
                "Langley is an incorporated municipality well over 10,000 population and "
                "is NOT on the provincial exemption list, so BC's principal-residence "
                "requirement applies: short-term rental is limited to the operator's "
                "principal residence plus one secondary suite or laneway/garden home. A "
                "non-resident, whole-home rental is not permitted.",
                "If the parcel is in the Agricultural Land Reserve (most large-acreage "
                "Langley properties are), ALC rules apply on top: renting the entire "
                "principal residence for short stays is NOT permitted outright and "
                "requires ALC approval.",
                "Owner-occupied bed & breakfast is allowed on ALR land without ALC "
                "approval, but is capped at four bedrooms in total (including any "
                "bedrooms in a secondary suite), with no guest staying more than 30 "
                "consecutive days.",
                "Agri-tourism accommodation is the one path that supports a larger "
                "operation: it permits up to 10 sleeping units, but ONLY where the land "
                "is classified 'farm' under the Assessment Act and genuine agri-tourism "
                "is actively operating. Critically, lodging on farm-class land is exempt "
                "from the provincial principal-residence requirement.",
                "Township zoning (Bylaw No. 2500) and a Township business licence must "
                "also permit the use, since local government can restrict further than the ALC.",
            ],
            source_url="https://www.alc.gov.bc.ca/use-of-alr-land/housing-in-the-alr/",
            registry_note=_BC_REGISTRY_NOTE,
            verify_note=(
                "Confirm the parcel's ALR status and 'farm' classification via BC "
                "Assessment, then confirm the agri-tourism accommodation path with the "
                "ALC (604-660-7000) and Township of Langley Planning."
            ),
        ),
    },
]

_BC_TOKENS = ("british columbia", " bc", ",bc", "bc,", "bc ", "canada")


def check_str_permitting(address: str = "", market: str = "") -> PermittingVerdict:
    """Return a permitting verdict for the subject location.

    Pure offline text match against the resolved address + market. Anything not
    in the verified table returns an 'unknown — verify' verdict (never a false
    'permitted').
    """
    haystack = f"{address} {market}".lower()

    for rule in _RULES:
        if any(sub in haystack for sub in rule["match"]):
            return rule["verdict"].model_copy(deep=True)

    is_bc = any(tok in haystack for tok in _BC_TOKENS)
    return PermittingVerdict(
        status="unknown",
        jurisdiction=(market or address or "this market").strip(),
        headline="STR rules for this market are not verified.",
        details=[
            "Confirm whole-home short-term rental is a permitted use with the local "
            "government (municipal / regional-district zoning + any business licence) "
            "before relying on this projection.",
            "In BC, incorporated municipalities over 10,000 population are subject to "
            "the provincial principal-residence requirement unless specifically exempt.",
        ],
        source_url=(
            "https://www2.gov.bc.ca/gov/content/housing-tenancy/short-term-rentals"
            if is_bc else ""
        ),
        registry_note=_BC_REGISTRY_NOTE if is_bc else "",
    )
