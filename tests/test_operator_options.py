"""Operator options (2026-10-22): what Solnest Stays' own copy of the agent did
that the student release did not.

Everything here is off unless asked for (a flag, or a setting in .env), so a
student's report is unchanged:

- PROJECTION_TIERS: Conservative / Base / Optimistic from the comps' actual
  trailing year, with the calculator opening on the Base Case.
- PERMITTING_CHECK: the verified STR-permitting table and its banner.
- COMP_EXCLUDE: comp-name keywords dropped on every run.
- REPORT_FILENAME_PREFIX: e.g. STR-Investment-Report.
- --pin-comps, --address-label, --sqft, --output-dir, --pdf, and --hero-url
  with a local image file.

Pass 1 never replaces a report already on disk (a same-day re-run of the same
property used to overwrite one that may already have gone to a client), while
--render still rewrites its own report. That one is on for everyone.

The tier numbers come from a real Sun Peaks 3-bedroom comp set (2026-10-22):
six comps at CA$76,694 to CA$107,779.
"""

from __future__ import annotations

import math
from argparse import Namespace
from pathlib import Path

import pytest

import agent
import config
from adapters.airroi_to_comp import map_batch_for_scorer
from comp_scorer import rank_comps
from generators.calculator import (align_calculator_to_base_case, derive_calculator_defaults,
                                   derive_revenue_projection)
from report.template_engine import render_report, report_filename, reserve_report_filename
from schema import (CompProperty, PermittingVerdict, PropertyBasics, RentalizerData,
                    ReportData)
from tests._fixtures import market_listings
from validators.sanity import check_subject_hero, validate_calculator_defaults

SUN_PEAKS_3BR = [(76_694, 39.5), (81_396, 25.0), (83_469, 38.0), (98_956, 43.0), (102_813, 31.8),
       (107_779, 53.4)]


def _comps(rows=SUN_PEAKS_3BR) -> list[CompProperty]:
    return [
        CompProperty(
            name=f"Comp {i}", image_url="https://example.com/i.jpg", sleeps=8,
            bedrooms=3, bathrooms=2.5, rating=4.9, review_count=40,
            annual_revenue=rev, occupancy_pct=occ, adr=600,
            nights_booked=150, nights_listed=360,
            airbnb_url=f"https://www.airbnb.com/rooms/{i}",
        )
        for i, (rev, occ) in enumerate(rows)
    ]


def _prop() -> PropertyBasics:
    return PropertyBasics(
        address="5050 Valley Drive Unit# 2, Sun Peaks, British Columbia V0E5N0",
        short_address="5050 Valley Drive Unit# 2, Sun Peaks, British Columbia V0E5N0",
        market="Sun Peaks", bedrooms=3, bathrooms=2.5, max_guests=8, currency="CA$",
        hero_image_url="https://cdn.realtor.ca/x.jpg",
    )


def _rent(adr=674, occ=42.25) -> RentalizerData:
    return RentalizerData(revenue_potential=99_575, adr=adr, occupancy_pct=occ)


def _nights(days, occ):
    return math.floor(days * occ / 100 + 0.5)


# ── PROJECTION_TIERS ──────────────────────────────────────────────────

def test_tiers_are_quartiles_of_the_comps_actual_revenue():
    p = derive_revenue_projection(_comps(), _rent(), days=362)
    assert (p.conservative, p.base_case, p.optimistic) == (81_914, 91_212, 101_849)


@pytest.mark.parametrize("days", [365, 362, 200])
def test_every_tier_card_multiplies_back_to_its_own_revenue(days):
    p = derive_revenue_projection(_comps(), _rent(), days=days)
    for rev, occ, adr in ((p.conservative, p.conservative_occ, p.conservative_adr),
                          (p.base_case, p.base_case_occ, p.base_case_adr),
                          (p.optimistic, p.optimistic_occ, p.optimistic_adr)):
        nights = _nights(days, occ)
        assert abs(nights * adr - rev) <= nights / 2 + 1


def test_calculator_opens_on_the_base_case():
    comps, prop = _comps(), _prop()
    calc = derive_calculator_defaults(comps, _rent(), prop)
    p = derive_revenue_projection(comps, _rent(), calc.days_default)
    aligned = align_calculator_to_base_case(calc, p)

    nights = _nights(aligned.days_default, aligned.occ_default)
    assert abs(nights * aligned.adr_default - p.base_case) <= nights / 2
    # The slider must not snap the computed rate to a grid nobody chose.
    assert aligned.adr_step == 1
    assert aligned.occ_basis == "comp_set"
    assert validate_calculator_defaults(aligned) == []


