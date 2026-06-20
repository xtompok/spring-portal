# Skalná data portal — design

Online portal for monitoring an underground water spring in an unmanned, off-grid
shelter near Skalná (Cheb district, West Bohemia). It ingests measurements from the
station, scrapes nearby weather, stores everything in a time-series database, and
presents it in Grafana.

This document is the agreed design. It is the source of truth for *why* the system
is shaped the way it is; the code implements it.

## Scope & non-goals

- **One spring, one station.** No multi-station clustering. A `station`/`source`
  tag is still carried so adding sources later is free, but nothing is built for scale.
- Develop on a laptop, deploy to a single self-hosted Linux server. Dev/prod parity
  via Docker Compose.
- A public graph viewer for visitors is **out of scope for now** (Grafana, accessed
  over an SSH tunnel, is the interface).

## Data sources

| Source   | Origin                                   | Path into the system            | Status   |
|----------|------------------------------------------|----------------------------------|----------|
| `lab`    | ReProBox firmware, SD-card `.TXT` files  | file-drop landing → loader CLI   | active   |
| `weather`| in-pocasi.cz archive (station `cheb`)    | `portal weather fetch` → direct  | active   |
| `seismic`| local seismic lab                        | TBD                              | future   |

## Platform

- **PostgreSQL + TimescaleDB.** Relational core, *not* a dedicated TSDB. The
  requirement "easy to add a source / add a metric / correct existing data" is a
  schema-evolution + mutation requirement, which is exactly where InfluxDB-style
  TSDBs are weakest and Postgres is strongest (`ALTER TABLE`, `UPDATE`, `pg_dump`).
  TimescaleDB adds hypertable partitioning + continuous aggregates for the
  higher-rate lab stream — and learning Timescale is an explicit goal.
- **Docker Compose**: `db`, `grafana`, `portal` (CLI image), `scheduler` (supercronic).
- **Python + `click`** single CLI. **Raw psycopg2** as the DB layer (no ORM, no
  SQLAlchemy). Schema lives in **numbered raw-SQL migrations** applied by a tiny
  runner — the schema stays readable SQL and is itself a Timescale learning artifact.

## Schema model

Typed **wide hypertable per source** (`lab`, `weather`, later `seismic`) with clear,
explicitly-named columns so SELECTs are straightforward. A `metric_meta` table
records units/descriptions for discoverability. The long/EAV ("metric_name, value")
model was rejected: the data is mixed-type (text addresses, boolean power flags,
numeric everything-else) and `ALTER TABLE ADD COLUMN` is already cheap in Postgres,
so EAV's flexibility buys nothing while making every query a pivot.

### Time

- Everything stored as **`timestamptz` in UTC**; displayed in `Europe/Prague` in Grafana.
- **Lab:** `time = to_timestamp(rtctime)`. The DS3231 is set to UTC (verified: a live
  reading of `1781981639` = 2026-06-20 18:53 UTC, ~8 min behind true UTC — i.e. lined
  up with UTC, not Prague). Raw `rtctime` (bigint) and `uptime` (bigint, ms) are kept
  as columns for ordering-within-boot, reboot detection, and drift diagnosis.
- **Weather:** scraped local Prague wall-time → UTC at ingest (`AT TIME ZONE`), so DST
  is handled once at the boundary.
- **Cumulative-since-midnight columns (`precip_mm`, `sunshine_min`):** in-pocasi reports
  these as a running total that resets at local midnight. The loader converts them to
  **per-interval** values on ingest (delta from the previous reading; a drop = midnight
  reset/gap → the value itself), so each means "amount in this 10-min slot" and
  dashboards aggregate them with `sum()` (not `avg`). See `weather._to_interval`.
- **Sanity floor:** rows whose `time` < `2024-01-01` are cold-boot epoch garbage
  (RTC never set) → quarantined, never inserted into the measurement tables.
- **Known caveat:** the RTC drifts (~minutes) and is only as good as the last
  `rtcset`. Acceptable for now; could later be corrected from NTP when the router is up.

### Thermometers — temporal sensor placement

The firmware emits four DS18B20 readings by **1-Wire enumeration order**
(`therm0..therm3`), each carrying its hardware **address**. The slot index is *not*
stable across reboots/reconnects, and a physical probe can be **moved to a different
location over time**. So the slot is meaningless for analysis and the label is
time-dependent.

Modeled as a slowly-changing dimension:

- `sensor(address, …)` — the physical probe (immutable hardware id).
- `location(location_id, name, …)` — the stable, scientifically-meaningful point a
  dashboard binds to (e.g. `spring_inlet`); persists even when the probe behind it changes.
- `sensor_placement(address, location_id, validity tstzrange)` — the time-bounded
  assignment. Current placement is open-ended `[from, infinity)`. A GiST **exclusion
  constraint** enforces *at most one probe per location and one location per probe at
  any instant*.
- `v_thermo(time, uptime, address, location_name, temp_c)` — derived view that
  unpivots the four raw slots and **range-joins** placement by timestamp containment
  (`validity @> time`), so a reading resolves to the location that probe occupied *then*.
  `LEFT JOIN`, so readings in an unassigned gap surface with `NULL` label rather than
  being dropped; backfilling a placement later makes every dashboard self-correct.

The raw `lab` table keeps all four slots faithfully; labeling lives entirely in the
derived layer.

## Ingestion

