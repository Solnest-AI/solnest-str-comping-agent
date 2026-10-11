"""Pro forma under the revenue projection (S2-027).

The tool has no expense, tax, insurance or HOA data for any property, so every
expense and financing figure is the student's own: their standard rates in
proforma.json (gitignored, never shipped), and this property's numbers in a
file Claude writes from what the student says (`agent.py --proforma FILE`).
Ryan, 2026-10-10: student's numbers, financing optional.

The rules that keep it honest:

- A blank line is NEVER zero. NOI waits until every line has a number (0
  counts: "no HOA" is a 0, not a blank) and names the lines still missing.
- Each financing result appears only once its own inputs exist: a price alone
  gives a cap rate, not a cash flow.
- The only figure pre-filled from data is the purchase price, from the price
  on the listing page, and the report labels it.

compute() is the single source of the arithmetic. The report's JavaScript
mirrors it line for line so the sliders and the inputs update the totals live;
render_report() uses compute() for the values printed before any JS runs.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from schema import ProformaInputs

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "proforma.json"

# Below this a "price" on a listing page is rent, not a purchase price.
MIN_PURCHASE_PRICE = 20_000
_MONEY = (0, 100_000_000)
_PCT = (0, 100)

# field -> (low, high, hint shown beside the input)
FIELDS: dict[str, tuple[float, float, str]] = {
    "management_pct":      (*_PCT, "% of gross"),
    "platform_fee_pct":    (*_PCT, "% of gross"),
    "cleaning_per_stay":   (*_MONEY, "per stay"),
    "avg_stay_nights":     (1, 365, "nights per stay"),
    "utilities_monthly":   (*_MONEY, "per month"),
    "insurance_annual":    (*_MONEY, "per year"),
    "property_tax_annual": (*_MONEY, "per year"),
    "hoa_monthly":         (*_MONEY, "per month"),
    "maintenance_pct":     (*_PCT, "% of gross"),
    "supplies_pct":        (*_PCT, "% of gross"),
    "other_annual":        (*_MONEY, "per year"),
    "purchase_price":      (*_MONEY, ""),
    "down_payment_pct":    (*_PCT, "%"),
    "interest_rate_pct":   (0, 30, "% a year"),
    "loan_years":          (1, 50, "years"),
    "startup_costs":       (*_MONEY, "one time"),
}
MONEY_FIELDS = frozenset(k for k, (low, high, _) in FIELDS.items() if (low, high) == _MONEY)

# (line key, label, input fields, how the annual amount is worked out)
EXPENSE_LINES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("management",   "Management fee",        ("management_pct",),                      "pct"),
    ("platform",     "Platform fees",         ("platform_fee_pct",),                    "pct"),
    ("cleaning",     "Cleaning",              ("cleaning_per_stay", "avg_stay_nights"), "cleaning"),
    ("utilities",    "Utilities and internet", ("utilities_monthly",),                  "monthly"),
    ("insurance",    "Insurance",             ("insurance_annual",),                    "annual"),
    ("property_tax", "Property tax",          ("property_tax_annual",),                 "annual"),
    ("hoa",          "HOA / strata",          ("hoa_monthly",),                         "monthly"),
    ("maintenance",  "Repairs and maintenance", ("maintenance_pct",),                   "pct"),
    ("supplies",     "Supplies",              ("supplies_pct",),                        "pct"),
    ("other",        "Other",                 ("other_annual",),                        "annual"),
)
FINANCING_FIELDS = ("purchase_price", "down_payment_pct", "interest_rate_pct",
                    "loan_years", "startup_costs")
FINANCING_LABELS = {
    "purchase_price": "Purchase price",
    "down_payment_pct": "Down payment",
    "interest_rate_pct": "Interest rate",
    "loan_years": "Loan term",
    "startup_costs": "Furnishing and setup",
}
# The same lines for the page's updateProforma().
JS_LINES = [{"key": k, "label": label, "fields": list(fields), "kind": kind}
            for k, label, fields, kind in EXPENSE_LINES]


class ProformaError(Exception):
    """A proforma file is missing, unreadable or holds an unusable number.
    The message names the file and every field at fault."""


def js_round(value: float) -> int:
    """Math.round: halves go up. Python's round() sends them to even, and the
    printed figures must match what the page's JavaScript shows."""
    return math.floor(value + 0.5)


# ── Loading ────────────────────────────────────────────────────────────