def test_a_far_off_airroi_model_is_flagged():
    assert derive_revenue_projection(_comps(), _rent(adr=900, occ=60)).airroi_divergence_flag
    assert not derive_revenue_projection(_comps(), _rent()).airroi_divergence_flag


def test_no_comp_revenue_falls_back_to_the_airroi_estimate():
    p = derive_revenue_projection([], _rent())
    assert p.base_case == round(674 * 365 * 0.4225)
    assert "AirROI" in p.source_description


def _report(**kw) -> ReportData:
    comps = _comps()
    return ReportData(property=_prop(), rentalizer=_rent(), comps=comps,
                      calculator=derive_calculator_defaults(comps, _rent(), _prop()), **kw)


def test_tier_cards_render_only_when_the_option_is_on():
    plain = render_report(_report())
    assert "Comp-Based Revenue Projection" not in plain
    assert "Interactive Performance Model" in plain

    tiered = render_report(_report(projection=derive_revenue_projection(_comps(), _rent(), 362)))
    assert "Comp-Based Revenue Projection" in tiered
    assert "CA$91,212" in tiered and "Conservative" in tiered and "Optimistic" in tiered


def test_apply_projection_is_a_no_op_when_off(monkeypatch):
    monkeypatch.setattr(config, "PROJECTION_TIERS", False)
    calc = derive_calculator_defaults(_comps(), _rent(), _prop())
    projection, out = agent._apply_projection(_comps(), _rent(), calc, _prop())
    assert projection is None and out is calc


# ── PERMITTING_CHECK ──────────────────────────────────────────────────

def test_permitting_banner_shows_for_an_unverified_market_only():
    unknown = render_report(_report(permitting=PermittingVerdict(status="unknown",
                                                                 jurisdiction="Somewhere")))
    assert "STR Rules Not Verified" in unknown
    permitted = render_report(_report(permitting=PermittingVerdict(status="permitted")))
    assert "STR Rules Not Verified" not in permitted
    assert "STR Rules Not Verified" not in render_report(_report())


def test_permitting_check_is_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "PERMITTING_CHECK", False)
    assert agent._permitting_check(_prop()) is None
    monkeypatch.setattr(config, "PERMITTING_CHECK", True)
    assert agent._permitting_check(_prop()).status == "permitted"   # Sun Peaks


# ── File names: never overwrite, prefix ───────────────────────────────

def test_a_same_day_rerun_gets_its_own_file(tmp_path):
    data = _report()
    first = reserve_report_filename(data, tmp_path)
    (tmp_path / first).write_text("sent to the client", encoding="utf-8")
    second = reserve_report_filename(data, tmp_path)
    assert second == first[:-len(".html")] + "-2.html"
    (tmp_path / second).write_text("x", encoding="utf-8")
    assert reserve_report_filename(data, tmp_path).endswith("-3.html")


def test_render_rewrites_the_file_pass_one_reserved():
    data = _report(report_file="STR-Investment-Report-x-2026-10-22-2.html")
    assert report_filename(data) == "STR-Investment-Report-x-2026-10-22-2.html"


def test_filename_prefix_from_env(monkeypatch):
    monkeypatch.setattr(config, "REPORT_FILENAME_PREFIX", "STR-Investment-Report")
    assert report_filename(_report()).startswith(
        "STR-Investment-Report-5050-Valley-Drive-Unit-2-Sun-Peaks")
    monkeypatch.setattr(config, "REPORT_FILENAME_PREFIX", "")
    assert "-Report-5050-Valley-Drive" in report_filename(_report())


# ── Subject overrides ─────────────────────────────────────────────────

def test_address_label_changes_only_the_display():
    prop = _prop()
    prop.title = "Exceptional Ski-in/Ski-out Townhome"
    agent._apply_display_overrides(Namespace(address_label="2-5050 Valley Drive, Sun Peaks, BC"), prop)
    assert prop.short_address == prop.address == "2-5050 Valley Drive, Sun Peaks, BC"
    assert prop.title is None


def test_sqft_is_an_override():
    prop = _prop()
    agent._apply_size_overrides(prop, Namespace(beds=0, baths=0, guests=0, market=None, sqft=1528))
    assert prop.sqft == 1528


def test_local_hero_file_is_embedded_and_trusted(tmp_path):
    photo = tmp_path / "hero.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    hero = agent._hero_from_arg(str(photo))
    assert hero.startswith("data:image/")
    assert check_subject_hero(hero) == []
    assert agent._hero_from_arg("https://cdn.realtor.ca/x.jpg") == "https://cdn.realtor.ca/x.jpg"

    # ...and the page shows it (safe_url alone dropped it to the placeholder).
    data = _report()
    data.property.hero_image_url = hero
    assert f'src="{hero}"' in render_report(data)


