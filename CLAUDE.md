> **Using this in Claude Code?** The skill at `.claude/skills/str-comping-agent/SKILL.md` is the operating manual and it drives the two-pass loop for you. This file covers setup and troubleshooting.

# STR Comping Agent: operating manual for Claude Code

A Python CLI that turns a property (Airbnb URL, Zillow/Realtor URL, or a street
address) into a branded, self-contained HTML income report built from real
comparable Airbnb listings.

You are the narrative engine for this tool. It ships with **no Anthropic API
key** on purpose: the user already has you. Read the narrative handoff section
below before you run anything, because it changes what a "finished" report means.

---

## When the user says "set this up"

Walk them through this in order. Confirm each step before moving on.

### Step 1: Check Python

Run `python --version`. If it fails or shows below 3.10:

> "You need Python 3.10 or newer. Download it from https://www.python.org/downloads/
> and check 'Add Python to PATH' during install. Tell me when it's done."

On macOS/Linux `python` may not exist; try `python3`.

### Step 2: Install dependencies

```bash
pip install -r requirements.txt
```

If `pip` is missing, use `python -m pip install -r requirements.txt`.

### Step 3: Collect API keys

**Only one key is required.** Do not ask for the others unless the user wants
what they unlock.

| Key | Required? | What breaks without it | Where to get it |
|---|---|---|---|
| `AIRROI_API_KEY` | **YES** | Everything. This is the comp data. | https://www.airroi.com/api/developer/activate |
| `FIRECRAWL_API_KEY` | No | Address and Zillow/Realtor input. Airbnb URLs still work. | https://www.firecrawl.dev |

| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | No | `--email` delivery. Reports still save to `output/`. | https://myaccount.google.com/apppasswords |

**There is no `ANTHROPIC_API_KEY` step.** If the user offers one, tell them it
is not needed and that you write the narratives yourself.

Run the interactive builder, or write `.env` yourself from `.env.example`:

```bash
python setup.py
```

Ask for one key at a time. Wait for each. Never echo a key back into the
transcript, and never write one anywhere except `.env`.

### Step 4: Set the branding

The report is white-label. It reads `branding.json`; if that file is absent it
falls back to `branding.example.json`, so a fresh clone renders with neutral
placeholder branding rather than someone else's company.

```bash
cp branding.example.json branding.json
```

Then edit `company_name`, `tagline`, `logo_url`, `website_url`,
`primary_color`, `accent_color`. Do **not** edit the template to rebrand.

### Step 5: First report

```bash
python agent.py --input "https://www.airbnb.com/rooms/39508095"
```

The HTML lands in `output/`. Then do the narrative handoff below. The first
report is not finished until you have.

---

## The narrative handoff loop: READ THIS

Without an Anthropic key the agent still produces a complete, valid report, but
its narrative sections are data-driven **template copy**. Your job is to replace
them. The loop is three steps.

### 1. Run the agent

```bash
python agent.py --input "<property>"
```

Alongside the HTML it writes a brief:

```
output/<slug>.narrative-brief.json
```

and prints a `NARRATIVE HANDOFF` block naming that file. `<slug>` is derived
from the property's short address, so the brief sits next to its report.

### 2. Read the brief and write the copy

Read the brief. It contains everything you are allowed to write from:

- `subject`: the property, its amenities, its description
- `revenue_estimate`: revenue potential, ADR, occupancy
- `market_seasonality`: this market's **real** peak/shoulder months and the
  peak share of annual revenue, derived from AirROI's monthly revenue
  distribution
- `comps`: the selected comp set, flattened to the fields the copy may cite
- `rules`: the constraints
- `output_schema` and `output_example`: the exact JSON shape to produce

Write the JSON to the path the brief names, normally:

```
output/<slug>.narratives.json
```

The object has seven required string fields:

```json
{
  "positioning_summary": "...",
  "guest_profile": "...",
  "amenity_upside": "...",
  "config_description": "...",
  "guests_description": "...",
  "peak_season_text": "...",
  "shoulder_season_text": "...",
  "amenity_badges":    [{"emoji": "🎮", "text": "..."}],
  "positioning_cards": [{"emoji": "📍", "title": "...", "text": "..."}]
}
```

`amenity_badges` and `positioning_cards` are optional but the template renders
holes without them, so write both.

