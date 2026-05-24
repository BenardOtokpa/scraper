# Scraper Developer Assessment

Welcome — this exercise is how we evaluate candidates joining our web-scraping team. It mirrors the actual work: extracting structured theatre/venue data from a live website and producing a CSV that conforms to our canonical schema.

## What you'll build

A scraper that crawls a theatre or event-listing website and outputs a single `output.csv` file conforming to the schema in [csv-validator.md](csv-validator.md).

Your submission is considered passing if:

```bash
python src/utils/csv_validator.py output.csv
```

exits with code `0` (warnings are acceptable; failures are not).

## Target site

Pick **one** of the following, or propose your own (email us first):

- A small-to-medium theatre with a public events page and detail pages per show
- Must have at least 5 upcoming shows
- Must expose performance dates/times (seat-level pricing is a bonus, not a requirement)

If you have nothing in mind, use: `https://www.auditoriumdellaconciliazione.it/en/events/`.

## Requirements

1. **Schema** — output the exact 18 columns in the order defined in [csv-validator.md](csv-validator.md).
2. **Pagination + detail pages** — follow the listing into each show's detail page.
3. **Resilience** — handle missing fields gracefully; retry transient failures; respect a configurable rate limit.
4. **Idempotency** — rerunning should not produce duplicates (rule 5: unique `(title, venue)`).
5. **Empty-but-present fields** — `upcoming_performances` must be `[]` (not blank) when there are no performances; `seat_pricing` must be `{}` (not blank) when there is no seat data. See rule 4 and the notes on rules 20–21.
6. **Python-literal serialisation** — `upcoming_performances` and `seat_pricing` are written as Python literals (`{'key': 'value'}`), not JSON. See rule 17.
7. **README.md** — explain: libraries chosen and why, how to run, what breaks if the site changes, what you would do with more time.

## What to submit

A zip or git repo containing:

- The scraper script(s)
- `requirements.txt` or `pyproject.toml`
- `output.csv` (your actual run output)
- `README.md`

## How we grade

| Tier       | Criteria                                                                                                                                                                                      |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Reject** | Validator fails on rules 1–4 (schema / required fields not populated)                                                                                                                         |
| **Weak**   | Validator passes schema checks but fails on dates (9–15), formats (17–22), or emits placeholder seat IDs (rule 7)                                                                             |
| **Solid**  | Validator exits `0` with only WARNs; sensible code structure; handles pagination correctly                                                                                                    |
| **Strong** | Validator exits `0` with zero warnings; found a hidden JSON API instead of parsing HTML; clean separation of fetch / parse / output; thoughtful README discussing failure modes and detection |

We look favourably on:

- Inspecting the network tab and finding a JSON endpoint rather than parsing HTML
- Asking clarifying questions before starting
- A small parser test or two
- Honest "I'd do X with more time" notes in the README

Common things that lose points:

- Headless browser (Selenium / Playwright) used on a static HTML page
- `except Exception: pass` (swallowed errors)
- Hardcoded selectors with no thought for site changes
- `json.dumps` used for `upcoming_performances` / `seat_pricing` (fails rule 17)
- Placeholder seat IDs like `seat1, seat1, seat1` (fails rule 7) — emit `seat_pricing={}` instead for unreserved venues
- Dates not normalised to `YYYY-MM-DD` (fails rule 10)
- `currency` left as `$` / `usd` / `Euro` instead of ISO 4217 uppercase (fails rule 23)

## Timeline

- **Effort**: 4–6 hours
- **Deadline**: 1 day from receipt

## Follow-up

Strong submissions are invited to a 45-minute call covering:

1. Code walkthrough — defend your choices
2. Debugging exercise — diagnose a broken scraper we provide
3. System design — "How would you run 1000 scrapers on different cadences and detect when one silently breaks?"

## Reference material

- [csv-validator.md](csv-validator.md) — the canonical schema and all 30+ validation rules
- [src/utils/csv_validator.py](../src/utils/csv_validator.py) — run this locally to self-check before submitting

Good luck.