def test_embedded_svg_and_scripts_are_still_refused():
    from report.template_engine import safe_img_src
    assert safe_img_src("data:image/svg+xml;base64,PHN2Zz4=") == ""
    assert safe_img_src("javascript:alert(1)") == ""
    assert safe_img_src("data:image/png;base64,iVBOR\" onerror=\"x") == ""


# ── COMP_EXCLUDE ──────────────────────────────────────────────────────

def _filter_args(**kw):
    base = dict(subject_on_water=False, allow_oceanfront_comps=False, require=None,
                no_feature_filter=True, exclude=None)
    return Namespace(**{**base, **kw})


def test_exclusion_reaches_a_host_that_does_not_name_itself_in_the_title():
    from comp_filters import apply_keyword_exclusions

    def raw(name, host):
        return {"listing_info": {"listing_name": name}, "host_info": {"host_name": host}}
    pool = [raw("Stone's Throw #55 | 3 Peaks Rentals", "⁨3 Peaks Rentals Ltd.⁩"),
            raw("The Solstice Lodge at Altitude #21", "⁨3 Peaks Rentals Ltd.⁩"),
            raw("Family Friendly Ski-in/Out", "Jacqueline")]
    kept, dropped = apply_keyword_exclusions(pool, ["3 Peaks"])
    assert dropped == ["Stone's Throw #55 | 3 Peaks Rentals", "The Solstice Lodge at Altitude #21"]
    assert len(kept) == 1


def test_env_exclusions_join_the_command_line_ones(monkeypatch):
    monkeypatch.setattr(config, "COMP_EXCLUDE", "3 Peaks")
    filters = agent._decide_filters(_filter_args(exclude="oceanfront"), _prop())
    assert filters.exclude_terms == ["oceanfront", "3 Peaks"]
    monkeypatch.setattr(config, "COMP_EXCLUDE", "")
    assert agent._decide_filters(_filter_args(), _prop()).exclude_terms == []


# ── --pin-comps ───────────────────────────────────────────────────────

SUBJECT = {"title": "Ski-in/Ski-out Townhome", "description": "3 bed townhome, private hot tub",
           "amenities": ["Hot tub", "Ski-in/Ski-out"], "property_type": "Entire townhouse",
           "bedrooms": 3, "bathrooms": 2.5, "max_guests": 8, "min_nights": 2, "adr": 674,
           "latitude": 50.886, "longitude": -119.865}


def test_pins_move_to_the_front_and_failed_ones_are_reinstated():
    result = {"ranked": [{"airbnbId": "1", "name": "a"}, {"airbnbId": "2", "name": "b"}],
              "hard_fails": [{"airbnbId": "3", "name": "c", "hard_fail": True}]}
    found = agent._pin_comps(["3", "2", "999"], result, [], SUBJECT)
    assert found == ["3", "2"]
    assert [c["airbnbId"] for c in result["ranked"]] == ["3", "2", "1"]
    assert result["hard_fails"] == []
    assert result["ranked"][0]["hard_fail"] is False


def test_a_pin_the_pool_filters_dropped_is_scored_and_kept():
    raw = market_listings("sunpeaks")
    pin = agent._listing_id(raw[0])
    result = rank_comps(dict(SUBJECT), map_batch_for_scorer(raw[1:]), top_n=12)
    assert agent._pin_comps([pin], result, raw, dict(SUBJECT)) == [pin]
    assert agent._listing_id(result["ranked"][0]) == pin


def test_no_pins_changes_nothing():
    result = {"ranked": [{"airbnbId": "1"}], "hard_fails": []}
    assert agent._pin_comps(agent._pin_ids(Namespace(pin_comps=None)), result, [], SUBJECT) == []
    assert result["ranked"] == [{"airbnbId": "1"}]


def test_new_flags_parse():
    args = agent._build_parser().parse_args([
        "--input", "x", "--pin-comps", "896575902724542679", "--address-label", "L",
        "--sqft", "1528", "--output-dir", "out", "--pdf"])
    assert agent._pin_ids(args) == ["896575902724542679"]
    assert (args.address_label, args.sqft, args.output_dir, args.pdf) == ("L", 1528, "out", True)


def test_pdf_export_never_overwrites(tmp_path):
    from report.pdf_export import free_path
    target = tmp_path / "r.pdf"
    assert free_path(target) == target
    target.write_bytes(b"%PDF")
    assert free_path(target) == Path(tmp_path / "r-2.pdf")
