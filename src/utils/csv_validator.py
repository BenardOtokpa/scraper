"""
CSV validator for scraped theatre data.

Usage:
    python src/utils/csv_validator.py output.csv
    python -m utils.csv_validator output.csv

    from utils.csv_validator import validate_csv, validate_dataframe

Exit codes: 0 = all checks pass (warnings allowed), 1 = any failure.
"""
from __future__ import annotations

import ast
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

COLUMNS: list[str] = [
    "title", "venue_url", "category", "venue", "address", "city", "country",
    "open_date", "close_date", "booking_start_date", "booking_end_date",
    "upcoming_performances", "capacity", "currency", "is_limited_run",
    "seat_pricing", "scrape_datetime",
]

REQUIRED_NONEMPTY: frozenset[str] = frozenset({
    "title", "venue_url", "venue", "city", "country",
    "address", "scrape_datetime", "upcoming_performances", "seat_pricing",
})

VALID_CATEGORIES: frozenset[str] = frozenset({"musical", "play"})
DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M"

GENERIC_SEAT_MIN_ENTRIES = 3
DUPLICATE_SEAT_MAP_MIN_PERFS = 2
DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS = 5

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, rule: str | int, msg: str) -> None:
        self.failures.append(f"FAIL [rule {rule}] {msg}")

    def warn(self, rule: str | int, msg: str) -> None:
        self.warnings.append(f"WARN [rule {rule}] {msg}")

    def ok(self, _rule: str | int) -> None:
        pass

    def print_report(self) -> None:
        for line in self.warnings:
            print(line)
        for line in self.failures:
            print(line)
        status = "PASSED" if not self.failures else "FAILED"
        print(f"\n{status} — {len(self.failures)} failure(s), {len(self.warnings)} warning(s)")

    @property
    def passed(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(value, fmt)
    except (ValueError, TypeError):
        return None


def _is_python_literal(raw: str) -> bool:
    """Return True if raw uses Python-literal quoting, not JSON double-quotes."""
    stripped = raw.strip()
    # [] and {} are identical in both formats — accept either
    if stripped in ("[]", "{}"):
        return True
    # JSON string delimiters are double-quotes; Python repr uses single quotes.
    # If the string has no double-quotes at all it cannot be JSON.
    if '"' not in stripped:
        return True
    # Has double-quotes — if json.loads succeeds it is JSON format → fail
    try:
        json.loads(stripped)
        return False
    except (json.JSONDecodeError, ValueError):
        return True  # double-quotes but not valid JSON → still Python literal


def _try_literal(raw: str) -> tuple[bool, Any]:
    try:
        return True, ast.literal_eval(raw.strip())
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# Row context
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    row: dict[str, str]
    idx: int          # 1-based row number
    report: Report
    parsed_perfs: list[dict] | None = None
    parsed_seat_pricing: dict | None = None
    perf_dates: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Schema checks (rules 1–3)  — run once before per-row checks
# ---------------------------------------------------------------------------

def check_schema(cols: list[str], report: Report) -> bool:
    """Returns False if schema is too broken to continue."""
    col_set = set(cols)
    required_set = set(COLUMNS)

    missing = required_set - col_set
    if missing:
        report.fail(1, f"Missing required columns: {sorted(missing)}")
        return False
    report.ok(1)

    extra = col_set - required_set
    if extra:
        report.warn(2, f"Unexpected extra columns: {sorted(extra)}")
    else:
        report.ok(2)

    # Rule 3: required columns must appear in canonical order relative to each other
    actual_order = [c for c in cols if c in required_set]
    if actual_order != COLUMNS:
        report.fail(3, f"Required columns not in canonical order.\n  expected: {COLUMNS}\n  got:      {actual_order}")
        return False
    report.ok(3)

    return True


# ---------------------------------------------------------------------------
# Cross-record checks  — run after all rows are collected
# ---------------------------------------------------------------------------

def check_uniqueness(rows: list[dict], idxs: list[int], report: Report) -> None:
    seen: dict[tuple, int] = {}
    for row, idx in zip(rows, idxs):
        key = (row.get("title", "").strip().lower(), row.get("venue", "").strip().lower())
        if key in seen:
            report.fail(5, f"Row {idx}: duplicate (title, venue) '{key}' — duplicate of row {seen[key]}")
        else:
            seen[key] = idx


def check_cross_venue_url(rows: list[dict], idxs: list[int], report: Report) -> None:
    """Rule 30: rows sharing a venue_url must have consistent address/city/country/currency/capacity."""
    groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for row, idx in zip(rows, idxs):
        url = row.get("venue_url", "").strip()
        if url:
            groups[url].append((idx, row))

    for url, entries in groups.items():
        if len(entries) < 2:
            continue
        for f in ("address", "city", "country", "currency", "capacity"):
            vals = {e[1].get(f, "").strip() for e in entries}
            populated = {v for v in vals if v}
            if len(populated) > 1:
                report.warn(30, f"venue_url '{url}': inconsistent '{f}' values: {populated}")


def check_cross_venue_capacity(rows: list[dict], idxs: list[int], report: Report) -> None:
    """Rule 31: within same venue name, if any row has capacity, every row must."""
    groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for row, idx in zip(rows, idxs):
        venue = row.get("venue", "").strip()
        if venue:
            groups[venue].append((idx, row))

    for venue, entries in groups.items():
        if len(entries) < 2:
            continue
        caps = [e[1].get("capacity", "").strip() for e in entries]
        if any(caps) and not all(caps):
            missing = [e[0] for e, cap in zip(entries, caps) if not cap]
            report.warn(31, f"venue '{venue}': capacity missing in row(s) {missing} but present in others")


# ---------------------------------------------------------------------------
# Per-row checks
# ---------------------------------------------------------------------------

def _check_required_nonempty(ctx: _Ctx) -> None:
    missing = [c for c in REQUIRED_NONEMPTY if not ctx.row.get(c, "").strip()]
    if missing:
        ctx.report.fail(4, f"Row {ctx.idx}: required fields empty: {sorted(missing)}")


def _check_category(ctx: _Ctx) -> None:
    val = ctx.row.get("category", "").strip()
    if val and val.lower() not in VALID_CATEGORIES:
        ctx.report.fail(6, f"Row {ctx.idx}: category '{val}' must be Musical, Play, or blank")


def _check_venue_url(ctx: _Ctx) -> None:
    url = ctx.row.get("venue_url", "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        ctx.report.fail(24, f"Row {ctx.idx}: venue_url '{url}' not a valid http(s):// URL")


def _check_is_limited_run(ctx: _Ctx) -> None:
    val = ctx.row.get("is_limited_run", "").strip()
    if val.lower() not in ("true", "false", "1", "0"):
        ctx.report.fail(25, f"Row {ctx.idx}: is_limited_run '{val}' not boolean-coercible")


def _check_capacity(ctx: _Ctx) -> None:
    val = ctx.row.get("capacity", "").strip()
    if not val:
        return
    try:
        n = int(val)
        if n < 0:
            raise ValueError
    except ValueError:
        ctx.report.fail(26, f"Row {ctx.idx}: capacity '{val}' not a non-negative integer")


def _check_currency(ctx: _Ctx) -> None:
    val = ctx.row.get("currency", "").strip()
    if val and not re.fullmatch(r"[A-Z]{3}", val):
        ctx.report.fail(23, f"Row {ctx.idx}: currency '{val}' not a 3-letter uppercase ISO 4217 code")


# ---------------------------------------------------------------------------
# Parse upcoming_performances (rules 17, 18, 19)
# ---------------------------------------------------------------------------

def _check_performances(ctx: _Ctx) -> None:
    raw = ctx.row.get("upcoming_performances", "")

    if not _is_python_literal(raw):
        ctx.report.fail(17, f"Row {ctx.idx}: upcoming_performances uses JSON double-quote format instead of Python literal")
        return

    ok, val = _try_literal(raw)
    if not ok:
        ctx.report.fail(18, f"Row {ctx.idx}: upcoming_performances cannot be parsed with ast.literal_eval")
        return
    if not isinstance(val, list):
        ctx.report.fail(18, f"Row {ctx.idx}: upcoming_performances is not a list (got {type(val).__name__})")
        return

    valid: list[dict] = []
    for i, entry in enumerate(val):
        loc = f"Row {ctx.idx} upcoming_performances[{i}]"
        if not isinstance(entry, dict):
            ctx.report.fail(19, f"{loc}: not a dict")
            continue
        if set(entry.keys()) != {"date", "time"}:
            ctx.report.fail(19, f"{loc}: expected keys {{date, time}}, got {set(entry.keys())}")
            continue
        if not _parse_dt(entry.get("date", ""), DATE_FMT):
            ctx.report.fail(19, f"{loc}: date '{entry.get('date')}' not YYYY-MM-DD")
            continue
        if not re.fullmatch(r"\d{2}:\d{2}", entry.get("time", "")):
            ctx.report.fail(19, f"{loc}: time '{entry.get('time')}' not HH:MM")
            continue
        valid.append(entry)

    ctx.parsed_perfs = valid
    ctx.perf_dates = {e["date"] for e in valid}


# ---------------------------------------------------------------------------
# Parse seat_pricing (rules 17, 20, 21, 22)
# ---------------------------------------------------------------------------

def _check_seat_pricing(ctx: _Ctx) -> None:
    raw = ctx.row.get("seat_pricing", "")

    if not _is_python_literal(raw):
        ctx.report.fail(17, f"Row {ctx.idx}: seat_pricing uses JSON double-quote format instead of Python literal")
        return

    ok, val = _try_literal(raw)
    if not ok:
        ctx.report.fail(20, f"Row {ctx.idx}: seat_pricing cannot be parsed with ast.literal_eval")
        return
    if not isinstance(val, dict):
        ctx.report.fail(20, f"Row {ctx.idx}: seat_pricing is not a dict (got {type(val).__name__})")
        return

    any_fail = False
    for dt_key, seat_list in val.items():
        loc = f"Row {ctx.idx} seat_pricing['{dt_key}']"

        if not _parse_dt(dt_key, DATETIME_FMT):
            ctx.report.fail(21, f"{loc}: key not in YYYY-MM-DD HH:MM format")
            any_fail = True
            continue
        if not isinstance(seat_list, list):
            ctx.report.fail(21, f"{loc}: value is not a list")
            any_fail = True
            continue

        for j, seat_entry in enumerate(seat_list):
            sloc = f"{loc}[{j}]"
            if not isinstance(seat_entry, dict):
                ctx.report.fail(21, f"{sloc}: not a dict")
                any_fail = True
                continue
            if set(seat_entry.keys()) != {"seat", "ticket_price"}:
                ctx.report.fail(21, f"{sloc}: expected keys {{seat, ticket_price}}, got {set(seat_entry.keys())}")
                any_fail = True
                continue
            tp = seat_entry.get("ticket_price")
            if not isinstance(tp, (int, float)) or tp < 0:
                ctx.report.fail(22, f"{sloc}: ticket_price '{tp}' not a non-negative number")
                any_fail = True

    if not any_fail:
        ctx.parsed_seat_pricing = val


# ---------------------------------------------------------------------------
# Seat ID checks (rules 7, 8, 8a, 8b)
# ---------------------------------------------------------------------------

def _check_seat_ids(ctx: _Ctx) -> None:
    sp = ctx.parsed_seat_pricing
    if not sp:
        return

    all_maps: list[frozenset] = []
    for dt_key, seat_list in sp.items():
        if not seat_list:
            continue
        seat_ids = [s.get("seat") for s in seat_list if isinstance(s, dict)]

        if len(seat_ids) >= GENERIC_SEAT_MIN_ENTRIES and len(set(seat_ids)) == 1:
            ctx.report.fail(7, f"Row {ctx.idx} seat_pricing['{dt_key}']: all seat IDs are '{seat_ids[0]}' — placeholder detected")

        if len(seat_ids) != len(set(seat_ids)):
            ctx.report.fail(8, f"Row {ctx.idx} seat_pricing['{dt_key}']: duplicate seat IDs found")

        all_maps.append(frozenset(
            (s.get("seat"), s.get("ticket_price")) for s in seat_list if isinstance(s, dict)
        ))

    n = len(all_maps)
    if n >= DUPLICATE_SEAT_MAP_MIN_PERFS and len(set(all_maps)) == 1:
        if n >= DUPLICATE_SEAT_MAP_FAIL_MIN_PERFS:
            ctx.report.fail("8b", f"Row {ctx.idx}: {n} performances share identical seat map — scraper is reusing one performance's data")
        else:
            ctx.report.warn("8a", f"Row {ctx.idx}: {n} performances share identical seat map — verify manually")


# ---------------------------------------------------------------------------
# Date checks (rules 9–15, 15a)
# ---------------------------------------------------------------------------

def _check_dates(ctx: _Ctx) -> None:
    row, idx, report = ctx.row, ctx.idx, ctx.report

    date_fields = ("open_date", "close_date", "booking_start_date", "booking_end_date")
    parsed: dict[str, datetime | None] = {}

    for f in date_fields:
        raw = row.get(f, "").strip()
        if not raw:
            parsed[f] = None
            continue
        dt = _parse_dt(raw, DATE_FMT)
        if dt is None:
            report.fail(9, f"Row {idx}: {f} '{raw}' not parseable")
            report.fail(10, f"Row {idx}: {f} '{raw}' not in YYYY-MM-DD format")
            parsed[f] = None
        else:
            if raw != dt.strftime(DATE_FMT):
                report.fail(10, f"Row {idx}: {f} '{raw}' not in YYYY-MM-DD format")
            parsed[f] = dt

    sd_raw = row.get("scrape_datetime", "").strip()
    if sd_raw and not _parse_dt(sd_raw, DATETIME_FMT):
        report.fail(9, f"Row {idx}: scrape_datetime '{sd_raw}' not parseable as YYYY-MM-DD HH:MM")
        report.fail(10, f"Row {idx}: scrape_datetime '{sd_raw}' not in YYYY-MM-DD HH:MM format")

    od = parsed.get("open_date")
    cd = parsed.get("close_date")
    bsd = parsed.get("booking_start_date")
    bed = parsed.get("booking_end_date")

    if od and cd:
        if od > cd:
            report.fail(11, f"Row {idx}: open_date {od.date()} > close_date {cd.date()}")

    if bsd and bed:
        if bsd > bed:
            report.fail(12, f"Row {idx}: booking_start_date {bsd.date()} > booking_end_date {bed.date()}")

    perfs = ctx.parsed_perfs or []

    if perfs and od:
        earliest = min(perfs, key=lambda p: p["date"])["date"]
        earliest_dt = _parse_dt(earliest, DATE_FMT)
        if earliest_dt and od > earliest_dt:
            report.fail(13, f"Row {idx}: open_date {od.date()} is after earliest performance {earliest}")

    if perfs and cd:
        latest = max(perfs, key=lambda p: p["date"])["date"]
        latest_dt = _parse_dt(latest, DATE_FMT)
        if latest_dt and cd < latest_dt:
            report.fail(14, f"Row {idx}: close_date {cd.date()} is before latest performance {latest}")

    if perfs and od and cd:
        distinct_dates = {p["date"] for p in perfs}
        if len(distinct_dates) > 1 and od == cd:
            report.fail(15, f"Row {idx}: open_date == close_date for a multi-date show")
        elif len(perfs) == 1:
            perf_dt = _parse_dt(perfs[0]["date"], DATE_FMT)
            if od != cd:
                report.warn("15a", f"Row {idx}: single-performance show but open_date != close_date")
            elif perf_dt and od != perf_dt:
                report.warn("15a", f"Row {idx}: single-performance show open/close date doesn't match performance date {perfs[0]['date']}")


# ---------------------------------------------------------------------------
# Capacity vs seat_pricing (rules 28a–28c)
# ---------------------------------------------------------------------------

def _check_capacity_vs_seats(ctx: _Ctx) -> None:
    cap = ctx.row.get("capacity", "").strip()
    sp = ctx.parsed_seat_pricing
    if sp is None:
        return

    if not sp:
        # sp == {} → rule 28c: no constraint
        return

    has_real_seats = any(
        isinstance(lst, list) and lst for lst in sp.values()
    )
    all_sold_out = all(lst == [] for lst in sp.values())

    if has_real_seats and not cap:
        ctx.report.fail("28a", f"Row {ctx.idx}: capacity missing but seat_pricing contains real seat data")
    elif all_sold_out and not cap:
        ctx.report.warn("28b", f"Row {ctx.idx}: capacity missing for sold-out-only seat_pricing")


# ---------------------------------------------------------------------------
# Performance ↔ seat_pricing match (rule 29)
# ---------------------------------------------------------------------------

def _check_perf_seat_match(ctx: _Ctx) -> None:
    perfs = ctx.parsed_perfs or []
    sp = ctx.parsed_seat_pricing

    # {} means "not on sale" — exempt from rule 29
    if sp is None or not sp:
        return

    for entry in perfs:
        dt_key = f"{entry['date']} {entry['time']}"
        if dt_key not in sp:
            ctx.report.warn(29, f"Row {ctx.idx}: performance '{dt_key}' has no matching seat_pricing key")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_dataframe(rows: list[dict], print_report: bool = True) -> Report:
    report = Report()

    if not rows:
        report.fail(0, "CSV contains no data rows")
        if print_report:
            report.print_report()
        return report

    cols = list(rows[0].keys())
    if not check_schema(cols, report):
        if print_report:
            report.print_report()
        return report

    idxs = list(range(1, len(rows) + 1))

    check_uniqueness(rows, idxs, report)

    ctxs: list[_Ctx] = []
    for idx, row in zip(idxs, rows):
        ctx = _Ctx(row=row, idx=idx, report=report)
        _check_required_nonempty(ctx)
        _check_category(ctx)
        _check_venue_url(ctx)
        _check_is_limited_run(ctx)
        _check_capacity(ctx)
        _check_currency(ctx)
        _check_performances(ctx)
        _check_seat_pricing(ctx)
        _check_seat_ids(ctx)
        _check_dates(ctx)
        _check_capacity_vs_seats(ctx)
        _check_perf_seat_match(ctx)
        ctxs.append(ctx)

    check_cross_venue_url(rows, idxs, report)
    check_cross_venue_capacity(rows, idxs, report)

    if print_report:
        report.print_report()
    return report


def validate_csv(path: str, print_report: bool = True) -> Report:
    p = Path(path)
    if not p.exists():
        r = Report()
        r.fail(0, f"File not found: {path}")
        if print_report:
            r.print_report()
        return r

    with p.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    return validate_dataframe(rows, print_report=print_report)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/utils/csv_validator.py output.csv")
        sys.exit(1)
    report = validate_csv(sys.argv[1])
    sys.exit(0 if report.passed else 1)
