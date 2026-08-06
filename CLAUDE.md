# STR Comping Agent — Setup & Usage Guide

This is an STR (short-term rental) income analysis report generator. It takes a property (Airbnb URL, Zillow URL, or street address), finds comparable Airbnb listings, scores them, and generates a branded HTML report with revenue projections.

## First-Time Setup

**Check if setup is needed:** Look for a `.env` file in this directory. If it doesn't exist, walk the user through setup step by step. Do NOT skip steps. Do NOT move to the next step until the current one is confirmed.

### Step 1: Install Python Dependencies

```bash
pip install -r requirements.txt
```

### Step 2: Create the .env File

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Then open the `.env` file in the user's editor so they can see it. Walk through each API key below, one at a time. For each key:
1. Tell the user where to get it
2. Tell them which line in the `.env` file to paste it on
3. Wait for them to confirm they've saved the file before moving on

**IMPORTANT: Do NOT ask the user to paste API keys into the chat. Always direct them to paste into the `.env` file directly. Open the file for them so they can see exactly where each key goes.**

### Step 3: AirROI API Key (REQUIRED)

This is the primary data source — property estimates, comparable listings, and performance metrics. **Every user needs their own account.**

1. Open the `.env` file for the user
2. Tell them: "Go to https://www.airroi.com/api/developer/activate, create an account, and copy your API key"
3. Tell them: "Paste your key on the `AIRROI_API_KEY=` line in the .env file and save"
4. Wait for confirmation

**This key is required. The agent cannot run without it.**

### Step 4: Firecrawl API Key (REQUIRED)

Used to search the web for property listings when given a street address. The free tier gives you ~500 pages.

1. Tell them: "Go to https://www.firecrawl.dev, sign up, and copy your API key"
2. Tell them: "Paste your key on the `FIRECRAWL_API_KEY=` line in the .env file and save"
3. Wait for confirmation

### Step 5: Apify Token (RECOMMENDED)

Used for reliable Zillow scraping — prevents hallucinated property data. Free tier available.

1. Tell them: "Go to https://console.apify.com/account#/integrations and copy your API token"
2. Tell them: "Paste your token on the `APIFY_TOKEN=` line in the .env file and save"
3. Wait for confirmation

**They can skip this, but Zillow lookups will be less reliable.**

### Step 6: Google Maps API Key (OPTIONAL)

Used for Google Street View photos when a property has no listing images online.

1. Tell them: "Go to https://console.cloud.google.com/apis/library/street-view-image-backend.googleapis.com and enable the Street View Static API"
2. Tell them: "Go to https://console.cloud.google.com/apis/credentials and create or copy an API key"
3. Tell them: "Paste your key on the `GOOGLE_MAPS_API_KEY=` line in the .env file and save"
4. Wait for confirmation

**They can skip this. Properties without photos will show a placeholder.**

### Step 7: Branding Setup

Ask the user these questions one at a time:

1. **What's your company name?** (e.g., "Acme Vacation Rentals")

2. **Do you have a logo you'd like to use?**
   - If YES and they have a **direct URL** to their logo image → use it
   - If YES but they only have their **website URL** → scrape the website to find their logo:
     - Fetch the website HTML
     - Look for the logo in these locations (in order):
       - `<link rel="icon">` or `<link rel="apple-touch-icon">` (favicon/app icon)
       - `<meta property="og:image">` (Open Graph image)
       - `<img>` tags in the header/nav with "logo" in the src, alt, or class name
       - Any `<img>` with "logo" in the filename
     - Show the user what you found and confirm it's their logo
   - If NO → leave blank, the report will show their company name as text instead

3. **What's your website URL?** (can leave blank if they don't have one)

4. **What's your brand color?**
   - If they provided a website, scrape it and extract the primary brand color from:
     - CSS custom properties (--primary-color, --brand-color, etc.)
     - The most prominent non-white, non-black color in the header/nav
     - The color used on buttons or links
   - Show the user what you found: "I found this color from your website: #2c5282 — want to use it?"
   - If they don't have a website, ask them to describe their brand color (e.g., "dark blue", "forest green") or provide a hex code
   - Default to `#1f3c34` if they don't have a preference

