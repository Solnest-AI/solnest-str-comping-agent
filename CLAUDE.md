> **Using this in Claude Code?** The skill at `.claude/skills/str-comping-agent/SKILL.md` is the operating manual and it drives the two-pass loop for you. This file covers setup and troubleshooting.

# STR Comping Agent: operating manual for Claude Code

A Python CLI that turns a property (Airbnb URL, Zillow/Realtor URL, or a street
address) into a branded single-file HTML income report (needs internet to
show photos, fonts and chart) built from real
comparable Airbnb listings.

You are the narrative engine for this tool. It ships with **no Anthropic API
key** on purpose: the user already has you. Read the narrative handoff section
below before you run anything, because it changes what a "finished" report means.

---

## When the user says "set this up"

Users run this from the Claude Code desktop app. They never type into a
terminal: you run every command yourself with the Bash tool, from this folder.
Confirm each step before moving on.

**Never commit, push, fork, open a pull request or create a GitHub repo for
this folder.** It is the student's own copy of a tool, not a project. When
they say "save", everything is already saved on disk: say so. At the
2026-09-29 summit a student's "save" became a push to Solnest-AI's GitHub.
Only if the student explicitly asks to put their copy on their own GitHub
account is that a separate job, and it never involves Solnest-AI.

### Step 0: Detach from GitHub (automatic, first)

```bash
bash scripts/detach_from_github.sh
```

A `git clone` links this folder to Solnest-AI's GitHub. This removes that link
and the `.git` folder, so nothing here can be pushed back. It only touches an
untouched copy of the public repo (never a working copy with edits, branches
or unpushed commits) and always exits 0: relay the one line it prints. The
tool never needs git to run.

**Never run bare `python`, `python3` or `pip`.** On a fresh Windows machine
`python` is the Microsoft Store stub and opens the Store instead of running.
Every Python command in this file goes through `scripts/ensure_env.sh`, which
prints the Python to use:

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py ...
```

Wherever this file shows `"$PY" something.py`, run it with that prefix.

**Never open, parse or print `~/.claude.json`** (or any other file holding
keys) to check for the connections kit. It holds the user's keys. `agent.py`
finds them itself and prints only where they came from.

### Step 1: Python and dependencies (automatic)

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" --version
```

That one command does the whole install. It finds uv (the connections kit
installs it) or installs it per-user with no admin, builds a private `.venv`
with Python 3.13 (uv downloads it; the machine's own Python, or lack of one,
does not matter), and installs `requirements.txt`. The first run on a fresh
machine takes a minute or two; tell the user that. After that it only checks,
in under a second, and reinstalls by itself when `requirements.txt` changes.
If it prints `[setup] FAILED:`, relay that line: it says what to do.

**Windows with no Bash tool** (Claude answers in PowerShell): everything here
runs in Git Bash. First check whether Git is installed at all
(`Test-Path "C:\Program Files\Git\bin\bash.exe"`):

- **Not installed:** follow the connections kit's `connectors/system-git.md` to
  install it (it works from PowerShell), then have the student fully quit and
  reopen Claude Code, and start again.
- **Installed, but Claude Code did not find it:** follow the kit's
  `CLAUDE_CODE_GIT_BASH_PATH` fix in the same file (section 3: merge that one
  `env` entry into `~/.claude/settings.json`, keeping everything else), then
  quit and reopen. To keep going in this session, run every command
  through a **login** shell, which
  loads Git's own PATH; without `-l`, `dirname` / `uname` are not found:
  `& "C:\Program Files\Git\bin\bash.exe" -l -c 'cd "<this folder>" && PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/check_setup.py'`

### Step 1b: Install the launcher now, before anything can fail (required)

This project's skill only loads when Claude Code is opened on this folder, and
users will not do that: they cloned it from another window. Without the
launcher a later "run comps on ..." never finds the tool and improvises comps
from web searches instead (measured: no report, $6.48 spent). It needs only
Step 1, so install it straight away, so that a key problem or a sample listing
that fails later cannot leave the student without it:

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/install_launcher.py
```

It prints where it installed (`~/.claude/skills/str-comping-agent/`). If the
folder is ever moved, run it again from the new location. It works in local
Claude Code sessions (desktop app or terminal), not cloud sessions.

### Step 2: The connections kit and keys (required, nothing skipped)

Every student ran the STR Secrets connections kit before the summit. Verify it
did its job:

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/check_setup.py
```

