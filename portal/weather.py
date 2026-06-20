"""Weather ingestion: scrape in-pocasi.cz archive and upsert directly into `weather`.

Bypasses the file-drop/loader path — the data is already clean and structured, so it
goes straight to the table. Scraped times are Prague local wall-time and converted to
UTC at ingest (DST handled once, here). Folds in the logic from the standalone
download_weather.py.
"""

import csv as _csv
import time as _time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from psycopg2.extras import execute_values

from . import db

BASE_URL = "https://www.in-pocasi.cz/archiv/{station}/"
PRAGUE = ZoneInfo("Europe/Prague")
DELAY_S = 1.0
TIMEOUT_S = 30


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _date_range(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _clean_number(text):
    if text is None:
        return None
    t = text.strip()
    if t in ("", "-"):
        return None
    number = ""
    for ch in t:
        if ch.isdigit() or ch in ".-":
            number += ch
        elif number:
            break
    try:
        return float(number)
    except ValueError:
        return None


def _parse_wind(text):
    if text is None:
        return None, None
    t = text.strip()
    if t in ("", "-"):
        return None, None
    parts = t.split(",", 1)
    direction = parts[0].strip() or None
    speed = _clean_number(parts[1]) if len(parts) > 1 else None
    return direction, speed


def _to_utc(day: date, hhmm: str):
    """Prague local 'HH:MM' on `day` -> aware UTC datetime."""
    h, m = hhmm.strip().split(":")
    local = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=PRAGUE)
    return local.astimezone(timezone.utc)


def _fetch_day(session, station, day):
    url = BASE_URL.format(station=station)
    r = session.get(
        url, params={"den": day.isoformat()}, timeout=TIMEOUT_S,
        headers={"User-Agent": "Mozilla/5.0 (skalna-portal)"},
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="table-data")
    if table is None:
        for cand in soup.find_all("table"):
            if any(len(tr.find_all("td")) >= 7 for tr in cand.find_all("tr")):
                table = cand
                break
    if table is None:
        return []

    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 7:
            continue
        time_, temp, wind, humidity, pressure, precip, sunshine = cells[:7]
        if not time_ or ":" not in time_:
            continue
        wind_dir, wind_kmh = _parse_wind(wind)
        rows.append((
            _to_utc(day, time_), _clean_number(temp), wind_dir, wind_kmh,
            _clean_number(humidity), _clean_number(pressure),
            _clean_number(precip), _clean_number(sunshine),
        ))
    return rows


_UPSERT = """
INSERT INTO weather
  (source, station, time, temperature_c, wind_dir, wind_kmh,
   humidity_pct, pressure_hpa, precip_mm, sunshine_min)
VALUES %s
ON CONFLICT (source, station, time) DO UPDATE SET
  temperature_c = EXCLUDED.temperature_c,
  wind_dir      = EXCLUDED.wind_dir,
  wind_kmh      = EXCLUDED.wind_kmh,
  humidity_pct  = EXCLUDED.humidity_pct,
  pressure_hpa  = EXCLUDED.pressure_hpa,
  precip_mm     = EXCLUDED.precip_mm,
  sunshine_min  = EXCLUDED.sunshine_min
"""


# Columns in the value tuple that in-pocasi reports as a running total since local
# midnight: precip_mm (index 8) and sunshine_min (index 9).
_CUMULATIVE_IDX = (8, 9)


def _to_interval(values):
    """Convert cumulative-since-midnight columns to per-interval values: delta from the
    previous reading, with a drop (the midnight reset, or a data gap) taken as the value
    itself. `values` must be sorted by time. Each column is tracked independently; a
    None value passes through and breaks that column's running total so the next reading
    is treated as a fresh start."""
    out = []
    prev = {i: None for i in _CUMULATIVE_IDX}
    for v in values:
        v = list(v)
        for i in _CUMULATIVE_IDX:
            cur = v[i]
            p = prev[i]
            if cur is None:
                prev[i] = None
            elif p is None or cur < p:      # first reading or midnight reset
                prev[i] = cur
            else:
                v[i] = round(cur - p, 2)
                prev[i] = cur
        out.append(tuple(v))
    return out


def _upsert(conn, source, station, rows):
    # Dedup within the batch on the conflict key (source, station, time): two local
    # times can map to one UTC instant on the DST fall-back day, and ON CONFLICT
    # DO UPDATE cannot affect the same row twice in one command. Last value wins.
    by_key = {}
    for (t, temp, wdir, wkmh, hum, pres, prec, sun) in rows:
        by_key[(source, station, t)] = (
            source, station, t, temp, wdir, wkmh, hum, pres, prec, sun
        )
    values = sorted(by_key.values(), key=lambda v: v[2])  # by time, for delta calc
    values = _to_interval(values)
    if not values:
        return 0
    with conn.cursor() as cur:
        execute_values(cur, _UPSERT, values)
    return len(values)


def fetch(start: date, end: date, station: str, source: str, echo=print) -> int:
    if end < start:
        raise ValueError("end date is before start date")
    conn = db.connect()
    total = 0
    try:
        with requests.Session() as session:
            days = list(_date_range(start, end))
            for i, day in enumerate(days, 1):
                rows = _fetch_day(session, station, day)
                total += _upsert(conn, source, station, rows)
                conn.commit()
                echo(f"[{i}/{len(days)}] {day}: {len(rows)} rows")
                if i < len(days):
                    _time.sleep(DELAY_S)
    finally:
        conn.close()
    echo(f"weather: upserted {total} rows for {station}")
    return total


def load_csv(csv_path: str, station: str, source: str, echo=print) -> int:
    """Backfill from an existing download_weather.py CSV (date,time,... columns)."""
    conn = db.connect()
    total = 0
    try:
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = _csv.DictReader(fh)
            batch = []
            for r in reader:
                d = date.fromisoformat(r["date"])
                batch.append((
                    _to_utc(d, r["time"]),
                    _clean_number(r.get("temperature_C")),
                    (r.get("wind_dir") or None),
                    _clean_number(r.get("wind_kmh")),
                    _clean_number(r.get("humidity_pct")),
                    _clean_number(r.get("pressure_hPa")),
                    _clean_number(r.get("precip_mm")),
                    _clean_number(r.get("sunshine_min")),
                ))
            total = _upsert(conn, source, station, batch)
        conn.commit()
    finally:
        conn.close()
    echo(f"weather: backfilled {total} rows from {csv_path}")
    return total