def _problems(data) -> list[str]:
    if not isinstance(data, dict):
        return ['must be one JSON object: { "management_pct": 20, ... }']
    out = [f"unknown field {k!r}" for k in data if k not in FIELDS]
    for key, value in data.items():
        if key not in FIELDS or value is None:
            continue
        low, high, _ = FIELDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            out.append(f"{key} {value!r} must be a plain number like 20 (no % or $)")
        elif not low <= value <= high:
            out.append(f"{key} {value!r} must be between {low:g} and {high:g}")
    return out


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ProformaError(f"{path.name} is not valid JSON ({e})") from e
    problems = _problems(data)
    if problems:
        raise ProformaError(f"{path.name}: " + "; ".join(problems))
    return data


def load_proforma(defaults_path: Path | None = None, file_path: str | Path | None = None,
                  list_price: Optional[float] = None) -> ProformaInputs:
    """The student's defaults, overlaid by this property's file, plus the
    listing price when no purchase price was given. A null in a file is a
    blank, and a null in the property file clears a default."""
    merged: dict = {}
    defaults = Path(defaults_path or DEFAULTS_PATH)
    if defaults.exists():
        merged.update(_read(defaults))
    if file_path:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise ProformaError(f"No proforma file at {path}")
        merged.update(_read(path))
    price_source = ""
    if (merged.get("purchase_price") is None and list_price
            and list_price >= MIN_PURCHASE_PRICE):
        merged["purchase_price"] = float(list_price)
        price_source = "listing"
    return ProformaInputs(**merged, price_source=price_source)


# ── The arithmetic ─────────────────────────────────────────────────────

def _line_amount(kind: str, fields: tuple[str, ...], p: ProformaInputs,
                 gross: float, occupied: float) -> Optional[float]:
    values = [getattr(p, f) for f in fields]
    if any(v is None for v in values):
        return None
    if kind == "pct":
        return gross * values[0] / 100
    if kind == "cleaning":
        per_stay, avg_stay = values
        return per_stay * js_round(occupied / avg_stay)
    if kind == "monthly":
        return values[0] * 12
    return values[0]


def annual_debt_service(price: float, down_pct: float, rate_pct: float, years: float) -> float:
    loan = price * (1 - down_pct / 100)
    if loan <= 0:
        return 0.0
    n = years * 12
    r = rate_pct / 100 / 12
    monthly = loan / n if r == 0 else loan * r / (1 - (1 + r) ** -n)
    return monthly * 12


def compute(p: ProformaInputs, gross: float, occupied: float) -> dict:
    lines = []
    for key, label, fields, kind in EXPENSE_LINES:
        lines.append({"key": key, "label": label,
                      "amount": _line_amount(kind, fields, p, gross, occupied)})
    missing = [line["label"] for line in lines if line["amount"] is None]
    total = None if missing else sum(line["amount"] for line in lines)
    noi = None if total is None else gross - total

    price = p.purchase_price
    cap_rate = noi / price * 100 if noi is not None and price else None
    debt = None
    if None not in (price, p.down_payment_pct, p.interest_rate_pct, p.loan_years):
        debt = annual_debt_service(price, p.down_payment_pct, p.interest_rate_pct, p.loan_years)
    cash_flow = noi - debt if noi is not None and debt is not None else None
    invested = None
    if None not in (price, p.down_payment_pct, p.startup_costs):
        invested = price * p.down_payment_pct / 100 + p.startup_costs
    coc = cash_flow / invested * 100 if cash_flow is not None and invested else None
    return {
        "gross": gross, "lines": lines, "missing": missing,
        "total_expenses": total, "noi": noi, "cap_rate_pct": cap_rate,
        "debt_service": debt, "cash_flow": cash_flow,
        "cash_invested": invested, "cash_on_cash_pct": coc,
    }


# ── Display (the page's JavaScript formats identically) ────────────────

def fmt_money(value: Optional[float], currency: str) -> str:
    if value is None:
        return "not set"
    whole = js_round(value)
    sign = "-" if whole < 0 else ""
    return f"{sign}{currency}{abs(whole):,}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "not set"
    return f"{js_round(value * 10) / 10:.1f}%"


def missing_text(missing: list[str]) -> str:
    if not missing:
        return ""
    return ("Fill in the blank lines to see NOI (enter 0 for a cost that does not apply): "
            + ", ".join(missing) + ".")


def input_value(value: Optional[float]) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)
