"""Station control surface for the LTE router (raw psycopg2, no ORM).

The router calls these over SSH on each daily upload session:

  * ``portal session-info`` -> JSON ``{station, max_rtctime, maintenance_flag}``
    - ``max_rtctime`` is the upload cursor: the newest ingested wall-time as epoch
      seconds (== the firmware ``rtctime``). The router uploads only records newer
      than this. ``null`` when ``lab`` is empty (first ever upload). Time-based and
      server-authoritative, so an SD reformat/renumber is a non-event.
    - ``maintenance_flag`` is the ``router_maintenance_hold`` config knob; when set
      the router stays powered after upload for operator access.
  * ``portal event add ...`` -> record a station_events alert (first consumer:
    ``rtc_backward_jump`` when the station's clock stops advancing). De-duped against
    an already-open event of the same (station, kind) so a daily-recurring condition
    bumps a counter instead of spamming one row per session.

See DESIGN.md and the upload plan.
"""

import json

from . import db

DEFAULT_STATION = "skalna"

# Config values that read as "flag set".
_TRUTHY = {"1", "true", "yes", "on"}


def session_info(station: str = DEFAULT_STATION) -> dict:
    """Return the upload cursor + maintenance flag for the router."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT extract(epoch FROM max(time))::bigint FROM lab")
            max_rtctime = cur.fetchone()[0]
            cur.execute(
                "SELECT value FROM config WHERE key = 'router_maintenance_hold'"
            )
            row = cur.fetchone()
        flag = bool(row) and (row[0] or "").strip().lower() in _TRUTHY
        return {
            "station": station,
            "max_rtctime": max_rtctime,
            "maintenance_flag": flag,
        }
    finally:
        conn.close()


def add_event(kind: str, severity: str = "warning", detail=None,
              station: str = DEFAULT_STATION) -> dict:
    """Record a station event, de-duping an already-open event of the same
    (station, kind): bumps occurrences + last_seen instead of inserting a duplicate.

    Returns ``{id, created, occurrences}`` where ``created`` is True for a fresh row.
    """
    detail_json = json.dumps(detail) if detail is not None else None
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO station_events (station, kind, severity, detail)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (station, kind) WHERE resolved_at IS NULL
                DO UPDATE SET last_seen    = now(),
                              occurrences  = station_events.occurrences + 1,
                              severity     = EXCLUDED.severity,
                              detail       = COALESCE(EXCLUDED.detail, station_events.detail)
                RETURNING id, (xmax = 0) AS created, occurrences
                """,
                (station, kind, severity, detail_json),
            )
            event_id, created, occ = cur.fetchone()
        conn.commit()
        return {"id": event_id, "created": created, "occurrences": occ}
    finally:
        conn.close()


def resolve_event(kind: str, station: str = DEFAULT_STATION) -> int:
    """Close any open event(s) of (station, kind). Returns how many were resolved."""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE station_events SET resolved_at = now()
                WHERE station = %s AND kind = %s AND resolved_at IS NULL
                """,
                (station, kind),
            )
            n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def list_events(open_only: bool = True, station: str = None) -> list:
    """Return events (most recent first) as plain dicts."""
    clauses, params = [], []
    if open_only:
        clauses.append("resolved_at IS NULL")
    if station:
        clauses.append("station = %s")
        params.append(station)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, station, kind, severity, detail,
                       first_seen, last_seen, occurrences, resolved_at
                FROM station_events {where}
                ORDER BY last_seen DESC
                """,
                params,
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
