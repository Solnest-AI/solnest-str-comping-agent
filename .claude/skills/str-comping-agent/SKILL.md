---
name: str-comping-agent
description: Generate a short-term-rental income analysis report for any property from an Airbnb link, a Zillow/Realtor link, or a plain street address. Use this skill whenever someone says "run comps", "comp this property", "what would this earn on Airbnb", "STR income report", "run the comping agent", "underwrite this STR", "what's the revenue potential", "analyze this property", or pastes an Airbnb/Zillow/Realtor URL or an address with income or investment intent. Also trigger on "rerun the report", "redo the copy", or "make the narrative better" for a property that already has a report on disk. Produces a branded single-file HTML report backed by real comparable listings pulled from AirROI, with revenue, ADR, occupancy, a seasonality curve and an interactive projection calculator. Needs the AirROI and Firecrawl keys from the STR Secrets connections kit. YOU write the narrative copy in pass two; there is deliberately no Anthropic API key.
---

# STR Comping Agent

Turns a property into a client-ready STR income report. The Python pipeline does the
data work: it pulls comparable Airbnb listings from AirROI, scores them on six weighted
categories, filters out the ones that would distort the projection, and renders the HTML.

**You write the words.** The pipeline ships defensible template copy so a report is always
valid on its own, then hands you a brief so you can replace it with something better. That
second pass costs nothing and takes under a second, so always do it.

**Saving means the files on disk.** Everything this skill makes is written to `output/`
the moment it exists. Never `git commit`, push, fork or open a pull request for this
folder, even when the student says "save": it would go to Solnest-AI's GitHub (it did at
the 2026-09-29 summit). Putting a copy on the student's own GitHub is a separate request
they must make in so many words, and it never goes to Solnest-AI.

## Step 0 — the environment (automatic, every time)

Users run this from the Claude Code desktop app and never type into a terminal, so
**you** make sure it can run. Never call bare `python`, `python3` or `pip`: on a fresh
Windows machine `python` is the Microsoft Store stub and opens the Store instead of
running. Every command in this skill starts with:

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py ...
```

Run it from this project's root with the Bash tool. `ensure_env.sh` finds uv (the
connections kit installs it) or installs it per-user with no admin, builds a private
`.venv` with Python 3.13, installs anything missing from `requirements.txt`, and prints
the Python to use. When everything is already there it only checks (under a second).
The first run on a fresh machine downloads Python and the libraries: tell the user it
takes a minute or two, once. If it prints `[setup] FAILED:`, relay that line; it says
what to do. Do not work around it with a system Python.

## Step 1 — verify the connections kit and keys (required, before the first run)

Every student ran the STR Secrets connections kit before the summit, so this is usually a
silent pass. Run it before the first report of a session, and again whenever a run fails
on a key:

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/check_setup.py
```

It finds the kit (next to this folder, Desktop, Documents, Downloads, OneDrive), reads
`AIRROI_API_KEY` and `FIRECRAWL_API_KEY` from the **kit's `.env`, the master copy**, and
tests each with one real request (AirROI $0.01, Firecrawl free). A pass is remembered for
24 hours. Both keys are required: nothing is skipped. Act on the exit code:

- **0 `READY`:** keys work and the report is branded. Go straight to the report.
- **4, keys work but there is no usable `branding.json`** (missing, not valid JSON, or
  still a placeholder name; the output names the field): the report would say "Your
  Company" with no logo. A name alone is a complete brand. Ask the student for their company website (their own site, not a
  listing), then **stop and wait for their answer.** Never fill it in yourself: not from
  their email address, a folder name, another copy's `branding.json` or a web search,
  however obvious it looks. Only the student knows which business their reports go out
  under. With their answer, run
  `PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/brand_from_website.py <website>`.
  It reads their name, logo and colours with Firecrawl and writes `branding.json`. **Look
  at the logo it saves** (`.cache/brand_logo.*`), then show the student the name, logo,
  tagline and colours and ask if that is their brand; change what they say in
  `branding.json` (colours stay `#rrggbb`). No logo found, or the wrong one: ask them
  to right-click their logo on their site, choose Copy image address, and paste it,
  then re-run the same command with `--logo "<that>"`. No website: copy `branding.example.json` to
  `branding.json` and fill it in with them. Never edit the template to rebrand. Then run
  the check again.
