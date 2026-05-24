# Concordia Theatre Scraper

Scraper for [Concordia Theatre](https://concordiatheatre.co.uk), an amateur theatre
in Hinckley, Leicestershire, UK. Extracts all upcoming productions with per-performance
seat availability and pricing.

---

## Requirements

The scraper depends on the following Python packages:

| Package | Purpose |
|---|---|
| `requests` | HTTP client for fetching listing and seat map pages |
| `beautifulsoup4` | HTML parsing |
| `python-dateutil` | Flexible date string parsing |
| `lxml` | Optional faster HTML parser (fallback to `html.parser` if absent) |

These are a subset of the project-wide `requirements.txt` at the repository root.

### Installing

Create and activate a virtual environment, then install from the project requirements file:

```powershell
# From the repository root
python -m venv myenv
myenv\Scripts\activate          # Windows
# source myenv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

To install only the packages the scraper itself needs:

```powershell
pip install requests beautifulsoup4 python-dateutil lxml
```

### Python version

Python 3.10 or later is required (uses `match`-free type annotations and `X | Y` union
syntax via `from __future__ import annotations`).

---

## How It Works

The scraper operates in three phases, all using plain HTTP requests — no browser
automation (Selenium) is required.

### Phase 0 — Venue Info

Before scraping shows, the scraper fetches two pages from concordiatheatre.co.uk to
extract venue metadata dynamically (nothing is hardcoded):

- **Venue name** — from the `<title>` of the about page:
  `"About Us | Concordia Theatre"` → split on `|` → `"Concordia Theatre"`
- **Full address** — from the footer "Visit Us" blurb (`<div class="..blurb_description..">`)
  on the homepage: e.g. `"Stockwell Head, Hinckley, Leics, LE10 1RE"`
- **City** — parsed from the address: 2nd comma-segment that is more than 4 characters
  and not a UK postcode prefix → `"Hinckley"`
- **Country** — inferred from the presence of a UK postcode pattern
  (`\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b`) in the address → `"United Kingdom"`

### Phase 1 — Listing

Fetches the Savoy Systems programme page for Concordia Theatre:

```
https://concordiatheatre.savoysystems.co.uk/ConcordiaTheatre.dll/TSelectItems.waSelectItemsPrompt.TcsWebMenuItem_834.TcsWebTab_835
```

This single page lists every upcoming show. Each show appears as a
`<div class="programme">` block containing:

- `<h1 class="title">` — show title
- A table of `<tr>` rows, one per performance date/time:
  - `<td class="PeformanceListDate">` — human-readable date (e.g. `Friday 22 May 2026`)
  - `<a class="Button" href="...">19:30</a>` — time text and direct booking URL for that
    performance's seat map page

The listing page is authoritative for what is currently on sale. Past performances are
automatically dropped by Savoy Systems and will not appear here.

### Phase 2 — Seat Maps

For each performance found in Phase 1, the scraper fetches its direct booking URL (a
Savoy Systems allocated-seating page). From that page it extracts:

**Price groups** — from `<div class="PriceGroupSeatType_NNN">` divs:
- `<div class="SeatType">` — seat section name (e.g. `Standard`)
- `<td class="PriceGroupPersonTypePrice">` — price in GBP (e.g. `£15.00`)
- The numeric suffix `NNN` is the `seatTypeId` used to link seats to prices

**Individual seats** — from `<a aria-label="Seat X">` elements:
- Total count = venue capacity for that page
- Available seats are those with an `onClick="SelectSeat('seatId', 'seatTypeId')"` handler
- Unavailable/booked seats have no `onClick` and are excluded from pricing output

When a performance's booking page does not contain seat elements (booking not yet open,
or the show uses a different ticket flow), the scraper records no seat data for those
performances and carries the capacity discovered from other shows at the same venue.

---

## Output

Written to `concordia_output.csv` in the working directory.

### Columns

| Column | Description |
|---|---|
| `title` | Show title as listed on Savoy Systems |
| `venue_url` | Savoy Systems listing URL (same for all rows) |
| `category` | `Musical` if "musical" appears in the title; otherwise blank |
| `venue` | `Concordia Theatre` |
| `address` | `Stockwell Head` |
| `city` | `Hinckley` |
| `country` | `United Kingdom` |
| `open_date` | Date of the first listed performance (`YYYY-MM-DD`) |
| `close_date` | Date of the last listed performance (`YYYY-MM-DD`) |
| `booking_start_date` | Same as `open_date` |
| `booking_end_date` | Same as `close_date` |
| `upcoming_performances` | Python literal list of `{date, time}` dicts |
| `capacity` | Total seat count from the seat map page (backfilled venue-wide if unavailable for a show) |
| `currency` | `GBP` when seat pricing was found; blank otherwise |
| `is_limited_run` | `True` when a close date exists; `False` otherwise |
| `seat_pricing` | Python literal dict: `{"YYYY-MM-DD HH:MM": [{"seat": "V1", "ticket_price": 15.0}, ...]}` |
| `scrape_datetime` | Timestamp of the scrape run (`YYYY-MM-DD HH:MM`) |

### seat_pricing format

Each key is a `"YYYY-MM-DD HH:MM"` datetime string matching an entry in
`upcoming_performances`. The value is a list of available seat dicts:

```python
{
    '2026-05-23 19:30': [
        {'seat': 'V1', 'ticket_price': 15.0},
        {'seat': 'V2', 'ticket_price': 15.0},
        ...
    ],
}
```

Seat IDs (e.g. `V1`, `T12`, `A20`) correspond to physical seat positions on the venue
plan. An empty list `[]` means the performance exists but no seats are currently
bookable (sold out or booking not open). An absent key means the performance was not
found on Savoy at the time of the scrape.

Shows whose booking pages return no seat layout at all (booking not yet set up) will
have `seat_pricing = {}`.

---

## Running the Scraper

```powershell
cd scraper
python concordia_scraper.py
```

The validator runs automatically after the CSV is written and prints a
`PASSED` / `FAILED` summary to the console. To run the validator separately:

```powershell
python src/utils/csv_validator.py concordia_output.csv
```

---

## Utilities Used

### `requests` + `BeautifulSoup`

All HTTP fetching uses the `requests` library with a persistent `Session` carrying a
Chrome-like `User-Agent` header. HTML is parsed with `BeautifulSoup` using the
built-in `html.parser`. No JavaScript rendering or browser automation is needed —
all required data is present in the static HTML returned by Savoy Systems.

### `python-dateutil`

`dateutil.parser.parse` handles the human-readable date strings returned by Savoy
Systems (e.g. `"Friday 22 May 2026"`) with `dayfirst=True` for UK date ordering.
Ordinal suffixes (`1st`, `2nd`, `3rd`, `4th`) are stripped by regex before parsing.

### `src/utils/csv_validator.py`

The internal CSV validator is imported at runtime and called automatically after the
scrape completes. It checks every rule defined in `csv-validator.md`, including:

- Schema: correct column names and order
- Field formats: dates as `YYYY-MM-DD`, `scrape_datetime` as `YYYY-MM-DD HH:MM`
- `is_limited_run`: must be `True` / `False` / `1` / `0`
- `category`: must be `Musical`, `Play`, or blank
- `seat_pricing`: must be a Python-literal dict with `YYYY-MM-DD HH:MM` keys and
  `[{seat, ticket_price}]` values
- Seat IDs: must be unique within each performance; not all identical (placeholder check)
- Capacity: must be present when real seat data exists
- Cross-row consistency: same venue should have consistent capacity across all shows

The validator path is resolved relative to the scraper file itself so it works
regardless of which directory the script is run from.

### Rate limiting

A 1.5-second delay (`RATE_LIMIT = 1.5`) is enforced before every HTTP request via
`time.sleep` inside `fetch_soup`. This applies to both the listing page fetch and each
individual seat map fetch.

---

## Key Implementation Details

**Why Savoy Systems listing page, not concordiatheatre.co.uk?**
The Savoy Systems page is the booking system's own view of available shows and
performances. It automatically removes past performances and shows exactly what is
currently on sale, giving accurate `upcoming_performances` data without manual date
filtering.

**Why no Selenium?**
All content including seat availability is rendered server-side by the Savoy Systems
Delphi web application. The seat map HTML — including `onClick` handlers on individual
seat `<a>` elements — is present in the initial HTTP response. No JavaScript execution
is required.

**Capacity backfill**
Shows whose booking pages have not yet published a seat layout (e.g. future shows with
booking not yet open) return no seat elements and therefore no capacity. After all shows
are processed, the highest capacity discovered across any show in the same run is
propagated to those empty rows. Since Concordia Theatre has a fixed auditorium of 398
seats, this ensures the field is always populated once at least one show's seat map
has loaded.

**`is_limited_run` derivation**
Set to `True` when a `close_date` is present (derived from the last performance date),
and `False` otherwise. All Concordia productions have defined run dates so this will
always resolve to `True` in practice, but it is data-driven rather than hardcoded.