It finds the kit, reads `AIRROI_API_KEY` and `FIRECRAWL_API_KEY` from the
**kit's `.env` (the master copy)**, and tests both for real. Both are required:
AirROI is the comp data, Firecrawl is street addresses. Act on its exit code:

- **0 `READY`:** keys work and the report is branded. Go to Step 4.
  If it lists other copies of the kit, ask the student which folder they set
  up; if it is not the one in use, re-run the check with
  `STR_SECRETS_KIT="<that folder>"` in front. The kit that passes is remembered.
- **4:** keys work but `branding.json` is missing, not valid JSON, or still a
  placeholder name; the output names the field. Go to Step 3.
- **2:** a key is not working. Blank or rejected: the kit's `.env` is already
  open in Notepad / TextEdit and the output says which line and where to get
  the key. Tell the student that, one key at a time: paste straight after the
  `=`, save, say "saved". Out of credit: top up at the vendor, the key stays.
  Rate limited, vendor error or unreachable: no key change, wait and re-run.
  Run the check again.
- **3:** no kit on this computer, or it was never run. Set it up for them:
  clone `https://github.com/Solnest-AI/str-secrets-connections` into this
  folder's parent (skip if it is already there), read its `CONNECTIONS.md` and
  follow it: Phase 0, Phase 1, then the AirROI and Firecrawl rows
  (`connectors/market-airroi.md`, `connectors/web-firecrawl.md`). Then run the
  check again until it says `READY`.

| Key | Required? | Where to get it |
|---|---|---|
| `AIRROI_API_KEY` | **YES** | https://www.airroi.com/api/developer (needs the $10 credit deposit) |
| `FIRECRAWL_API_KEY` | **YES** | https://www.firecrawl.dev/app/api-keys |
| `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` | No | https://myaccount.google.com/apppasswords (only for `--email`) |

**There is no `ANTHROPIC_API_KEY` step.** If the user offers one, tell them it
is not needed and that you write the narratives yourself. A key already in
their environment is ignored; the API is used only with `NARRATIVE_MODE=api`. Never ask for a key in
chat, never type one into a file yourself, never read one back.

### Step 3: Set the branding

The report is white-label: every colour, the logo, the name and the links come
from `branding.json`. Without it the report says "Your Company" with no logo, so
`check_setup.py` stops with exit 4 (and every run stops) until it holds a real
company name. A name alone is a complete brand; logo, website and colours are
optional. Build it from the student's
own website:

