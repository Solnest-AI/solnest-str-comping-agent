# STR Comping Agent: Setup

A Python CLI that generates a branded HTML short-term-rental income report for
any property. Drop in an Airbnb URL, a listing URL, or just an address. You get
back a report with comps, seasonality, revenue projections, and an interactive
calculator.

**You need one API key: AirROI.** Everything else is optional.

---

## The easy way

1. Put this folder somewhere on your computer (Desktop is fine).
2. Open Claude Code in this folder.
3. Type:

   ```
   set this up
   ```

Claude reads `CLAUDE.md`, installs dependencies, and walks you through the key
one step at a time. You don't need to know Python.

---

## What you'll need

| Service | Required? | Where to get it | Cost |
|---|---|---|---|
| **AirROI** | **YES** | https://www.airroi.com/api/developer/activate | Free tier available |
| Firecrawl | optional | https://www.firecrawl.dev | Free tier available |
| Airbtics | optional | https://airbtics.com | Skip unless you have an account |
| Gmail app password | optional | https://myaccount.google.com/apppasswords | Free |

**There is no Anthropic key.** The report's written analysis is produced by
Claude Code using the Claude you already have. Nothing extra to buy, no
per-report API cost.

### What each optional key actually unlocks

- **Firecrawl.** Lets you pass a *street address* or a *Zillow/Realtor URL*.
  Without it, those inputs fail, but Airbnb URLs work fine because they resolve
  entirely through AirROI. Add it if you analyze off-market properties.
- **Airbtics.** A market-level seasonality overlay. Without it, seasonality is
  derived from AirROI's own monthly revenue distribution. Most people skip it.
- **Gmail.** Only for `--email`. Reports always save to `output/` as HTML
  either way. Use an **app password**, never your account password.

---

## Manual setup

1. Install Python 3.10+ from https://www.python.org/downloads/
2. Open a terminal in this folder.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Add your key. Either run the interactive builder:
   ```bash
   python setup.py
   ```
   or copy the template and edit it:
   ```bash
   cp .env.example .env
   ```
5. Set your branding:
   ```bash
   cp branding.example.json branding.json
   ```
   Then edit `company_name`, `tagline`, `logo_url`, `website_url`, and the two
   colors. Both `.env` and `branding.json` are gitignored.
6. Run it:
   ```bash
   python agent.py --input "https://www.airbnb.com/rooms/39508095"
   ```

The HTML report drops into `output/`. Open it in any browser.

---

## How to use it

```bash
# Airbnb listing URL: best results
python agent.py --input "https://www.airbnb.com/rooms/39508095"

# Plain address (needs Firecrawl)
python agent.py --input "5005 Valley Drive Unit 13, Sun Peaks BC"

# Zillow / Realtor URL (needs Firecrawl)
python agent.py --input "https://www.zillow.com/homedetails/..."

# Override property details
python agent.py --input "<address>" --beds 4 --baths 3 --guests 8

# Email it when done
python agent.py --input "<address>" --email buyer@example.com
```

`python agent.py --help` lists every flag. The full table is in `README.md`.

### What you get

A self-contained HTML file with:

- Subject property details and hero image
- Annual revenue, ADR, and occupancy projections in three tiers
- Six ranked comparable Airbnb listings with real trailing-twelve-month numbers
- A seasonality chart built from the market's real monthly revenue distribution
- An interactive revenue calculator
- A methodology section showing how the numbers were derived

**These are estimates derived from comparable listings, not forecasts or a
valuation.** See the "Read this before you trust a number" section of
`README.md`.

---

## Finishing the report: the narrative handoff

Your first run produces a complete report whose written sections are generic
template copy, plus a file called `output/<slug>.narrative-brief.json`.

To replace the boilerplate with real analysis, paste this into Claude Code (the
agent prints the exact paths for you):

```
Read output/<slug>.narrative-brief.json
Write the narratives it asks for to output/<slug>.narratives.json
Then re-run the agent with --narratives pointing at that file.
```

Then:

```bash
python agent.py --input "<same input>" --narratives "output/<slug>.narratives.json"
```

That is the whole loop. The brief tells Claude exactly what it is allowed to
write from, so the copy cannot invent numbers or seasons.

---

## Troubleshooting

| Message | What it means | Fix |
|---|---|---|
| `AIRROI_API_KEY is not set` | No `.env`, or the key is blank | `cp .env.example .env` and paste your key, or run `python setup.py` |
| `FIRECRAWL_API_KEY is not set` | You passed an address or listing URL without Firecrawl | Add the key, or use an Airbnb URL |
| `Phase A sanity failed: <6 comps` | Not enough comparable listings nearby | Try `--no-feature-filter`, or a denser market. There is no radius lever: AirROI caps results at 25 and only accepts a 1-10 mile radius, so widening is not possible. |
| `--narratives file not found` | You used `--narratives` before writing the file | Run once without it to generate the brief |
| `not valid JSON` | The narratives file has markdown fences or commentary | Write the bare JSON object only |
| Report shows the wrong company | No `branding.json` | `cp branding.example.json branding.json` and edit it |
| `ModuleNotFoundError` | Dependencies not installed | `pip install -r requirements.txt` |

### Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

They run offline against captured API responses. No keys needed.