- **One idempotent, file-based loader; two delivery paths feeding one landing area.**
  Manual = operator copies an SD backup tree into the landing dir and runs
  `portal load`. Automatic (future) = router `rsync`s into the *same* dir. The portal
  only ever depends on "raw files appear in a directory", so it is fully decoupled
  from the (separate, unbuilt) firmware-transfer plan and testable today against
  existing dumps.
- **Format = new_format only** (has `rtctime`). The firmware `dump_line()` format
  string is the **versioned ground truth**, *not* the per-dir `COLUMNS.TXT` (whose
  header is corrupted by a firmware bug: `therm4_tempacc_x` mash, and its field count
  disagrees with the data). Detection: a dir's `COLUMNS.TXT` containing `rtctime`
  marks it new-format; data rows are then mapped by **field count**:
  - **34 fields → `new_format_v1`** (legacy): `base_dir … magn_z`; `angle_*` left NULL
    (the firmware truncation dropped them).
  - **37 fields → `new_format_v2`** (current): adds `angle_x/y/z`.
  - **no `rtctime` in `COLUMNS.TXT` → old format → skipped** (no wall clock, unanchorable).
- **Permissive store-everything.** Store all present fields; NULL the missing tail;
  filter "looks wrong" downstream in views. A row is rejected only if it is
  *structurally* unparseable (wrong field count for the detected version, non-numeric
  in a numeric column) and then goes to a **`quarantine`** table with the raw line +
  reason — nothing is silently lost.

### Idempotency / dedup

- **Lab identity = `UNIQUE(time, uptime)`**, `ON CONFLICT DO NOTHING` (raw measurements
  are immutable). `uptime` is strictly monotonic within a boot; across reboots `time`
  has advanced — so the pair stays unique. Survives SD reformat/renumbering (unlike a
  `(base_dir, file_name)` provenance key, which would silently drop new data after a
  reformat). A `raw_line_hash` column is kept as an **integrity audit**: same
  `(time, uptime)` arriving with a different hash is a real anomaly to surface.
- **Weather identity = `UNIQUE(source, station, time)`**, `ON CONFLICT DO UPDATE`
  (re-scraping can legitimately refine a slot, and manual corrections are wanted).
- **Bulk lab path:** `COPY` parsed rows into an UNLOGGED staging table →
  `INSERT … SELECT … ON CONFLICT DO NOTHING`. COPY speed + idempotency. The loader is
  thus crash-safe and resumable for free.
- **Bookkeeping:** `ingest_run` (file, bytes, parsed/inserted/skipped/quarantined,
  timestamp) and `quarantine` (raw line + reason) tables.

## Derived data

- **Raw is sacred; all derivation/filtering happens downstream** (views, continuous
  aggregates, the flow script). Derived artifacts are always rebuildable from raw.
- **Flow** is a derived, rebuildable `flow` table — *not* a SQL aggregate, because the
  flow meter is power-hungry and gated on the `cam` output (`cam_on`), powered ~15 s
  every 60 s. `pulses` is a **per-interval count** (read-and-reset each sample), so flow
  is only meaningful inside a `cam_on` window after the meter spins up. A **Python
  harness** walks `cam_on` windows, drops settling samples, and emits one value per
  window (~1/min) or NULL when invalid (window too short ⇒ NULL; stable window with
  zero pulses ⇒ valid 0). **The pulse→flow computation itself is a stub to be filled
  in later.** Calibration `K` (pulses/litre) is externalized in a `config` table so
  recalibrating never requires re-ingest.
- **Continuous aggregates** downsample temps/power/summed-pulses for fast long-range
  panels; thin views apply calibration & units.

## Presentation

Grafana, provisioned-as-code (Postgres datasource + dashboard JSON in the repo).
Initial dashboards (iterate later):

1. **Overview** — latest flow, water temps per location, battery voltage,
   time-since-last-sample (alive?), power-rail states.
2. **Spring science** — flow over time, water temperatures by `location`, with weather
   (precip/air-temp) overlaid to see whether rain drives flow/temperature.
3. **Station health** — battery voltage/cells, currents, computed power, internal temp,
   router/cam/switch on-times, reboot detection (uptime resets), ingest/quarantine counts.

`time-since-last-sample` and battery are kept first-class so **station-offline /
low-battery alerts** are a quick later add. Alert wiring is deferred (needs a
notification channel).

## Ops

- `docker compose up` runs the whole stack. Grafana accessed via SSH tunnel; TLS /
  reverse proxy deferred.
- **`scheduler` (supercronic)** runs an in-repo crontab: daily `weather fetch`,
  `flow rebuild` after loads, nightly `pg_dump`.
- **Backups:** nightly rotated `pg_dump` (custom format) to a `backups` volume.
  Note: the `sensor`/`location`/`sensor_placement` dimensions, manual corrections, and
  `config` are **not** reconstructable from raw files — `pg_dump` is their only
  protection. Off-box sync is a later step.

## Build sequence

1. Repo skeleton + compose (`db`+`grafana`) + `.env` + migration runner.
2. Migration `001`: tables, hypertables, dimensions, exclusion constraints, views, bookkeeping.
3. **Loader** + version detection, validated against `data_2026-06-20/` (risky core, done early).
4. Weather fetch (fold in `download_weather.py`) + backfill the Cheb CSV.
5. Flow harness (stub) + continuous aggregates + calibration view.
6. Grafana three dashboards.
7. supercronic + nightly `pg_dump` + README.