- **2, a key is blank or rejected:** it has already opened the kit's `.env` in Notepad
  (Windows) or TextEdit (Mac) and printed which line and where to get the key. Tell the
  student exactly that, one key at a time: paste the key straight after the `=`, save,
  say "saved". Then run the check again. Never ask for a key in chat, never type a key into
  a file yourself, never read one back. `unreachable` means the internet, not the key.
- **3, no kit, or it was never run:** set the kit up for them. Clone
  `https://github.com/Solnest-AI/str-secrets-connections` into this folder's parent (skip
  the clone if the folder is already there), read its `CONNECTIONS.md` and follow it:
  Phase 0, Phase 1, then the AirROI and Firecrawl rows (`connectors/market-airroi.md`,
  `connectors/web-firecrawl.md`). Nothing else in the kit is needed
  for comps. Then come back and run the check again until it says `READY`.

`agent.py` reads the same keys the same way and prints where each came from
(`[Config] AirROI key: connections kit (.env)`). Never open or print `~/.claude.json` or
the kit's `.env` to look for keys yourself. `setup.py` is interactive and cannot run from
the desktop app.

AirROI is the single market-data source. Airbtics was removed 2026-09-20: two providers
meant "the market" meant different things on different client reports, and only the
AirROI market call carries the p25/p75 percentiles the seasonality chart shades as a band.

## The two-pass loop — always run both

### Pass 1 — data

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py --input "<airbnb url | zillow url | street address>"
```

This fetches, scores, filters, renders an HTML report with template copy, and writes two
files into `output/`:

- `<slug>.report-data.json` — the whole assembled report, cached
- `<slug>.narrative-brief.json` — the facts you need to write the copy

Pass 1 spends real money on the user's AirROI key (about 50 cents a report; up to about
1.60 USD when AirROI's own comparables are the wrong kind of listing and it runs a
targeted search, in a market too thin for a market curve) and takes
about 30 seconds. **Run it once per property.** If a `report-data.json` already exists
and the user just wants better wording, skip straight to pass 2.

### Pass 2 — you write the copy

Read `output/<slug>.narrative-brief.json`. It carries the comp table, this market's real
peak and shoulder months, the occupancy median and range across the selected comps, and
the exact JSON shape to return.

Write that JSON to `output/<slug>.narratives.json`, then:

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py --render "output/<slug>.report-data.json" \
    --narratives "output/<slug>.narratives.json"
```

**Pass 2 makes no API calls, spends nothing, and finishes in under a second.** It re-renders
the same HTML with your copy and re-runs the post-render safety gate.

