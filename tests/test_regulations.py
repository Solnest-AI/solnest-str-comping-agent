"""
STR permitting pre-check tests — guard the regulations lookup.

Key invariants:
  - Verified jurisdictions return the right status (prohibited / restricted).
  - The Cosens Bay / RDNO signal wins over the "Coldstream" mailing city.
  - Unknown markets NEVER return a false 'permitted' — they return 'unknown'.
  - BC locations carry the provincial registry note; non-BC do not.

Run: pytest tests/test_regulations.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from regulations import check_str_permitting


def test_coldstream_is_prohibited():
    v = check_str_permitting("5853 Cosens Bay Rd, Coldstream, BC", "Coldstream")
    # "cosens bay" is the more specific signal and must win over the
    # "Coldstream" mailing city — the parcel is really RDNO Area B.
    assert v.status == "restricted"
    assert "RDNO" in v.jurisdiction
    assert v.source_url.startswith("https://www.rdno.ca")


def test_plain_coldstream_address_is_prohibited():
    v = check_str_permitting("9300 Kalavista Dr, Coldstream, BC V1B 1Z6", "Coldstream")
    assert v.status == "prohibited"
    assert "Coldstream" in v.jurisdiction
    assert "coldstream.ca" in v.source_url


def test_rdno_signal_flags_restricted():
    v = check_str_permitting("Lot 4 Electoral Area B, North Okanagan", "RDNO")
    assert v.status == "restricted"
    assert "RDNO" in v.jurisdiction


def test_unknown_us_market_is_unknown_not_permitted():
    v = check_str_permitting("123 Main St, Nashville, TN 37203", "Nashville")
    assert v.status == "unknown"
    assert v.source_url == ""          # not BC → no provincial link
    assert v.registry_note == ""


def test_unknown_bc_market_carries_registry_note():
    v = check_str_permitting("Lake Country, BC, Canada", "Lake Country")
    assert v.status == "unknown"
    assert "registration is mandatory" in v.registry_note
    assert v.source_url != ""


def test_never_false_permitted():
    # Restricted/prohibited or unverified markets should never come back
    # 'permitted' — the agent must not imply a market is STR-legal without
    # verification. (Sun Peaks and AQUA are hand-verified 'permitted' entries.)
    for addr in ["Cosens Bay Rd, Coldstream BC", "Coldstream BC",
                 "Nashville TN", "Whistler, BC", "Revelstoke, BC"]:
        assert check_str_permitting(addr, "").status != "permitted"


def test_returns_independent_copies():
    a = check_str_permitting("Coldstream BC", "Coldstream")
    b = check_str_permitting("Coldstream BC", "Coldstream")
    a.details.append("MUTATION")
    assert "MUTATION" not in b.details   # table entry must not be poisoned