1. Ask the student for their company website (their own site, not a listing).
2. Run:
   ```bash
   PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/brand_from_website.py <their website>
   ```
   It reads the site's brand with Firecrawl (the kit's key), checks the logo
   loads, picks brand colours dark enough to read on the report's cream
   background (a dark site's "primary" is often its cream text), and puts a
   dark site's light logo on its own background colour (`logo_background`).
3. **Look at the logo it saved** (`.cache/brand_logo.*`), then show the student
   the name, logo, tagline and colours and ask if that is their brand. Change
   what they say in `branding.json`; colours stay `#rrggbb`. No logo found
   (some sites draw it in code), or the wrong one: ask the student to
   right-click their logo on their site, choose Copy image address, and paste
   it, then re-run step 2 with `--logo "<that>"`. It checks the image loads and
   puts a white logo on a dark plate so it shows on the report.
4. Run `scripts/check_setup.py` again: it says `READY`.

Exit 3 from the script means the site could not be read: ask for another page,
or copy `branding.example.json` to `branding.json` and fill it in with them.
Do **not** edit the template to rebrand.

### Step 4: Tell them they're set up

Setup is done once `check_setup.py` says `READY` and the launcher is installed
(Step 1b). Tell the user, in these words: "You're set up. From any Claude Code
window, just say: run comps on <an Airbnb link, a Zillow link or an address>.
If it ever doesn't pick that up, type /str-comping-agent." Also give them this
folder's path.

### Step 5: First report

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py --input "https://www.airbnb.com/rooms/39508095"
```

The HTML lands in `output/`. Then do the narrative handoff below. The first
report is not finished until you have. If this sample listing fails (sparse
market, delisted, vendor hiccup), setup is still complete: say so, and try the
student's own property instead. Never make up comps to fill the gap.

---

## When the user says "update the comping agent"

Their copy is a plain folder (Step 0), so it updates by download, not
`git pull`. `branding.json`, `.env`, `.venv`, `output/` and `.cache/` are not
in the download and stay as they are:

```bash
T="$(mktemp -d)" && curl -fsSL -o "$T/c.tar.gz" https://github.com/Solnest-AI/solnest-str-comping-agent/archive/refs/heads/main.tar.gz && tar -xzf "$T/c.tar.gz" -C "$T" && cp -R "$T/solnest-str-comping-agent-main/." . && rm -rf "$T"
```

Then run Step 1 (it reinstalls anything new) and `scripts/check_setup.py`.

---

## The narrative handoff loop: READ THIS

Without an Anthropic key the agent still produces a complete, valid report, but
its narrative sections are data-driven **template copy**. Your job is to replace
them. The loop is three steps.

### 1. Run the agent

```bash
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py --input "<property>"
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

`amenity_badges` and `positioning_cards` are required too: the loader rejects
the file without them (the template would render holes).

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
PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py --render "output/<slug>.report-data.json" --narratives "output/<slug>.narratives.json"
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
| `--hero-url URL` | Supply the hero photo when the listing scrape is blocked, or when the run stops with "Subject photo refused" |
| `--allow-other-unit` | Accept a listing for a different unit at the same street address. The address search stops before AirROI and asks otherwise; only pass it after the user confirms that unit is a fair stand-in |
| `--listing-url URL` | Link the report's "View Listing" button |
| `--render DATA_JSON` | Re-render a report from its `*.report-data.json`, no AirROI calls. Pair with `--narratives` |

Run `"$PY" agent.py --help` if a flag here disagrees with the code; the code wins.

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
| `comp_scorer.py` | Hard gates, scoring, ranking; picks the final comp set by similarity, never by performance |
| `comp_similarity.py` | Premium features both ways, rarity-weighted amenity overlap, property type, bathrooms, minimum stay, targeted-search filter |
| `data/amenity_prevalence.json` | How common each AirROI amenity is (rarity weights); rebuilt by `scripts/build_amenity_prevalence.py` |
| `generators/calculator.py` | Calculator defaults, seasonal data, season labels |
| `generators/narrative_brief.py` | **The narrative contract + the brief written for you** |
| `generators/narratives.py` | Template copy, and `--narratives` file loading/validation |
| `generators/methodology.py` | Methodology footnote |
| `validators/sanity.py` | Phase A/B gates that block a bad report |
| `report/template_engine.py` | Renders Jinja2 → HTML |
| `report/email_sender.py` | Optional Gmail SMTP delivery |
| `templates/report.html.j2` | The report template |
| `scripts/brand_from_website.py` | Builds `branding.json` (name, logo, colours) from the student's website via Firecrawl |
| `scripts/package.py` | Builds the distribution zip from the git manifest |

### Data semantics you must not get wrong

These came from auditing 200 live AirROI records. Getting them backwards
produces a confidently wrong report:

- `ttm_available_days` is **unsold nights**, not "days listed". A high value
  means the listing barely booked. The code uses `nights_booked` /
  `nights_listed` instead, and there is deliberately no field named
  `days_available`.
- `occupancy_pct` is **adjusted** occupancy: booked ÷ open nights.
- `annual_revenue` is **fee-inclusive**. Never divide it by `adr` to
  reconstruct nights.
- **`adr` (`ttm_avg_rate`) is not the rate guests paid.** Measured 2026-09-25
  on 30 comps against `/listings/metrics/all` monthly sums: off by -13.8% to
  +18.9%, 9 of 30 beyond 5%. Never multiply it by nights and never show it as
  a rate. Room revenue is `ttm_revpar x ttm_total_days` (see below); the rate
  paid is room revenue / `nights_booked`.
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
- **`ttm_revpar` is ROOM revenue (fees excluded) / `ttm_total_days`**, rounded
  to 0.1. Corrected 2026-09-25: this note used to say it reconciled to nothing,
  because it was compared with fee-inclusive revenue; its 0.80-0.97 ratio is
  the room share. `ttm_revpar x ttm_total_days` matched the monthly room
  revenue within $18 on 30/30 comps (median $1; 0.05 x 365 = $18.25 is the
  rounding), so the comp cards split revenue into room + fees for free
  instead of buying six `/listings/metrics/all` calls ($0.60).
  `ttm_adjusted_revpar x open nights` matches too. `revenue_per_listed_night`
  stays fee-inclusive and is still computed locally.

Market endpoints accept only `usd` or `native` for `currency` and 422 on
`cad`; `/price-recommendation/*` accepts `cad` fine. The full OpenAPI spec is
at `https://www.airroi.com/openapi.json` (24 endpoints); the docs site does not
link it and `/api/openapi.json` 404s.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AIRROI_API_KEY is not set` | The kit's `.env` has no working key | Run `scripts/check_setup.py` (Step 2); it opens the kit's `.env` for them |
| `FIRECRAWL_API_KEY is not set` | The kit's `.env` has no working Firecrawl key | Run `scripts/check_setup.py` (Step 2); it opens the kit's `.env` for them |
| `[setup] AirROI rejected the key` / `is out of credit` (or Firecrawl) | The key stopped working mid-run (the run stops; the 24h pass is forgotten) | Run `scripts/check_setup.py` (Step 2); out of credit means topping up at the vendor, not a new key |
| `Phase A sanity failed: <6 comps` | Too few comparable listings in this market | Relax with `--no-feature-filter`, or try a denser market. AirROI caps comparables at 25 and accepts only a 1-10 mile radius, so there is no way to widen the pool. |
| Report renders but the prose is generic | You have not done the narrative handoff | Read `output/<slug>.narrative-brief.json` and re-run with `--narratives` |
| `--narratives file not found` | Ran with `--narratives` before writing the file | Run once without it to generate the brief |
| `NarrativeFileError: ... not valid JSON` | Markdown fences or commentary around the object | Write the bare JSON object only |
| Comps look wrong (oversized, waterfront, dormant) | Filters too loose or too tight | `--require`, `--exclude`, `--allow-oceanfront-comps` |
| Module import error, or `python` opens the Microsoft Store | Ran bare `python` instead of the `.venv` | Always prefix with `PY="$(bash scripts/ensure_env.sh)" &&` and run `"$PY"` |
| `listed as a multi-unit building` (the run stops, nothing spent) | A duplex or a house split into apartments; the listing's size is the whole building | Ask which unit to comp and re-run with `--beds N --baths N --guests N` for it; comp each unit to value the building |
| `Missing required fields: --guests` on a Zillow/Realtor link | Sale listings do not state guest capacity, and it is never guessed | Ask the user how many guests it sleeps and re-run with `--guests N` |
| `[setup] FAILED: ...` | No internet, or uv/Python blocked on this machine | Do what the message says, then run the same command again |
| `[Branding] No branding.json yet` or `branding.json is not usable yet` (the run stops, nothing spent) | The student has not been branded, or the file is broken / still a placeholder | Ask for their website and run `scripts/brand_from_website.py` (Step 3) |
| Report says "Your Company" / no logo | No `branding.json` | Ask for the student's website and run `scripts/brand_from_website.py` (Step 3) |

---

## Don't do these things

- Don't commit, push, fork or open pull requests for a student's copy, even
  when they say "save". Saving means the files on disk. See Step 0.
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
PY="$(bash scripts/ensure_env.sh --dev)"   # adds pytest + ruff to the .venv
"$PY" -m pytest -q                          # hermetic: no network, no API keys
"$PY" -m ruff check .                       # lint
```

The suite must never need network access or an API key. It runs against real
API responses captured in `tests/fixtures/`. If you add a test that reaches the
network, you have broken CI for everyone.
