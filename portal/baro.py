"""Barologger ingestion: load a logger CSV export straight into `baro`.

Like weather, this bypasses the file-drop/loader path — the export is already clean
and structured. A logger export looks like::

    Serial_number:
    2088021
    Project ID:

    Location:
    CHEVAK baro
    LEVEL
    UNIT: kPa
    TEMPERATURE
    UNIT: <deg>C
    Date,Time,ms,LEVEL,TEMPERATURE
    2025/06/28,17:30:00,0,96.6816,21.942
    ...

The file is ISO-8859-1 (the degree sign in the temperature unit line). Timestamps are
Europe/Prague wall-time and are converted to UTC at ingest (DST handled once, here).

Placement is auto-detected from the filename: a name containing 'BARO' is the open-air
reference logger; anything else is the in-tube logger. Ambiguity is a hard error rather
than a silent wrong guess. The serial in the header is stored for provenance and, when
it matches a logger we know, cross-checked against the filename guess.
"""

import csv as _csv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from psycopg2.extras import execute_values

from . import db

PRAGUE = ZoneInfo("Europe/Prague")
SOURCE = "baro"

# Data header row that separates the metadata preamble from the readings.
_DATA_HEADER_PREFIX = "Date,Time"

# Known logger serials -> placement, used only to warn when the filename guess and the
# header serial disagree (e.g. a file was renamed). Not authoritative.
_KNOWN_SERIALS = {
    "2088021": "air",
    "2087258": "spring_tube",
}


def placement_from_filename(name: str) -> str:
    """Map a CSV filename to a placement. 'BARO' (any case) -> air; else spring_tube.

    Raises ValueError if the name is empty / clearly not a logger export, so a bad
    name fails loud instead of being filed under the wrong logger.
    """
    base = name.rsplit("/", 1)[-1]
    if not base.lower().endswith(".csv"):
        raise ValueError(f"not a .csv file: {name!r}")
    return "air" if "baro" in base.lower() else "spring_tube"


def _read_header_serial(lines):
    """Return the serial number from the 'Serial_number:' preamble line, or None."""
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("serial_number"):
            # Serial may be inline ("Serial_number: 123") or on the next line.
            inline = line.split(":", 1)[1].strip() if ":" in line else ""
            if inline:
                return inline
            if i + 1 < len(lines):
                return lines[i + 1].strip() or None
            return None
    return None


def _to_utc(day_str: str, time_str: str):
    """'YYYY/MM/DD' + 'HH:MM:SS' in Europe/Prague -> aware UTC datetime."""
    y, mo, d = (int(x) for x in day_str.split("/"))
    h, mi, s = (int(x) for x in time_str.split(":"))
    local = datetime(y, mo, d, h, mi, s, tzinfo=PRAGUE)
    return local.astimezone(timezone.utc)


_UPSERT = """
INSERT INTO baro (source, placement, serial, time, pressure_kpa, temperature_c)
VALUES %s
ON CONFLICT (placement, time) DO UPDATE SET
  source        = EXCLUDED.source,
  serial        = EXCLUDED.serial,
  pressure_kpa  = EXCLUDED.pressure_kpa,
  temperature_c = EXCLUDED.temperature_c
"""


def _num(s):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_csv(csv_path: str, echo=print) -> int:
    """Parse one logger CSV and upsert its readings into `baro`. Idempotent."""
    placement = placement_from_filename(csv_path)

    with open(csv_path, "r", encoding="iso-8859-1") as fh:
        lines = fh.read().splitlines()

    serial = _read_header_serial(lines)
    expected = _KNOWN_SERIALS.get(serial)
    if expected and expected != placement:
        echo(f"WARNING: serial {serial} is a known {expected!r} logger but the filename "
             f"{csv_path!r} resolved to placement {placement!r} — using {placement!r}.")

    # Find the data header, then parse the readings after it.
    start = None
    for i, line in enumerate(lines):
        if line.startswith(_DATA_HEADER_PREFIX):
            start = i + 1
            break
    if start is None:
        raise ValueError(f"{csv_path}: no '{_DATA_HEADER_PREFIX}...' data header found")

    by_time = {}  # dedup within the file (e.g. DST fall-back collides two local times)
    for row in _csv.reader(lines[start:]):
        if len(row) < 5:
            continue
        date_s, time_s, _ms, level_s, temp_s = row[:5]
        if ":" not in time_s or "/" not in date_s:
            continue
        t = _to_utc(date_s, time_s)
        by_time[t] = (SOURCE, placement, serial, t, _num(level_s), _num(temp_s))

    values = sorted(by_time.values(), key=lambda v: v[3])
    if not values:
        echo(f"baro: no readings in {csv_path}")
        return 0

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ingest_run (path) VALUES (%s) RETURNING id",
                        (csv_path,))
            run_id = cur.fetchone()[0]
            execute_values(cur, _UPSERT, values)
            inserted = cur.rowcount
            cur.execute(
                "UPDATE ingest_run SET finished_at=now(), status='ok', "
                "rows_parsed=%s, rows_inserted=%s, "
                "note=%s WHERE id=%s",
                (len(values), inserted,
                 f"baro {placement} serial={serial}", run_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    span = f"{values[0][3].date()}..{values[-1][3].date()}"
    echo(f"baro: {placement} (serial {serial}) — upserted {inserted}/{len(values)} "
         f"readings, {span}")
    return inserted
