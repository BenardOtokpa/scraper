# CSV Validator

Validates scraped theatre CSVs against the canonical schema and a set of data-quality rules before they are uploaded or used downstream.

Source: [src/utils/csv_validator.py](../src/utils/csv_validator.py)

## Usage

```bash
# CLI
python src/utils/csv_validator.py path/to/output.csv
python -m utils.csv_validator path/to/output.csv

# Programmatic
from utils.csv_validator import validate_csv, validate_dataframe
validate_csv("output.csv")              # loads from disk
validate_dataframe(df, print_report=True)  # in-memory DataFrame
```

Exit codes: `0` if all checks pass (warnings allowed), `1` on any failure.

## Schema

Every output CSV must contain exactly these columns, in this order:

```
title, venue_url, category, venue, address, city, country,
open_date, close_date, booking_start_date, booking_end_date,
upcoming_performances, capacity, currency, is_limited_run,
seat_pricing, scrape_datetime
```

Required non-empty fields: `title`, `venue_url`, `venue`, `city`, `country`, `address`, `scrape_datetime`, `upcoming_performances`, `seat_pricing`.

`upcoming_performances` and `seat_pricing` must always be emitted by the scraper, even when there is nothing to report: `upcoming_performances` is either `[]` or a list of performances, and `seat_pricing` is either `{}` (no seat plan / not on sale) or a dict of seat data. A truly blank cell indicates a scraper bug and fails rule 4.

## Rules

Levels: **FAIL** blocks the run; **WARN** is informational.

### Schema
| # | Rule | Level |
|---|------|-------|
| 1 | All required columns present | FAIL |
| 2 | No unexpected extra columns | WARN |
| 3 | Required columns appear in canonical order | FAIL |
| 4 | Required non-empty fields populated in every row | FAIL |

