# Skalná data portal

Time-series portal for an unmanned water-spring monitoring station near Skalná
(Cheb district). Ingests the station's SD-card data and scraped weather into
PostgreSQL + TimescaleDB, presents it in Grafana. See [DESIGN.md](DESIGN.md) for the
full rationale.

## Stack

- **PostgreSQL + TimescaleDB** — storage (`db` service)
- **Grafana** — dashboards (`grafana` service, provisioned as code)
- **`portal` CLI** (Python + click, raw psycopg2) — ingestion, weather, flow
- **supercronic** (`scheduler` service) — daily weather, nightly flow rebuild + `pg_dump`

## Quick start

```bash
cp .env.example .env          # then edit passwords
docker compose up -d db grafana scheduler

# apply schema
docker compose run --rm portal migrate

# load a lab SD backup (place/clone it under ./landing first)
docker compose run --rm portal load /landing/data_2026-06-20

# backfill weather from an existing CSV, then fetch forward
docker compose run --rm portal weather load-csv /landing/cheb_2025-09-15_2026-06-20.csv
docker compose run --rm portal weather fetch --from 2026-06-01 --to 2026-06-20

# (re)build the derived flow series
docker compose run --rm portal flow rebuild
```

Grafana: tunnel and open <http://localhost:3000> (`ssh -L 3000:localhost:3000 server`).
Default dashboards: Overview, Spring science, Station health.

## Local dev without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .
export DATABASE_URL=postgresql://skalna:change-me@localhost:5432/skalna
portal migrate
portal load /path/to/backup
```

## Key concepts

- **Format:** new_format only (rows with `rtctime`). Parsing is driven by the
  firmware `dump_line()` layout, not the buggy `COLUMNS.TXT` — 34 fields →
  `new_format_v1` (angle_* NULL), 37 → `new_format_v2`. See `portal/formats.py`.
- **Dedup:** lab `(time, uptime)` DO NOTHING; weather `(source, station, time)`
  DO UPDATE. Bulk lab via COPY → staging → `INSERT … ON CONFLICT`. Re-loading an
  overlapping backup is a no-op.
- **Time:** stored UTC, displayed Prague. Lab `to_timestamp(rtctime)` (RTC is UTC);
  weather Prague→UTC at ingest. Rows before 2024-01-01 are dropped as cold-boot garbage.
- **Thermometers:** labeled by *time-correct* placement (`sensor` / `location` /
  `sensor_placement`), resolved in the `v_thermo` view. Record probe placements, e.g.:
  ```sql
  INSERT INTO sensor (address) VALUES ('286164351f80ebde') ON CONFLICT DO NOTHING;
  INSERT INTO location (name, description) VALUES ('spring_inlet', 'spring water inlet');
  INSERT INTO sensor_placement (address, location_id, validity)
    VALUES ('286164351f80ebde', (SELECT location_id FROM location WHERE name='spring_inlet'),
            tstzrange('2025-09-15', NULL));
  ```
- **Flow:** derived, rebuildable `flow` table; the per-window aggregation harness is
  in `portal/flow.py` with the physics left as a `compute_flow` **stub**. Calibration
  `K` (`config.flow_pulses_per_litre`) is applied in the `v_flow` view, so
  recalibrating never needs a rebuild.

## Backups

Nightly `pg_dump -Fc` to `./backups` (kept ~14 days). This is the **only** protection
for human-curated data (sensor placements, manual corrections, config) — it is not
reconstructable from raw SD files.
```bash
docker compose run --rm portal sh -c 'pg_restore -h db -U $POSTGRES_USER -d $POSTGRES_DB --clean /backups/skalna_YYYYMMDD.dump'
```
