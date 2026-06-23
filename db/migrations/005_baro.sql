-- 005_baro.sql — barologger pressure/temperature series.
--
-- Two submersible loggers downloaded as CSV: one in open air ('air', the "BARO"
-- reference) and one in the spring tube ('spring_tube'). Both report absolute
-- pressure (kPa) and temperature (deg C) at a half-hourly cadence. Stored long by
-- placement (one row per logger reading), mirroring `weather`'s source/station/time
-- shape — adding a third logger later is then free. Raw is sacred: any derived water
-- level (tube - air) belongs in a downstream view, not here.

CREATE TABLE baro (
    time          timestamptz NOT NULL,    -- logger local (Europe/Prague) -> UTC at ingest
    source        text NOT NULL,           -- instrument family tag (e.g. 'baro')
    placement     text NOT NULL,           -- 'air' | 'spring_tube'
    serial        text,                    -- logger serial from the CSV header (provenance)
    pressure_kpa  double precision,        -- absolute pressure as reported (kPa)
    temperature_c double precision,        -- logger temperature (deg C)
    UNIQUE (placement, time)
);

-- A placement is one physical logger; re-downloading an overlapping export must be a
-- no-op (or a clean correction), so (placement, time) is the idempotency key.
SELECT create_hypertable('baro', 'time', chunk_time_interval => INTERVAL '30 days');
CREATE INDEX ON baro (time DESC);

INSERT INTO metric_meta (source, column_name, unit, description) VALUES
    ('baro', 'pressure_kpa',  'kPa',   'Absolute pressure measured by the logger (air = atmosphere; spring_tube = atmosphere + water column).'),
    ('baro', 'temperature_c', 'deg C', 'Logger temperature at its placement.');