### Content
| # | Rule | Level |
|---|------|-------|
| 5 | `(title, venue)` pairs are unique (one row per show+venue) | FAIL |
| 6 | `category` is `Musical` or `Play` (case-insensitive); blank allowed | FAIL |
| 7 | Seat IDs not all identical within a performance (≥3 entries) — flags placeholder IDs | FAIL |
| 8 | Seat IDs unique within each performance | FAIL |
| 8a | Seat map differs across performances — flags rows where 2–4 performances share an identical `(seat, ticket_price)` map (possible scraper reuse, verify manually) | WARN |
| 8b | Seat map differs across performances — flags rows where ≥5 performances share an identical `(seat, ticket_price)` map (scraper is reusing one performance's seat data) | FAIL |

### Dates
| # | Rule | Level |
|---|------|-------|
| 9 | Date fields parseable | FAIL |
| 10 | Date fields use expected format (`YYYY-MM-DD`; `scrape_datetime` is `YYYY-MM-DD HH:MM`) | FAIL |
| 11 | `open_date ≤ close_date` | FAIL |
| 12 | `booking_start_date ≤ booking_end_date` | FAIL |
| 13 | `open_date` not after earliest `upcoming_performances` date | FAIL |
| 14 | `close_date` not before latest `upcoming_performances` date | FAIL |
| 15 | `open_date ≠ close_date` for multi-date shows | FAIL |
| 15a | `open_date == close_date == performance date` for single-performance shows (no date range allowed) | WARN |

### Format
| # | Rule | Level |
|---|------|-------|
| 17 | `upcoming_performances` and `seat_pricing` use single-quoted Python literals (not JSON) | FAIL |
| 18 | `upcoming_performances` is a list | FAIL |
| 19 | Each `upcoming_performances` entry is `{date: YYYY-MM-DD, time: HH:MM}` | FAIL |
| 20 | `seat_pricing` is a dict | FAIL |
| 21 | `seat_pricing` shape: `{"YYYY-MM-DD HH:MM": [{seat, ticket_price}, ...]}` — seat entries must contain *only* `seat` and `ticket_price`; any extra keys fail | FAIL |
| 22 | `ticket_price` is a non-negative number | FAIL |
| 23 | `currency` is a 3-letter uppercase ISO 4217 code | FAIL |
| 24 | `venue_url` is a valid `http(s)://…` URL | FAIL |

### Field-level
| # | Rule | Level |
|---|------|-------|
| 25 | `is_limited_run` is boolean-coercible (`True`/`False`/`1`/`0`) | FAIL |
| 26 | `capacity` is a non-negative integer | FAIL |
| 27 | `currency`: no presence constraint vs. `seat_pricing` (format still enforced by rule 23); allowed regardless of whether `seat_pricing` is empty or populated | — |
| 28a | `capacity` must be populated when `seat_pricing` contains real seat data (any datetime key has a non-empty seat list) | FAIL |
| 28b | `capacity` missing for sold-out-only `seat_pricing` (all datetime keys map to `[]`, e.g. `{'YYYY-MM-DD HH:MM': []}`) | WARN |
| 28c | `capacity` may be populated or blank when `seat_pricing={}` (unreserved-venue exception) | — |

### Cross-record
| # | Rule | Level |
|---|------|-------|
| 29 | Every `upcoming_performances` entry has a matching `seat_pricing` key (sold-out / not-on-sale exempt) | FAIL |
| 30 | Rows sharing a `venue_url` have consistent `address`, `city`, `country`, `currency`, `capacity` (all populated or all blank) | WARN |
| 31 | Venue capacity completeness — within rows sharing the same `venue` name, if any row has `capacity`, every row must | WARN |

## Notes on specific rules

**Currency presence (rule 27)** — No presence constraint relative to `seat_pricing`. Both blank and populated `currency` are allowed whether `seat_pricing` has data or is `{}`. The exception exists because unreserved venues return `seat_pricing={}` while still having a real currency. Currency *format* (3-letter uppercase ISO 4217) is still enforced by rule 23 whenever the field is populated, and rule 30 still flags inconsistency across rows sharing a `venue_url`.

**Capacity presence (rules 28a–28c)** — `capacity` must be populated when `seat_pricing` contains real seat data (FAIL if missing). For sold-out-only `seat_pricing` (datetime keys with empty seat lists, e.g. `{'YYYY-MM-DD HH:MM': []}`), missing capacity is a WARN rather than a FAIL — consistent with rules 20–21 treating that shape as an accepted empty form. The inverse (capacity populated while `seat_pricing={}`) is allowed, since unreserved venues return `seat_pricing={}` but still have a venue capacity.

**Quote style (rule 17)** — `upcoming_performances` and `seat_pricing` are serialised as Python literals (`{'key': 'value'}`), not JSON (`{"key": "value"}`), because they are reparsed with `ast.literal_eval`.

**Seat ID placeholders (rule 7)** — FAIL. Unreserved venues must return `seat_pricing={}` rather than emitting repeated placeholder seat entries, so any performance whose seats all share the same ID (≥3 entries, `GENERIC_SEAT_MIN_ENTRIES`) is treated as a scraper bug.

**Duplicated seat map across performances (rules 8a / 8b)** — Triggers when every performance in a row shares the *same* sorted set of `(seat, ticket_price)` pairs. Severity scales with the performance count: 2–4 identical performances are a WARN (small venues with untouched seats can legitimately match — `DUPLICATE_SEAT_MAP_MIN_PERFS = 2`); ≥5 identical performances are a FAIL (`DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS = 5`), since at that scale the scraper is almost certainly reusing one performance's data across the rest instead of fetching each separately.

**Performance ↔ seat_pricing match (rule 29)** — A WARN because sold-out and not-yet-on-sale performances may legitimately appear in `upcoming_performances` without seat-level pricing.

**Empty `seat_pricing` shapes (rules 20–21)** — Two empty shapes are accepted and not flagged:
- `{}` — a show with no performances yet on sale.
- `{'YYYY-MM-DD HH:MM': []}` — a sold-out performance (datetime key with an empty seat list).

## Adding a new rule

1. Add a `check_*` function in [src/utils/csv_validator.py](../src/utils/csv_validator.py) that takes `_Ctx` and calls `ctx.report.ok/warn/fail`.
2. Register it in the `_CHECKS` tuple, grouped with similar checks.
3. Update this doc.

If the rule reads `seat_pricing` or `upcoming_performances`, reuse `ctx.parsed_seat_pricing` / `ctx.parsed_perfs` / `ctx.perf_dates` instead of re-parsing.
