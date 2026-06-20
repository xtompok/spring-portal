-- 001_init.sql — core schema for the Skalná data portal.
-- Applied by `portal migrate`. See DESIGN.md for the rationale behind each choice.

CREATE EXTENSION IF NOT EXISTS timescaledb;
-- btree_gist lets a GiST exclusion constraint combine equality (address / location_id)
-- with range overlap (&&) on the validity column.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ---------------------------------------------------------------------------
-- Reference / config
-- ---------------------------------------------------------------------------

-- Free-form key/value config. Calibration constants live here so changing them
-- never requires re-ingesting or re-materializing anything.
CREATE TABLE config (
    key         text PRIMARY KEY,
    value       text,
    description text
);

-- Externalized flow calibration + gating knobs (values to be set/tuned later).
INSERT INTO config (key, value, description) VALUES
    ('flow_pulses_per_litre', NULL, 'Flow-meter calibration K (pulses per litre). NULL until measured.'),
    ('flow_settle_seconds',   '3',  'Seconds to drop after cam_on rises (rotor spin-up) before pulses are trusted.'),
    ('flow_min_stable_seconds','5',  'Minimum stable cam_on duration for a window to count as valid.');

-- Units / descriptions for discoverability ("what is v_in_v?"). Self-documenting layer.
CREATE TABLE metric_meta (
    source      text NOT NULL,
    column_name text NOT NULL,
    unit        text,
    description text,
    PRIMARY KEY (source, column_name)
);

-- ---------------------------------------------------------------------------
-- Sensor placement dimension (temporal SCD for the DS18B20 thermometers)
-- ---------------------------------------------------------------------------

-- Physical probe, identified by its immutable 1-Wire hardware address.
CREATE TABLE sensor (
    address text PRIMARY KEY,
    kind    text DEFAULT 'ds18b20',
    notes   text
);

-- Stable, scientifically-meaningful measurement point a dashboard binds to.
-- Persists even when the probe behind it is swapped.
CREATE TABLE location (
    location_id serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    description text
);

-- Time-bounded assignment of a probe to a location. Current placement is
-- open-ended: validity = tstzrange(from, NULL) = [from, infinity).
CREATE TABLE sensor_placement (
    id          serial PRIMARY KEY,
    address     text NOT NULL REFERENCES sensor(address),
    location_id integer NOT NULL REFERENCES location(location_id),
    validity    tstzrange NOT NULL,
    -- At most one location per probe at any instant...
    EXCLUDE USING gist (address WITH =, validity WITH &&),
    -- ...and at most one probe per location at any instant.
    EXCLUDE USING gist (location_id WITH =, validity WITH &&)
);

-- ---------------------------------------------------------------------------
-- Ingestion bookkeeping
-- ---------------------------------------------------------------------------

CREATE TABLE ingest_run (
    id               bigserial PRIMARY KEY,
    path             text NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    status           text NOT NULL DEFAULT 'running',  -- running | ok | error
    bytes            bigint DEFAULT 0,
    rows_parsed      integer DEFAULT 0,
    rows_inserted    integer DEFAULT 0,
    rows_skipped     integer DEFAULT 0,   -- duplicates / old-format / below sanity floor
    rows_quarantined integer DEFAULT 0,
    note             text
);