**Hard rules when writing narrative copy:**

- **Never invent a number.** Every figure must come from the brief. If the
  brief does not carry it, do not write it.
- **Never name a season, month, or climate the brief did not give you.** The
  peak months come from `market_seasonality`, not from what you know about the
  region. A Destin beach report once shipped "Peak Season (Dec–Mar) … ski-in
  proximity" next to a chart peaking in July. That is the bug this rule exists
  to prevent.
- **Never state an occupancy range the comp set does not support.** Cite what
  the comps actually did.
- Write the JSON object alone, with no markdown fences and no commentary.
  The loader rejects anything else.

### 3. Re-run with the copy

```bash
python agent.py --input "<same input>" --narratives "output/<slug>.narratives.json"
```

The brief carries a ready-made `rerun_command` field. Use it verbatim.

`--narratives` is strict on purpose: if the file is missing, malformed, or a
field is absent, the run **fails loudly with every problem listed**. It does not
silently fall back to template copy, because the user asked for that file
specifically. Fix the JSON and re-run.

---

## CLI reference

`--input` is the only required flag.

| Flag | What it does |
|---|---|
| `--input` | **Required.** Airbnb URL, Zillow/Realtor URL, or a street address |
| `--narratives PATH` | Load narrative copy you wrote (see handoff above) |
| `--email you@example.com` | Email the report after generating (needs Gmail configured) |
| `--beds N` / `--baths N` / `--guests N` | Property details, needed for addresses with no live listing |
| `--market "City"` | Override market detection |
| `--no-cache` | Bypass the 24h vendor-response cache and force fresh (paid) AirROI calls |
| `--require "pool,hot_tub"` | Require comps to have these features |
| `--exclude "oceanfront,beachfront"` | Drop comps whose names contain these keywords (alias `--exclude-comps`; also takes a full listing name to drop one specific comp) |
| `--no-feature-filter` | Disable the automatic must-have feature filter |
| `--subject-on-water` | Declare the subject is on water (skips the auto water-proximity filter) |
| `--allow-oceanfront-comps` | Keep waterfront comps even when the subject is inland |
| `--currency "$"` / `"CA$"` | Override currency (auto-detected from the address) |
| `--hero-url URL` | Supply the hero photo when the listing scrape is blocked |
| `--listing-url URL` | Link the report's "View Listing" button |
| `--skip-financials` | Dev only. Skips AirROI, produces an empty estimate |

Run `python agent.py --help` if a flag here disagrees with the code; the code wins.

---

## How the agent works

| File | Job |
|---|---|
| `setup.py` | Interactive `.env` builder, run once |
| `agent.py` | CLI entry. Orchestrates the pipeline |
| `config.py` | Loads `.env` + `branding.json`, exposes typed settings |
| `schema.py` | Pydantic models for the report data |
| `scrapers/airroi.py` | Listings, comps, revenue estimates from AirROI |
| `scrapers/property_search.py` | Firecrawl property scraper (address / listing URL) |
| `scrapers/airbnb.py` | Airbnb listing scraper |
| `adapters/airroi_to_comp.py` | Maps AirROI payloads into the internal Comp model |
| `comp_scorer.py` | Hard gates, scoring, ranking; picks the final comp set |
| `generators/calculator.py` | Calculator defaults, seasonal data, season labels |
| `generators/narrative_brief.py` | **The narrative contract + the brief written for you** |
| `generators/narratives.py` | Template copy, and `--narratives` file loading/validation |
| `generators/methodology.py` | Methodology footnote |
| `validators/sanity.py` | Phase A/B gates that block a bad report |
| `report/template_engine.py` | Renders Jinja2 → HTML |
| `report/email_sender.py` | Optional Gmail SMTP delivery |
| `templates/report.html.j2` | The report template |
| `scripts/package.py` | Builds the distribution zip from the git manifest |

### Data semantics you must not get wrong

These came from auditing 200 live AirROI records. Getting them backwards
produces a confidently wrong report:

- `ttm_available_days` is **unsold nights**, not "days listed". A high value
  means the listing barely booked. The code uses `nights_booked` /
  `nights_listed` instead, and there is deliberately no field named
  `days_available`.
- `occupancy_pct` is **adjusted** occupancy: booked ÷ open nights.
- `annual_revenue` is **fee-inclusive**; `adr` is **fee-exclusive**. Never
  divide one by the other to reconstruct nights. You will be off by ~19%.
