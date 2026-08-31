---
name: str-comping-agent
description: Generate a short-term-rental income analysis report for any property from an Airbnb link, a Zillow/Realtor link, or a plain street address. Use this skill whenever someone says "run comps", "comp this property", "what would this earn on Airbnb", "STR income report", "run the comping agent", "underwrite this STR", "what's the revenue potential", "analyze this property", or pastes an Airbnb/Zillow/Realtor URL or an address with income or investment intent. Also trigger on "rerun the report", "redo the copy", or "make the narrative better" for a property that already has a report on disk. Produces a branded, self-contained HTML report backed by real comparable listings pulled from AirROI, with revenue, ADR, occupancy, a seasonality curve and an interactive projection calculator. Requires only an AIRROI_API_KEY. YOU write the narrative copy in pass two; there is deliberately no Anthropic API key.
---

# STR Comping Agent

Turns a property into a client-ready STR income report. The Python pipeline does the
data work: it pulls comparable Airbnb listings from AirROI, scores them on six weighted
categories, filters out the ones that would distort the projection, and renders the HTML.

**You write the words.** The pipeline ships defensible template copy so a report is always
valid on its own, then hands you a brief so you can replace it with something better. That
second pass costs nothing and takes under a second, so always do it.

## Prerequisites

1. **`AIRROI_API_KEY` in `.env`** — the only required key. Get one at
   https://www.airroi.com/api/developer/activate. If it is missing, `agent.py` prints
   setup instructions and exits; walk the user through `SETUP.md` rather than guessing.
2. **`FIRECRAWL_API_KEY`** — optional, and only needed for a street address or a
   Zillow/Realtor link. An Airbnb URL resolves entirely through AirROI.
3. **`AIRBTICS_API_KEY`** — optional. Adds a real market seasonality curve. Without it,
   seasonality is derived from AirROI's monthly revenue distribution.

Never ask the user for a key value in chat. Point them at `python setup.py`.

## The two-pass loop — always run both

### Pass 1 — data

```bash
python agent.py --input "<airbnb url | zillow url | street address>"
```

This fetches, scores, filters, renders an HTML report with template copy, and writes two
files into `output/`:

- `<slug>.report-data.json` — the whole assembled report, cached
- `<slug>.narrative-brief.json` — the facts you need to write the copy

Pass 1 spends real money on the user's AirROI key (roughly $0.40 a report) and takes
about 30 seconds. **Run it once per property.** If a `report-data.json` already exists
and the user just wants better wording, skip straight to pass 2.

### Pass 2 — you write the copy

Read `output/<slug>.narrative-brief.json`. It carries the comp table, this market's real
peak and shoulder months, the occupancy median and range across the selected comps, and
the exact JSON shape to return.

Write that JSON to `output/<slug>.narratives.json`, then:

```bash
python agent.py --render "output/<slug>.report-data.json" \
                --narratives "output/<slug>.narratives.json"
```

**Pass 2 makes no API calls, spends nothing, and finishes in under a second.** It re-renders
the same HTML with your copy and re-runs the post-render safety gate.

## Rules for the narrative copy

The report is a client deliverable attached to real money decisions. These are not style
preferences.

1. **Never invent a number.** Every figure you write must appear in the brief. If you want
   to say occupancy runs 60%, the brief must say 60%. Do not round a median into a range
   or infer a trend the data does not show.
2. **Never state an occupancy or revenue figure outside the comp set's observed range.**
   The brief gives you the median, min and max. Stay inside them.
3. **Use the market's real season.** The brief carries the derived peak and shoulder
   months for this specific market, computed from its own revenue distribution. Never
   assume a ski season, a summer season, or any calendar you were not given.
4. **These are estimates from comparable listings, not guarantees.** Write like an analyst,
   not a broker. No "guaranteed", no "you will earn", no superlatives the data cannot carry.
5. **Match the counts the layout expects**: 4 `amenity_badges`, 3 `positioning_cards`.
   The renderer warns if you supply a different number, and the layout will look wrong.
6. **Do not name or describe a specific neighbouring property as if it were identifiable.**
   AirROI returns approximate coordinates, and the comps are anonymised on the card by design.

## Useful flags

| Flag | Use it when |
|---|---|
| `--beds` / `--baths` / `--guests` | The address path guessed the configuration wrong. Always sanity-check these against what the user told you. |
| `--require pool,hot_tub` | Force must-have features. Auto-detected from the subject otherwise. |
| `--no-feature-filter` | The pool is too thin and you would rather have more comps than perfect ones. |
| `--allow-oceanfront-comps` | The subject is inland but you deliberately want waterfront comps in the set. |
| `--subject-on-water` | The subject IS on the water, so waterfront comps should be kept. |
| `--exclude "keyword"` | Drop a specific comp by name keyword. |
| `--market "Name"` | The market was misidentified. |
| `--email someone@example.com` | Send the finished report. Needs Gmail configured. |

## Reading the output

Tell the user what actually happened, not just that it finished:

- **How many comps survived filtering, and why any were dropped.** The run prints this.
  A report built on a heavily filtered pool is weaker and the user should know.
- **The seasonality source.** Airbtics is the strongest; an AirROI revenue-distribution
  fallback is weaker and worth naming.
- **Anything the sanity gate flagged.**

If Phase A blocks the run, do not try to force it through. It blocks because a comp is
missing data the report needs, and shipping a broken report is worse than none.

## When there is no usable comp set

Some markets are genuinely too thin: rural areas, brand-new listings, places where AirROI
coverage is sparse. The agent will tell you. **Say so plainly rather than producing a
report on three weak comps.** A confident-looking report built on a thin pool is the
failure mode that costs the user credibility with their own client.