**Nights Listed on the user's own listing.** If pass 1 printed `ASK THE USER: is this
listed year-round`, ask exactly that before pass 2, then wait for the answer. The slider
starts at the listing's open nights on its Airbnb calendar (AirROI), and AirROI cannot see
VRBO or direct bookings, so those and owner stays count as closed: a summit student's
year-round listing started at 200. If they say yes, add `--year-round` to the pass 2
command. It starts the slider at 365, keeps Airbnb's own figure in the report beside it,
and is saved into `report-data.json`, so later re-renders keep it. If they say no, or are
not sure, leave it off. Never add it without asking.

## Co-branding a report for a real estate agent

When the user wants the report to carry a real estate agent's branding next to theirs
("co-brand it", "for my agent", "put Jane's logo on it"), the agent's logo, name and
contact line go in the header and footer. The user's own brand and colours stay in charge.

1. See who is saved: `PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/add_partner.py --list`.
   If the agent is there, skip to step 3.
2. A new agent: ask the user for the agent's name, brokerage, phone, email and website,
   then **stop and wait for the answers.** Never look an agent up or fill any of it in
   yourself: these go on a client document under a real person's name. Anything the user
   does not give is left off. Then run
   `"$PY" scripts/add_partner.py --name "<name>" --brokerage "<brokerage>" --phone "<phone>" --email "<email>" --website "<site>"`.
   **Look at the logo it saves** (`.cache/partner_logo.*`) and show the user the details.
   No logo or the wrong one: ask them to right-click it on the agent's site, Copy image
   address, and re-run with `--logo "<that>"`. Re-running with the same `--name` updates
   that agent and keeps everything else.
3. Add `--partner <agent>` to the report command. On an existing report, the pass 2
   `--render` command takes it for free. It is saved into `report-data.json`, so later
   re-renders keep it. `--no-partner` on a `--render` takes it off.

Agent profiles live in `partners/` in this folder only: a real person's phone and email.

## Pro forma (expenses, NOI, financing)

The report can carry a pro forma under the revenue projection: expense lines, net
operating income, and optional financing (cap rate, debt service, cash flow,
cash-on-cash). **Every expense and financing number is the user's own.** The tool has no
tax, insurance, HOA or expense data for any property: never estimate, look up or fill in
one yourself. A blank line stays blank (NOI waits for it); 0 means "none".

1. **Their standard rates, once.** When the user first wants a pro forma, ask for the
   numbers they use on every property: management %, platform fee %, cleaning cost per
   stay and average nights per stay, repairs and maintenance %, supplies %, and if they
   like their usual down payment %, interest rate and loan term. Wait for the answers.
   Copy `proforma.example.json` to `proforma.json` and put in only what they said (plain
   numbers, `null` for anything not given). From then on every report includes the pro
   forma by itself.
2. **This property's numbers.** If pass 1 printed `[Proforma] ASK THE USER for the blank
   numbers`, ask for those (usually property tax, insurance, HOA, utilities, purchase
   price, furnishing and setup) and wait. Write what they say to
   `output/<slug>.proforma.json` (same keys, plain numbers), then add
   `--proforma "output/<slug>.proforma.json"` to the pass 2 `--render` command. It is
   free and saved into `report-data.json`. Anything still blank shows as "not set" and the
   report says which lines are missing.
3. If the run says the purchase price was pre-filled from the listing page, tell the user
   and ask whether to replace it with their offer price.
4. `--proforma` alone adds it with only the saved rates; `--no-proforma` leaves it out.
   Anyone viewing the HTML report can also change the numbers in place before printing.

## Rules for the narrative copy

The report is a client deliverable attached to real money decisions. These are not style
preferences.

1. **Never invent a number.** Every figure you write must appear in the brief. If you want
   to say occupancy runs 60%, the brief must say 60%. Do not round a median into a range
   or infer a trend the data does not show.
2. **Never state an occupancy or revenue figure outside the comp set's observed range.**
   The brief gives you the median, min and max. Stay inside them, and **round bounds
   inward**: a low of 61.4% is "61.5%" or "about 62%", never "61%". Rounding a bound
   outward silently widens the range you are claiming.
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
| `--proforma [FILE]` / `--no-proforma` | Add the pro forma (saved rates in `proforma.json`, plus this property's numbers from FILE), or leave it out. Free on `--render`. See pro forma above. |
| `--partner <agent>` / `--no-partner` | Co-brand with a real estate agent saved by `scripts/add_partner.py`, or take it off. Free on `--render`. See co-branding above. |
| `--year-round` | The user said the property is listed all year (Airbnb plus VRBO, direct or other sites). Starts Nights Listed at 365. Free on `--render`. Only after asking (see pass 2). |
| `--pin-comps <ids>` | The user wants specific listings in the comp set (e.g. a unit in the same building). AirROI listing ids, in order. |
| `--address-label "..."` | The found address reads badly ("5050 VALLEY Drive Unit# 2"): the report shows this instead. |
| `--sqft N` | The listing's square footage is wrong or missing. |
| `--hero-url <file>` | A local photo instead of a URL: embedded in the report so it cannot break later. |
| `--output-dir <path>` | A test run that must stay out of the user's reports folder. |
| `--pdf` | The user wants a PDF too. Needs Playwright, see `report/pdf_export.py`. |

A report already on disk is never replaced: a same-day re-run is saved with `-2`, `-3`, and
pass 2 rewrites the report its own pass 1 wrote. Operator options in `.env` (`.env.example`
lists them, all off when blank) can add Conservative / Base / Optimistic tier cards
(`PROJECTION_TIERS`), an STR-permitting check (`PERMITTING_CHECK`), always-excluded comp
keywords (`COMP_EXCLUDE`) and a file-name prefix (`REPORT_FILENAME_PREFIX`). With tiers on,
the Base Case is the headline: quote it, and the Conservative to Optimistic range, in your copy.

## Reading the output

Tell the user what actually happened, not just that it finished:

- **Whether a targeted search ran** (`[Targeted]` in the log). AirROI's comparables
  endpoint only knows location and size, so when its listings are the wrong kind
  (ski-in/ski-out comps for a cabin with no ski access, hot-tub comps for a house
  without one) the agent buys one extra search for listings that match. Say so, and
  say how many comparables still carry a feature the subject lacks: the report
  discloses it, and the user should hear it from you first.
- **Comps are picked for similarity, not performance.** A quiet comp that matches the
  subject beats a busy one that does not. Never describe the comp set as "top
  performers".

- **How many comps survived filtering, and why any were dropped.** The run prints this.
  A report built on a heavily filtered pool is weaker and the user should know.
- **The seasonality source.** The AirROI market curve (p50 with a p25-p75 band) is
  the normal path. A per-comp average or a revenue-distribution fallback is weaker
  and worth naming.
- **Anything the sanity gate flagged.**
- **"Update available: Comping Agent X.Y.Z"** as the last line: a newer release is out.
  Tell the user, once, and offer it. Never update without their yes. On a yes, follow
  `CLAUDE.md` > "When the user says "update the comping agent"" in this folder: it runs
  `scripts/update.py`, which keeps their keys, branding, agents and reports, and
  `scripts/update.py --undo` goes back.

**Address input:** the search rejects listings at another street number and prefers the
exact unit. If it can only find a different unit in the same building, the run stops with
`Asked for Unit 13, but the best listing found is Unit 12` before anything is spent on
AirROI. Tell the user which unit was found and ask: if that unit is a fair stand-in (same
building and layout), re-run the same command with `--allow-other-unit` and say in your
summary that the report is built on that unit; otherwise ask for their unit's own Airbnb,
Zillow or Realtor link and use it as `--input`. Never add `--allow-other-unit` on your own. If it stops with
`Missing required fields: --beds, --baths, --guests` (the listing page did not say),
nothing has been spent: ask the user for those numbers and re-run with them. Never guess
them or copy them from a neighbouring unit.
Zillow, Realtor and Redfin sale listings almost never say how many guests a home sleeps,
so expect `--guests` to be asked for on most of them.

**Multi-unit building:** if it stops with `listed as a multi-unit building` (duplex,
triplex, a house split into apartments), nothing has been spent. One comp set for the
whole building would price it as one big house. Ask the user which unit to comp and its
bedrooms, bathrooms and guest count, then re-run with `--beds N --baths N --guests N`.
For the whole building, comp each unit separately and add them up in your summary.
If the run stops with `Subject photo refused` (an address often
resolves to a local rental company's site), nothing has been spent on AirROI yet: ask
the user for a photo of the property on Airbnb, Zillow, Realtor.ca or Redfin (right-click
the photo, Copy image address) and re-run the same command with `--hero-url "<that>"`.

If Phase A blocks the run, do not try to force it through. It blocks because a comp is
missing data the report needs, and shipping a broken report is worse than none.

## When there is no usable comp set

Some markets are genuinely too thin: rural areas, brand-new listings, places where AirROI
coverage is sparse. The agent will tell you. **Say so plainly rather than producing a
report on three weak comps.** A confident-looking report built on a thin pool is the
failure mode that costs the user credibility with their own client.
