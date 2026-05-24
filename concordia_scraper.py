
from __future__ import annotations # for Python 3.7-3.9 compatibility with list[dict] etc.

import csv
import datetime
import logging
import os as _os
import re
import sys as _sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# Make src.utils importable regardless of working directory
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from src.utils.csv_validator import validate_csv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://concordiatheatre.savoysystems.co.uk"
LISTING_URL = (
    "https://concordiatheatre.savoysystems.co.uk/ConcordiaTheatre.dll/"
    "TSelectItems.waSelectItemsPrompt.TcsWebMenuItem_834.TcsWebTab_835"
)
RATE_LIMIT = 1.5
OUTPUT_CSV = "output.csv"

CSV_COLUMNS = [
    "title", "venue_url", "category", "venue", "address", "city",
    "country", "open_date", "close_date", "booking_start_date",
    "booking_end_date", "upcoming_performances", "capacity", "currency",
    "is_limited_run", "seat_pricing", "scrape_datetime",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_SELECT_SEAT_RE = re.compile(r"SelectSeat\('([^']+)',\s*'([^']+)'\)")
_PRICE_GROUP_CLASS_RE = re.compile(r"PriceGroupSeatType_(\d+)$")

# ---------------------------------------------------------------------------
# Venue info — populated at runtime from concordiatheatre.co.uk
# ---------------------------------------------------------------------------
_venue: dict = {
    "name":    "",
    "address": "",
    "city":    "",
    "country": "",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return sess


def fetch_soup(url: str, sess: requests.Session) -> BeautifulSoup:
    time.sleep(RATE_LIMIT)
    r = sess.get(url, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean(s: str) -> str:
    return " ".join(s.split())


def parse_date_str(s: str) -> Optional[str]:
    """Parse a date string like 'Friday 22 May 2026' to 'YYYY-MM-DD'. the validator requires this format."""
    s = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", s)
    try:
        return dateparser.parse(s, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        return None


def parse_price(s: str) -> Optional[float]:
    m = re.search(r"£(\d+(?:\.\d{1,2})?)", s)
    return float(m.group(1)) if m else None


def standardize_category(title: str) -> str:
    tl = title.lower()
    if "musical" in tl:
        return "Musical"
    return ""


# ---------------------------------------------------------------------------
# Venue info extraction
# ---------------------------------------------------------------------------

def _load_venue_info(sess: requests.Session) -> None:
    """Populate _venue from concordiatheatre.co.uk — called once before scraping."""
    hp_soup  = fetch_soup("https://concordiatheatre.co.uk/", sess)
    abt_soup = fetch_soup("https://concordiatheatre.co.uk/about/", sess)

    # Venue name: "About Us | Concordia Theatre" → "Concordia Theatre"
    title_tag = abt_soup.find("title") or hp_soup.find("title")
    name = ""
    if title_tag:
        parts = title_tag.get_text().split("|")
        name = clean(parts[-1]) if len(parts) >= 2 else clean(parts[0])

    # Address: footer "Visit Us" blurb
    address = ""
    footer = hp_soup.find("footer")
    if footer:
        for h4 in footer.find_all(["h4", "h3"]):
            if "visit" in h4.get_text().lower():
                desc = h4.find_next("div", class_=re.compile(r"blurb_description"))
                if desc:
                    address = clean(desc.get_text())
                    break
    if not address:
        text = clean(hp_soup.get_text(" "))
        m = re.search(
            r"([\w\s]+,\s*[\w\s]+,\s*[\w]+,\s*[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})", text
        )
        if m:
            address = m.group(1).strip()

    # City: 2nd comma-segment that is >4 chars and not a postcode prefix
    city = ""
    for part in [p.strip() for p in address.split(",")][1:]:
        if re.match(r"^[A-Z]{1,2}\d", part):
            break
        if len(part) > 4:
            city = part
            break

    # Country: UK postcode in address → United Kingdom
    country = ""
    if re.search(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", address):
        country = "United Kingdom"

    _venue.update({"name": name, "address": address, "city": city, "country": country})
    log.info("Venue: %s | %s | %s | %s", name, address, city, country)


# ---------------------------------------------------------------------------
# Phase 1 — Listing (Savoy Systems programme page)
# ---------------------------------------------------------------------------

def _scrape_listing(sess: requests.Session) -> list[dict]:
    """
    Fetch the Savoy Systems listing page and return all shows with their
    per-performance booking URLs.

    Each <div class="programme"> block contains the show title and a table
    of date/time rows. Each time link href is the direct booking URL for
    that performance seat map.
    """
    log.info("Fetching listing: %s", LISTING_URL)
    soup = fetch_soup(LISTING_URL, sess)

    shows: list[dict] = []
    for div in soup.find_all(
        "div",
        class_=lambda c: c and "programme" in (c if isinstance(c, list) else c.split()),
    ):
        h1 = div.find("h1", class_="title")
        if not h1:
            continue
        title_a = h1.find("a")
        title = clean(title_a.get_text() if title_a else h1.get_text())
        if not title:
            continue

        category = standardize_category(title)

        performances: list[dict] = []
        seen: set[tuple] = set()
        for tr in div.find_all("tr"):
            date_td = tr.find("td", class_="PeformanceListDate")
            if not date_td:
                continue
            date_iso = parse_date_str(clean(date_td.get_text()))
            if not date_iso:
                continue

            a = tr.find("a", class_="Button")
            if not a or not a.get("href"):
                continue
            time_24 = clean(a.get_text())
            booking_url = a["href"]
            if not time_24:
                continue

            key = (date_iso, time_24)
            if key in seen:
                continue
            seen.add(key)
            performances.append(
                {"date_iso": date_iso, "time_24": time_24, "booking_url": booking_url}
            )

        if not performances:
            continue

        shows.append({"title": title, "category": category, "performances": performances})
        log.info("Show: %s (%d perf(s))", title, len(performances))

    log.info("Listing complete: %d shows", len(shows))
    return shows


# ---------------------------------------------------------------------------
# Phase 2 — Seat map (one fetch per performance booking URL)
# ---------------------------------------------------------------------------

def _scrape_seat_map(sess: requests.Session, booking_url: str) -> tuple[list[dict], Optional[int]]:
    """
    Fetch one Savoy Systems allocated-seating page and extract available seats.

    Price groups map seatTypeId -> (type_name, price).
    Available seats have onClick="SelectSeat(seatId, seatTypeId)".
    Capacity = total count of all <a aria-label="Seat *"> elements.

    Returns (seats, total_capacity). On fetch failure returns ([], None).
    """
    try:
        soup = fetch_soup(booking_url, sess)
    except Exception as exc:
        log.warning("Seat map fetch failed (%s): %s", booking_url[-60:], exc)
        return [], None

    # Build price group dict: seatTypeId -> (type_name, price)
    price_groups: dict[str, tuple[str, float]] = {}
    for pg_div in soup.find_all(
        "div",
        class_=lambda c: c and any(
            _PRICE_GROUP_CLASS_RE.match(cls)
            for cls in (c if isinstance(c, list) else c.split())
        ),
    ):
        type_id: Optional[str] = None
        for cls in pg_div.get("class", []):
            m = _PRICE_GROUP_CLASS_RE.match(cls)
            if m:
                type_id = m.group(1)
                break
        if not type_id:
            continue

        type_name_div = pg_div.find("div", class_="SeatType")
        type_name = clean(type_name_div.get_text()) if type_name_div else "Standard"

        price_td = pg_div.find("td", class_="PriceGroupPersonTypePrice")
        if not price_td:
            continue
        price = parse_price(price_td.get_text())
        if price is None:
            continue
        price_groups[type_id] = (type_name, price)

    # All seat elements (available + unavailable) → capacity
    all_seats = soup.find_all(
        "a", attrs={"aria-label": re.compile(r"^Seat ", re.IGNORECASE)}
    )
    capacity: Optional[int] = len(all_seats) if all_seats else None

    # Available seats: those with onClick="SelectSeat(seatId, typeId)"
    seats: list[dict] = []
    for a in all_seats:
        onclick = a.get("onclick") or a.get("onClick") or ""
        if not onclick:
            continue
        m = _SELECT_SEAT_RE.search(onclick)
        if not m:
            continue
        seat_id, type_id = m.group(1), m.group(2)
        if type_id not in price_groups:
            continue
        _, price = price_groups[type_id]
        seats.append({"seat": seat_id, "ticket_price": price})

    log.info(
        "  Seat map: %d available / %s total  url=...%s",
        len(seats), capacity, booking_url[-50:],
    )
    return seats, capacity


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    sess = make_session()
    _load_venue_info(sess)
    shows = _scrape_listing(sess)

    rows: list[dict] = []
    for show in shows:
        seat_pricing: dict[str, list] = {}
        capacity = 0

        for perf in show["performances"]:
            seats, cap = _scrape_seat_map(sess, perf["booking_url"])
            if cap is not None:
                capacity = max(capacity, cap)
                key = f"{perf['date_iso']} {perf['time_24']}"
                seat_pricing[key] = seats

        perfs = show["performances"]
        open_date = perfs[0]["date_iso"] if perfs else ""
        close_date = perfs[-1]["date_iso"] if perfs else ""
        upcoming = [{"date": p["date_iso"], "time": p["time_24"]} for p in perfs]
        currency = "GBP" if seat_pricing else ""

        rows.append({
            "title":               show["title"],
            "venue_url":           LISTING_URL,
            "category":            show["category"],
            "venue":               _venue["name"],
            "address":             _venue["address"],
            "city":                _venue["city"],
            "country":             _venue["country"],
            "open_date":           open_date,
            "close_date":          close_date,
            "booking_start_date":  open_date,
            "booking_end_date":    close_date,
            "upcoming_performances": repr(upcoming),
            "capacity":            capacity if capacity else "",
            "currency":            currency,
            "is_limited_run":      "True" if close_date else "False",
            "seat_pricing":        repr(seat_pricing),
            "scrape_datetime":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        log.info(
            "OK: %-40s perfs=%-2d cap=%-5s pricing_keys=%d",
            show["title"], len(perfs), capacity, len(seat_pricing),
        )

    if not rows:
        log.error("No data scraped!")
        return

    # Backfill capacity for shows whose booking pages had no seat layout.
    # Venue capacity is fixed; we learn it from whichever shows did return seats.
    venue_cap = max((int(r["capacity"]) for r in rows if r.get("capacity")), default=0)
    if venue_cap:
        for r in rows:
            if not r.get("capacity"):
                r["capacity"] = str(venue_cap)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Wrote %d rows -> %s", len(rows), OUTPUT_CSV)

    log.info("Running validator: %s", OUTPUT_CSV)
    report = validate_csv(OUTPUT_CSV)
    if not report.passed:
        log.error("Validation FAILED")
    else:
        log.info("Validation PASSED")


if __name__ == "__main__":
    main()
