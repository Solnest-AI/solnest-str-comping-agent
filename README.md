# STR Comping Agent — instant short-term-rental income reports

[![CI](https://github.com/Solnest-AI/solnest-str-comping-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Solnest-AI/solnest-str-comping-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Runs in Claude Code](https://img.shields.io/badge/runs%20in-Claude%20Code-d97757.svg)](https://claude.com/claude-code)

Give it a property — an Airbnb link, a Zillow link, or just a street address — and it finds the real comparable Airbnb listings nearby, scores them, and generates a branded, self-contained HTML report with revenue projections. The kind of analysis that takes an analyst an afternoon, in about a minute.

It runs inside **Claude Code**. You don't need to be technical: open Claude Code, drag this folder in, and say **"set this up."** Claude walks you through it one step at a time — installing dependencies, collecting your API keys, and branding the report with your company. Most people generate their first report in under 15 minutes.

**Bring your own keys. Zero secrets in the repo.** Every API key lives in a local `.env` that is gitignored and never leaves your machine.

---

## What it does

- **Any input** — Airbnb URL (best results), Zillow URL, or a plain street address
- **Real comps** — pulls comparable listings and trailing-twelve-month performance from the [AirROI](https://www.airroi.com) API, then scores each one against the subject property (physical match, financials, quality, amenities, data reliability, must-match features like waterfront/pool/hot-tub)
- **Defensible projections** — Conservative / Base / Optimistic revenue tiers, an interactive calculator, and a seasonal occupancy chart
- **Branded output** — a single self-contained HTML file with your logo, colors, and tagline

---

## What's in the box

```
str-comping-agent/
├── CLAUDE.md            ← the guided setup. The file Claude reads to walk you through everything. START HERE.
├── README.md            ← this page
├── agent.py             ← CLI entrypoint + orchestration
├── comp_scorer.py       ← the comp scoring engine
├── config.py            ← reads your .env + branding.json
├── schema.py            ← typed data models
│
├── scrapers/            ← data sources (AirROI, Airbnb, Airbtics, property search)
├── adapters/            ← maps API responses into the scorer's shape
├── validators/          ← sanity gates (catches bad/hallucinated data before it reaches the report)
├── generators/          ← revenue projection, narratives, methodology
├── report/              ← HTML template engine + optional email delivery
├── templates/           ← the report HTML template
├── tests/               ← 53 tests (pytest)
│
├── .env.example         ← copy to .env and fill in your keys
├── branding.example.json← copy to branding.json and add your company
├── requirements.txt     ← runtime dependencies
└── requirements-dev.txt ← test + lint dependencies
```

---

## Setup

The fastest path is to let Claude Code drive it — open this folder in Claude Code and say **"set this up."** `CLAUDE.md` is the conductor; it walks you through each step and waits for you at each one.

If you'd rather do it by hand:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your API keys
cp .env.example .env        # then edit .env

# 3. Set your branding
cp branding.example.json branding.json   # then edit it
```

### API keys

| Key | Required? | What it's for | Get it |
|-----|-----------|---------------|--------|
| `AIRROI_API_KEY` | **Required** | Property estimates, comps, performance metrics | https://www.airroi.com/api/developer/activate |
| `FIRECRAWL_API_KEY` | **Required** | Web search when given a street address | https://www.firecrawl.dev |
| `APIFY_TOKEN` | Recommended | Reliable Zillow scraping (prevents hallucinated data) | https://console.apify.com/account#/integrations |
| `AIRBTICS_API_KEY` | Optional | Market-level overlay (occupancy / ADR / listing count) | Airbtics |
| `GOOGLE_MAPS_API_KEY` | Optional | Street View hero photo for off-market properties | https://console.cloud.google.com |

---

## Usage

```bash
# Airbnb URL — best results, pulls everything from AirROI
python agent.py --input "https://www.airbnb.com/rooms/12345678"

# Zillow URL — uses Apify for reliable extraction
python agent.py --input "https://www.zillow.com/homedetails/123-Main-St/12345_zpid/"

# Street address — pass beds/baths/guests if the property isn't listed online
python agent.py --input "123 Main St, Nashville, TN 37203" --beds 3 --baths 2 --guests 8 --market "Nashville"
```

### Useful flags

| Flag | What it does |
|------|--------------|
| `--beds` / `--baths` / `--guests` | Property details (for addresses with no live listing) |
| `--market "City Name"` | Override market detection for comp accuracy |
| `--radius N` | AirROI comp search radius in miles |
| `--exclude "oceanfront,beachfront"` | Drop comps whose names contain these keywords (alias: `--exclude-comps`, also takes a full listing name) |
| `--require "pool,hot_tub"` | Require comps to have these features |
| `--subject-on-water` / `--allow-oceanfront-comps` | Water-proximity controls |
| `--no-feature-filter` | Disable the automatic must-have feature filter |
| `--currency "$"` / `"CA$"` | Override currency (auto-detected from address) |
| `--hero-url "https://..."` | Provide a custom property photo |
| `--listing-url "https://..."` | Link the report's "View Listing" button |
| `--email you@example.com` | Email the report after generating (needs Gmail SMTP configured) |

Reports are written to `output/` as self-contained `.html` files — open in any browser.

---

## The report

- Three-tier revenue projection (Conservative / Base Case / Optimistic)
- Six comparable properties with actual TTM performance data
- Interactive revenue calculator with sliders
- Seasonal occupancy chart
- A methodology section explaining how the numbers were derived

---

## Development

```bash
pip install -r requirements-dev.txt
pytest          # run the test suite (53 tests)
ruff check .    # lint
```

---

## Requirements

- Python 3.10+
- An AirROI API key (required) and Firecrawl API key (required); others optional — see the table above.

---

## License

MIT — see [LICENSE](LICENSE). Built by [Solnest AI](https://www.solnestai.com).