- `CompProperty.rating` is `Optional`. `None` means too few reviews to rate;
  AirROI sends `0.0` for that case and the adapter converts it.
- Amenities are Title Case display strings matched by **exact set membership**,
  never substring. "Pool" is a substring of "Pool table" and "Pool view";
  "fire" is a substring of "Fire extinguisher".

These four came from the 2026-09-20 pass. They cost real time to find:

- **`percentiles` on `/calculator/estimate` is the spread of AirROI's MODEL
  PREDICTIONS, not of observed results.** Measured against the 25 comparables
  in the same response: p25/p50/p75 land within 5% of what the pool actually
  did, p90 was 28% above what the single best of 25 listings earned. The
  headline uses **p75**. Never p90, and never describe any of them as measured.
- **A month with no market activity comes back as a row of zeros, not as an
  absent row.** `/markets/metrics/occupancy` returns the SAME value for
  avg/p25/p50/p75/p90 when it has nothing. Three of Sun Peaks' twelve months
  look like that. `generators/calculator.market_has_data()` is the detector.
  The sentinel is IDENTICAL percentiles, not MISSING ones: a row carrying only
  `p50` is a sparse market, not a dead one, and rejecting it blanks the chart.
- **`ttm_occupancy` is computed over the full 365-day window whether or not the
  listing existed for it.** A listing live three months reports roughly a
  quarter of its real pace, with the pre-launch period counted as blocked
  inventory. `SubjectPerformance.is_stabilized` gates this; `has_history` is a
  much lower bar and only decides whether to DISPLAY the trailing numbers.
  Never anchor a projection to an unstabilized subject.
- **`ttm_revpar` and `ttm_adjusted_revpar` reconcile to neither `revenue/365`
  nor `revenue/nights_listed`** (measured ratios 0.80-0.97 across the Sun Peaks
  pool). Their definition is not one this code can state, so nothing uses them.
  `CompProperty.revenue_per_listed_night` is computed locally instead.

Market endpoints accept only `usd` or `native` for `currency` and 422 on
`cad`; `/price-recommendation/*` accepts `cad` fine. The full OpenAPI spec is
at `https://www.airroi.com/openapi.json` (24 endpoints); the docs site does not
link it and `/api/openapi.json` 404s.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AIRROI_API_KEY is not set` | No `.env`, or the key is blank | `cp .env.example .env`, paste the key, or run `python setup.py` |
| `FIRECRAWL_API_KEY is not set` | Address or listing-URL input without Firecrawl | Add the key, or pass an Airbnb URL instead |
| `Phase A sanity failed: <6 comps` | Too few comparable listings in this market | Relax with `--no-feature-filter`, or try a denser market. AirROI caps comparables at 25 and accepts only a 1-10 mile radius, so there is no way to widen the pool. |
| Report renders but the prose is generic | You have not done the narrative handoff | Read `output/<slug>.narrative-brief.json` and re-run with `--narratives` |
| `--narratives file not found` | Ran with `--narratives` before writing the file | Run once without it to generate the brief |
| `NarrativeFileError: ... not valid JSON` | Markdown fences or commentary around the object | Write the bare JSON object only |
| Comps look wrong (oversized, waterfront, dormant) | Filters too loose or too tight | `--require`, `--exclude`, `--allow-oceanfront-comps` |
| Module import error | Dependencies not installed | `pip install -r requirements.txt` |
| Report shows someone else's company | No `branding.json` | `cp branding.example.json branding.json` and edit it |

---

## Don't do these things

- Don't commit `.env` or `branding.json`. Keys and identity stay local.
- Don't hardcode API keys in source. Ever.
- Don't rebrand by editing `templates/report.html.j2`. Edit `branding.json`.
- Don't change the template's six-section structure; colors and copy only.
- Don't invent numbers, months, or seasons in narrative copy. See the hard
  rules above.
- Don't weaken `--narratives` validation into a silent fallback. A wrong
  report that looks finished is worse than an error.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q        # hermetic: no network, no API keys
ruff check .     # lint
```

The suite must never need network access or an API key. It runs against real
API responses captured in `tests/fixtures/`. If you add a test that reaches the
network, you have broken CI for everyone.
