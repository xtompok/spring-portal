"""Idempotent file-drop loader for lab SD backups.

A backup is a tree of numbered directories, each with a COLUMNS.TXT header and
NNNNN.TXT data files. We:

  * skip directories whose COLUMNS.TXT lacks `rtctime` (old format, unanchorable);
  * parse each data row via the firmware-derived spec (portal.formats);
  * skip rows below the sanity floor / without a usable rtctime;
  * quarantine structurally-broken rows (raw line + reason), never dropping silently;
  * COPY parsed rows into an UNLOGGED staging table, then
    INSERT ... SELECT DISTINCT ON (time, uptime) ... ON CONFLICT DO NOTHING into lab.

Re-running on an overlapping backup is a no-op for already-seen rows.
"""

import csv
import hashlib
import io
import pathlib
from datetime import datetime, timezone

from . import db, formats

# Columns we populate in lab (and COPY into staging), in COPY order.
_COPY_COLUMNS = [
    "time", "uptime", "rtctime", "base_dir", "file_name",
    "router_pwr", "switch_pwr", "cam_pwr", "pulses",
    "vin_voltage", "batt_voltage", "vbus",
    "vcell1", "vcell2", "vcell3", "vcell4", "vin_current", "batt_current",
    "therm0_addr", "therm0_temp", "therm1_addr", "therm1_temp",
    "therm2_addr", "therm2_temp", "therm3_addr", "therm3_temp",
    "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z",
    "magn_x", "magn_y", "magn_z", "angle_x", "angle_y", "angle_z",
    "format_version", "raw_line_hash", "ingest_run_id",
]

_FLUSH_ROWS = 50_000


class _Stats:
    def __init__(self):
        self.parsed = self.inserted = self.skipped = self.quarantined = self.bytes = 0


def _csv_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def _numbered_dirs(root: pathlib.Path):
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.isdigit()),
        key=lambda p: int(p.name),
    )


def _data_files(dir_path: pathlib.Path):
    return sorted(
        (p for p in dir_path.iterdir()
         if p.is_file() and p.suffix.upper() == ".TXT" and p.stem.isdigit()),
        key=lambda p: int(p.stem),
    )


def load(path: str, echo=print) -> dict:
    root = pathlib.Path(path)
    if not root.is_dir():
        raise NotADirectoryError(f"{path} is not a directory")

    conn = db.connect()
    stats = _Stats()

    # Record the run first and commit, so it survives a later failure.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ingest_run (path) VALUES (%s) RETURNING id", (str(root),))
        run_id = cur.fetchone()[0]
    conn.commit()

    staging = f"lab_staging_{run_id}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE UNLOGGED TABLE {staging} (LIKE lab INCLUDING DEFAULTS)"
            )

        buf = io.StringIO()
        writer = csv.writer(buf)
        pending = 0

        def flush():
            nonlocal buf, writer, pending
            if pending == 0:
                return
            buf.seek(0)
            with conn.cursor() as cur:
                cur.copy_expert(
                    f"COPY {staging} ({','.join(_COPY_COLUMNS)}) "
                    f"FROM STDIN WITH (FORMAT csv)",
                    buf,
                )
            buf = io.StringIO()
            writer = csv.writer(buf)
            pending = 0

        for d in _numbered_dirs(root):
            header_path = d / "COLUMNS.TXT"
            if not header_path.exists():
                continue
            header = header_path.read_text(encoding="utf-8", errors="ignore")
            if not formats.header_is_new_format(header):
                continue  # old format, no wall clock

            for f in _data_files(d):
                stats.bytes += f.stat().st_size
                with f.open("r", encoding="utf-8", errors="ignore") as fh:
                    for line_no, raw in enumerate(fh, 1):
                        if not raw.strip():
                            continue
                        stats.parsed += 1
                        try:
                            row = formats.parse_row(raw)
                        except formats.ParseError as exc:
                            stats.quarantined += 1
                            with conn.cursor() as cur:
                                cur.execute(
                                    "INSERT INTO quarantine "
                                    "(ingest_run_id, source_file, line_no, raw_line, reason) "
                                    "VALUES (%s,%s,%s,%s,%s)",
                                    (run_id, str(f), line_no, raw.rstrip("\n"), str(exc)),
                                )
                            continue

                        rtc = row["rtctime"]
                        if rtc is None or rtc < formats.SANITY_FLOOR_EPOCH:
                            stats.skipped += 1  # cold-boot garbage; recoverable from raw
                            continue

                        ts = datetime.fromtimestamp(rtc, tz=timezone.utc)
                        row["time"] = ts.isoformat()
                        row["raw_line_hash"] = hashlib.sha1(
                            raw.rstrip("\n").encode("utf-8")
                        ).hexdigest()
                        row["ingest_run_id"] = run_id

                        writer.writerow(_csv_value(row[c]) for c in _COPY_COLUMNS)
                        pending += 1
                        if pending >= _FLUSH_ROWS:
                            flush()

        flush()

        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO lab ({','.join(_COPY_COLUMNS)}) "
                f"SELECT DISTINCT ON (time, uptime) {','.join(_COPY_COLUMNS)} "
                f"FROM {staging} ORDER BY time, uptime "
                f"ON CONFLICT (time, uptime) DO NOTHING"
            )
            stats.inserted = cur.rowcount
            cur.execute(f"DROP TABLE {staging}")
            cur.execute(
                "UPDATE ingest_run SET finished_at=now(), status='ok', bytes=%s, "
                "rows_parsed=%s, rows_inserted=%s, rows_skipped=%s, rows_quarantined=%s "
                "WHERE id=%s",
                (stats.bytes, stats.parsed, stats.inserted, stats.skipped,
                 stats.quarantined, run_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()  # undoes staging + this run's inserts/quarantine atomically
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ingest_run SET finished_at=now(), status='error' WHERE id=%s",
                (run_id,),
            )
        conn.commit()
        raise
    finally:
        conn.close()

    echo(
        f"run {run_id}: parsed {stats.parsed}, inserted {stats.inserted}, "
        f"skipped {stats.skipped}, quarantined {stats.quarantined}"
    )
    return {
        "run_id": run_id,
        "parsed": stats.parsed,
        "inserted": stats.inserted,
        "skipped": stats.skipped,
        "quarantined": stats.quarantined,
    }