5. **What's your tagline?** (e.g., "Premium Vacation Home Management" — or leave as default)

Save their answers to `branding.json`:
```json
{
  "company_name": "Their Company Name",
  "tagline": "Their Tagline or Short-Term Rental Management",
  "logo_url": "https://their-logo-url.com/logo.png",
  "website_url": "https://www.theirwebsite.com",
  "primary_color": "#2c5282",
  "accent_color": "#4299e1"
}
```

For the accent color, automatically derive it from the primary color — use a lighter/softer version of the same hue.

### Step 8: Verify Setup

Run a test comp to make sure everything works:
```bash
python agent.py --input "1883 Pointe Drive, Panama City Beach, FL 32407" --beds 3 --baths 2 --guests 8 --market "Panama City Beach"
```

If it generates a report in the `output/` folder, setup is complete.

---

## How to Comp a Property

The user can provide any of these:

### Airbnb URL
```bash
python agent.py --input "https://www.airbnb.com/rooms/12345678"
```
Best results — pulls all property data directly from AirROI.

### Zillow URL
```bash
python agent.py --input "https://www.zillow.com/homedetails/123-Main-St/12345_zpid/"
```
Uses Apify for reliable data extraction.

### Street Address
```bash
python agent.py --input "123 Main St, Nashville, TN 37203" --beds 3 --baths 2 --guests 8 --market "Nashville"
```
For addresses, provide `--beds`, `--baths`, and `--guests` if the property isn't listed online. The `--market` flag helps with comp accuracy.

### Optional Flags
- `--email user@example.com` — Email the report after generating
- `--hero-url "https://..."` — Provide a custom property photo
- `--listing-url "https://..."` — Link to the property listing (shown as "View Listing" button)
- `--market "City Name"` — Override market detection
- `--currency "$"` or `"CA$"` — Override currency (auto-detected from address)
- `--exclude "keyword"` (alias `--exclude-comps`) — Drop comps whose listing name contains any of the comma-separated keywords. Also takes a full comp name to remove one specific listing.

## Comp Set Review (do this on every report)

**Standing rule: comps must not out-size the subject.** AirROI returns bedrooms and guest capacity but *not* a bed (sleeping-surface) count, so an oversized unit can come through looking identical to the subject and inflate the projection.

Typical case: a comp reported as "2BR / sleeps 6" is actually a 4-bed unit sleeping more people, and it carries the highest ADR in the set. Left in, it pulls the Base Case up by a few percent.

Before sending a report, eyeball the comp set. Any comp whose ADR is a clear outlier above the rest is the one to check. Drop it by name:

```bash
python agent.py --input "..." --exclude-comps "Exact Listing Name To Drop"
```

Note: the value is split on commas, so a comp name containing a comma becomes two keywords. It still matches the intended listing, but it can over-match others. Use a distinctive comma-free fragment of the name when that happens.

**Not automated yet.** Options under consideration: (a) drop ADR/revenue outliers heuristically before the projection, or (b) add a data source that exposes bed counts. Undecided.

## Report Output

Reports are saved as self-contained HTML files in the `output/` folder. Open them in any web browser. They include:
- Three-tier revenue projection (Conservative / Base Case / Optimistic)
- 6 comparable properties with actual TTM performance data
- Interactive revenue calculator with sliders
- Seasonal occupancy chart
- Methodology section

## Troubleshooting

- **"AIRROI_API_KEY is not set"** — Run setup again, Step 3
- **Firecrawl returns wrong property data** — The address validation will catch this. Try passing the Zillow URL directly instead of the address.
- **"Only N comps"** — Thin market. The agent will widen the search automatically.
- **"No hero image found"** — Provide one with `--hero-url` or set up Google Maps API key.
- **AirROI divergence flag** — The model estimate differs significantly from comp data. The comp-based projection is more defensible.