-- Nothing is silently lost: structurally-broken rows land here with the reason.
CREATE TABLE quarantine (
    id            bigserial PRIMARY KEY,
    ingest_run_id bigint REFERENCES ingest_run(id),
    source_file   text,
    line_no       integer,
    raw_line      text,
    reason        text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- lab — the station measurements (one wide typed hypertable, faithful to dump_line)
-- ---------------------------------------------------------------------------

CREATE TABLE lab (
    time            timestamptz NOT NULL,   -- to_timestamp(rtctime), UTC
    uptime          bigint NOT NULL,        -- ms since boot (monotonic within a boot)
    rtctime         bigint,                 -- raw epoch seconds as emitted
    base_dir        bigint,
    file_name       bigint,

    router_pwr      boolean,
    switch_pwr      boolean,
    cam_pwr         boolean,                -- powers the flow meter (see flow derivation)
    pulses          bigint,                 -- per-interval count (read-and-reset each sample)

    vin_voltage     double precision,
    batt_voltage    double precision,
    vbus            double precision,
    vcell1          double precision,
    vcell2          double precision,
    vcell3          double precision,
    vcell4          double precision,
    vin_current     double precision,
    batt_current    double precision,

    therm0_addr     text,
    therm0_temp     double precision,
    therm1_addr     text,
    therm1_temp     double precision,
    therm2_addr     text,
    therm2_temp     double precision,
    therm3_addr     text,
    therm3_temp     double precision,

    acc_x           double precision,
    acc_y           double precision,
    acc_z           double precision,
    gyro_x          double precision,
    gyro_y          double precision,
    gyro_z          double precision,
    magn_x          double precision,
    magn_y          double precision,
    magn_z          double precision,
    angle_x         double precision,       -- NULL for new_format_v1 (truncated firmware)
    angle_y         double precision,
    angle_z         double precision,

    format_version  text,                   -- new_format_v1 | new_format_v2
    raw_line_hash   text,                   -- integrity audit, NOT the dedup key
    ingest_run_id   bigint REFERENCES ingest_run(id),

    -- Dedup identity. Must include the partition column (time) for Timescale.
    UNIQUE (time, uptime)
);

SELECT create_hypertable('lab', 'time', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON lab (time DESC);

-- ---------------------------------------------------------------------------
-- weather — scraped nearby observations (in-pocasi.cz, station cheb)
-- ---------------------------------------------------------------------------

CREATE TABLE weather (
    time           timestamptz NOT NULL,    -- Prague local -> UTC at ingest
    source         text NOT NULL,           -- e.g. in-pocasi (future: chmi)
    station        text NOT NULL,           -- e.g. cheb
    temperature_c  double precision,
    wind_dir       text,                    -- raw Czech compass (S/J/V/Z...), faithful
    wind_kmh       double precision,
    humidity_pct   double precision,
    pressure_hpa   double precision,
    precip_mm      double precision,
    sunshine_min   double precision,
    UNIQUE (source, station, time)
);

SELECT create_hypertable('weather', 'time', chunk_time_interval => INTERVAL '30 days');

-- ---------------------------------------------------------------------------
-- v_thermo — labeled, time-correct thermometer readings
-- Unpivots the four raw slots and range-joins the placement valid at each reading.
-- ---------------------------------------------------------------------------

CREATE VIEW v_thermo AS
SELECT
    l.time,
    l.uptime,
    t.address,
    loc.name AS location_name,
    t.temp_c
FROM lab l
CROSS JOIN LATERAL (
    VALUES
        (l.therm0_addr, l.therm0_temp),
        (l.therm1_addr, l.therm1_temp),
        (l.therm2_addr, l.therm2_temp),
        (l.therm3_addr, l.therm3_temp)
) AS t(address, temp_c)
LEFT JOIN sensor_placement p
       ON p.address = t.address
      AND p.validity @> l.time
LEFT JOIN location loc
       ON loc.location_id = p.location_id
WHERE t.address IS NOT NULL
  AND t.address <> '';

-- Metric metadata seeds (extend as columns are added).
INSERT INTO metric_meta (source, column_name, unit, description) VALUES
    ('lab', 'pulses',       'count',  'Flow-meter pulses since previous sample (valid only when cam_pwr).'),
    ('lab', 'vin_voltage',  'V',      'Input (solar/charge) voltage.'),
    ('lab', 'batt_voltage', 'V',      'Battery pack voltage.'),
    ('lab', 'vbus',         'V',      'Bus voltage.'),
    ('lab', 'vin_current',  'A',      'Input current.'),
    ('lab', 'batt_current', 'A',      'Battery current.'),
    ('lab', 'therm0_temp',  'deg C',  'DS18B20 slot 0 temperature (label via v_thermo).'),
    ('weather', 'temperature_c', 'deg C', 'Air temperature at the weather station.'),
    ('weather', 'precip_mm',     'mm',    'Precipitation in the interval.'),
    ('weather', 'pressure_hpa',  'hPa',   'Atmospheric pressure.');
