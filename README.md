# STR Comping Agent: short-term-rental income reports

[![CI](https://github.com/Solnest-AI/solnest-str-comping-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Solnest-AI/solnest-str-comping-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Runs in Claude Code](https://img.shields.io/badge/runs%20in-Claude%20Code-d97757.svg)](https://claude.com/claude-code)

Give it a property (an Airbnb link, a Zillow link, or a street address) and it
pulls comparable Airbnb listings nearby, scores them, and generates a branded,
self-contained HTML report with revenue projections.

It runs inside **Claude Code**. Open Claude Code in this folder and say
**"set this up"**; `CLAUDE.md` walks you through it one step at a time.

**One API key required.** AirROI. Everything else is optional. There is **no
Anthropic key**. The report's written analysis comes from Claude Code itself.

**Bring your own keys. Zero secrets in the repo.** Keys live in a local `.env`
that is gitignored and never leaves your machine.

---

## Read this before you trust a number

The revenue figures in these reports are **estimates derived from comparable
listings**, not forecasts and not a valuation. The agent takes the trailing
twelve months of real performance from AirROI for a set of nearby listings it
judges comparable, and projects from that set.

That means:

- The output is only as good as the comp set. In thin or unusual markets there
  may not be six genuinely comparable listings, and the report will say so
  rather than quietly widening until it finds some.
- Trailing performance is not a prediction. Regulation changes, new supply,
  interest rates, and a market turning over will all break the extrapolation.
- Every comp is somebody else's listing, with their pricing strategy, their
  photos, and their reviews. A comp earning $90k does not mean this property
  will.
- **We publish no accuracy figure, because we have not measured one.** Do not
  read the three-tier projection as a confidence interval; it is a spread of
  assumptions, not a statistical bound.

Use it the way you would use an analyst's first pass: a defensible starting
point that shows its work. Not an appraisal, not investment advice.

---

## What it does

- **Any input.** Airbnb URL (best results), Zillow/Realtor URL, or a plain
  street address
- **Real comps.** Pulls listings and trailing-twelve-month performance from the
  [AirROI](https://www.airroi.com) API, then scores each against the subject on
  physical match, financials, quality, amenities, distance, and data
  reliability. Dormant and part-time listings are gated out rather than averaged
  in.
- **Sanity gates.** A report that cannot find a defensible comp set fails
  instead of shipping
- **Three-tier projection.** Conservative / Base / Optimistic, an interactive
  calculator, and a seasonality chart driven by the market's real monthly
  revenue distribution
- **Written analysis from Claude Code.** The agent emits a brief, Claude writes
  the copy, you re-run. No API key, no per-report cost.
- **Branded output.** One self-contained HTML file with your logo and colors

## What it does not do

- It does not value the property or estimate what you should pay for it.
- It does not model your expenses, financing, taxes, or local STR regulation.
- It does not check whether short-term rental is legal at the address.
- It does not work well where AirROI has thin coverage. Rural and newly-opened
  markets are the weak spot.
- It is not a pricing tool. Use PriceLabs or Wheelhouse for that.

---

## Setup

Fastest path: open this folder in Claude Code and say **"set this up."**

By hand:

```bash
pip install -r requirements.txt
cp .env.example .env                      # then add your AirROI key
cp branding.example.json branding.json    # then add your company
```

### API keys

| Key | Required? | What it buys you | Get it |
|---|---|---|---|
| `AIRROI_API_KEY` | **Required** | The comp data: listings, comparables, TTM performance, revenue estimates | https://www.airroi.com/api/developer/activate |
| `FIRECRAWL_API_KEY` | Optional | Street-address and Zillow/Realtor input. Airbnb URLs work without it. | https://www.firecrawl.dev |
| `AIRBTICS_API_KEY` | Optional | Market seasonality overlay. Without it, seasonality comes from AirROI's monthly distribution. | https://airbtics.com |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | Optional | `--email` delivery. Reports always save locally regardless. | https://myaccount.google.com/apppasswords |

No Anthropic key. See the narrative handoff below.

---

## Run it as a Claude Code skill (recommended)

Clone the repo, open Claude Code in the folder, and just ask:

> run comps on https://www.airbnb.com/rooms/12345678

The bundled skill at `.claude/skills/str-comping-agent/SKILL.md` drives the whole
loop: it runs the pipeline, reads the narrative brief, writes the analysis copy
itself, and re-renders. You get one finished HTML report.

## Or run it directly

```bash
# Pass 1 — fetch, score, render. Costs about $0.40 of AirROI credit, ~30s.
python agent.py --input "https://www.airbnb.com/rooms/12345678"
#   output/<slug>.html                    the report
#   output/<slug>.report-data.json        cached pipeline output
#   output/<slug>.narrative-brief.json    what to write the copy from

# Pass 2 — swap in better copy. No API calls, no cost, under a second.
python agent.py --render "output/<slug>.report-data.json" \
                --narratives "output/<slug>.narratives.json"
```

Pass 1 is the expensive half and only needs to run once per property. Re-rendering
with new copy is free, which is the point: the analysis text can be rewritten as
many times as you like without paying for the data again.

## The narrative handoff

The report's written sections (positioning, guest profile, amenity upside,
season commentary) are generated by **Claude Code**, not by an API key you pay
for. The loop:

```bash
# 1. Run it. You get a complete report with template copy,
#    plus output/<slug>.narrative-brief.json
python agent.py --input "https://www.airbnb.com/rooms/12345678"

# 2. In Claude Code: read that brief, write output/<slug>.narratives.json
#    The brief carries the comp table, the market's real peak and shoulder
#    months, the calculator defaults, and the exact JSON shape to write.

# 3. Re-run with the copy
python agent.py --input "https://www.airbnb.com/rooms/12345678" \
                --narratives "output/<slug>.narratives.json"
```

The agent prints the exact three lines to paste into Claude Code when it writes
the brief. The report from step 1 is complete and valid on its own; step 3 just
replaces boilerplate with real analysis.

`--narratives` validates strictly and fails loudly rather than falling back to
template copy, so a report never silently claims to be something it is not.

---

## Branding

The report is white-label. Copy `branding.example.json` to `branding.json` and
edit it:

```json
{
  "company_name": "Your Company",
  "tagline": "Short-Term Rental Management",
  "logo_url": "",
  "website_url": "",
  "primary_color": "#1f3c34",
  "accent_color": "#4b7c6b"
}
```

`branding.json` is gitignored, so your identity never ships with the code. Do
not edit the template to rebrand.

---

## What's in the box

```
str-comping-agent/
├── CLAUDE.md              ← the guided setup + narrative handoff. START HERE.
├── README.md              ← this page
├── SETUP.md               ← the non-technical walkthrough
├── agent.py               ← CLI entrypoint + orchestration
├── comp_scorer.py         ← scoring, hard gates, ranking
├── config.py              ← reads .env + branding.json
├── schema.py              ← typed data models
│
├── scrapers/              ← AirROI, Airbnb, Airbtics, property search
├── adapters/              ← maps API payloads into the scorer's shape
├── validators/            ← sanity gates that block a bad report
├── generators/            ← calculator, narrative brief, narratives, methodology
├── report/                ← HTML rendering + optional email
├── templates/             ← the report template
├── scripts/package.py     ← builds the distribution zip from the git manifest
├── tests/                 ← hermetic tests, run against captured API responses
│
├── .env.example           ← copy to .env
├── branding.example.json  ← copy to branding.json
├── requirements.txt
└── requirements-dev.txt
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest -q        # hermetic: no network, no keys
ruff check .     # lint
```

The test suite is **hermetic**: no network, no API keys. It runs against real
AirROI responses captured in `tests/fixtures/` covering six markets, so a change
to the scorer is checked against data that actually exists rather than against
mocks that agree with the code. CI runs lint and tests on Python 3.10, 3.11, and
3.12 with no secrets configured. Any test that reaches the network fails the
build by design.

---

## Requirements

- Python 3.10+
- An AirROI API key. Everything else is optional.

---

## License

MIT. See [LICENSE](LICENSE).
