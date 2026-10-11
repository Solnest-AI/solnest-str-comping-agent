#!/usr/bin/env python3
"""Save a real estate agent's details for co-branded reports (S2-027).

    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/add_partner.py \\
        --name "Jane Doe" --brokerage "Summit Realty Group" \\
        --phone "+1 865 555 0100" --email jane@summitrealty.com \\
        --website summitrealty.com
    "$PY" scripts/add_partner.py --list

Writes partners/<agent>.json. Every detail comes from the student: never look
an agent up and fill it in. --website finds the agent's logo with Firecrawl
(the kit's key) the same way scripts/brand_from_website.py finds the
student's; --logo takes the image address directly instead. The logo is saved
to .cache/partner_logo.* so Claude can LOOK at it before the first report.

Re-running with the same --name updates that agent and keeps every detail not
given again. Then: agent.py ... --partner <agent>.

Exit codes:
  0  saved (or listed)
  2  Firecrawl key missing, rejected or out of credit (run scripts/check_setup.py)
  3  a detail is not usable; the output says which, nothing was written
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import brand_from_website as bw  # noqa: E402
import config  # noqa: E402
import kit  # noqa: E402
import partners  # noqa: E402


def _previous(name: str) -> dict:
    path = partners.PARTNERS_DIR / f"{partners.partner_slug(name)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in partners.FIELDS} if isinstance(data, dict) else {}


def _list() -> int:
    saved = partners.list_partners()
    if not saved:
        print("[partner] No agents saved yet.")
    for slug in saved:
        try:
            p = partners.load_partner(slug)
            extra = f", {p.brokerage}" if p.brokerage else ""
            print(f"  {slug:<28} {p.name}{extra}")
        except partners.PartnerError as e:
            print(f"  {slug:<28} NOT USABLE: {e}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list the saved agents")
    ap.add_argument("--name", help="the agent's name (required to save)")
    ap.add_argument("--brokerage", help="the agent's brokerage or team")
    ap.add_argument("--phone", help="the agent's phone number")
    ap.add_argument("--email", help="the agent's email address")
    ap.add_argument("--website", help="the agent's website: finds their logo")
    ap.add_argument("--logo", help="the logo's image address, when the site's own was not found "
                                   "or is the wrong one (right-click the logo, Copy image address)")
    a = ap.parse_args(argv)
    if a.list:
        return _list()
    if not (a.name or "").strip():
        print("[partner] --name is required. NEXT: ask the student for the agent's name, wait, then run this again.")
        return 3

    data = {**_previous(a.name), "name": a.name.strip()}
    for key in ("brokerage", "phone", "email"):
        value = getattr(a, key)
        if value is not None:
            data[key] = value.strip()

    site, brand = "", {}
    if a.website:
        site = a.website.strip()
        if not site.lower().startswith(("http://", "https://")):
            site = "https://" + site
        origin = bw.site_origin(site)
        if not origin:
            print(f"[partner] '{a.website}' is not a website address.")
            print("NEXT: ask the student for the agent's website (like agentsite.com), wait, then run this again.")
            return 3
        data["website_url"] = origin
        if not a.logo:
            try:
                config.ensure_firecrawl_configured()
                brand = bw.read_brand(site)
            except RuntimeError as e:
                print(f"[partner] {e}")
                return 2
            except kit.KeyFailure as e:
                kit.forget_setup_pass()
                print(e)
                return 2
            except (httpx.HTTPError, ValueError) as e:
                print(f"[partner] Could not read {site} for a logo: {type(e).__name__}")

    logo = bw.logo_url({"logo": a.logo.strip()}, site) if a.logo else bw.logo_url(brand, site)
    logo_file = None
    if logo:
        logo_file = bw.fetch_logo(logo, stem="partner_logo")
        if logo_file:
            data["logo_url"] = logo
            data["logo_background"] = bw.plate_for(brand, logo_file, config.BRANDING["primary_color"])
        else:
            print(f"[partner] The logo did not load as an image: {logo}")
    elif a.logo:
        print(f"[partner] The logo given is not a web link: {a.logo.strip()}")

    problems = partners.partner_problems(data)
    if problems:
        print("[partner] Not saved:")
        for p in problems:
            print(f"  - {p}")
        print("NEXT: ask the student for the right detail, wait, then run this again.")
        return 3

    path = partners.save_partner(data)
    slug = path.stem
    saved = json.loads(path.read_text(encoding="utf-8"))
    print(f"[partner] Saved {path}")
    for key in partners.FIELDS:
        print(f"  {key:<15} {saved.get(key) or '(none)'}")
    if logo_file:
        print(f"  logo saved      {logo_file}")
    elif not saved.get("logo_url"):
        print("  (no logo: the report shows the agent's name only. For a logo, ask the student to")
        print('   right-click it on the agent\'s site, Copy image address, and re-run with --logo "<that>")')
    print("NEXT: look at the saved logo, show the student these details and ask if they are right.")
    print(f"      Then add --partner {slug} to the report command (free on --render).")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
