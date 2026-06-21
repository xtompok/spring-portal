-- 004_events.sql — station events/alerts + router maintenance flag.
-- See DESIGN.md §Presentation: "station-offline / low-battery alerts are a quick
-- later add … alert wiring deferred (needs a notification channel)." This adds the
-- store; a notification channel can read from it later.

-- ---------------------------------------------------------------------------
-- station_events — generic critical-event/alert log for the station.
-- First consumer: the router posts a 'rtc_backward_jump' event when the station's
-- newest data fails to advance past the ingested cursor (suspected DS3231 battery
-- failure) and then skips the upload. Kept generic so future conditions
-- (low-battery, station-offline, …) reuse the same table.
-- ---------------------------------------------------------------------------

CREATE TABLE station_events (
    id          bigserial PRIMARY KEY,
    station     text NOT NULL DEFAULT 'skalna',
    kind        text NOT NULL,                       -- e.g. 'rtc_backward_jump'
    severity    text NOT NULL DEFAULT 'warning',     -- info | warning | critical
    detail      jsonb,                               -- structured context (cursor, observed time, …)
    first_seen  timestamptz NOT NULL DEFAULT now(),  -- server clock at first occurrence
    last_seen   timestamptz NOT NULL DEFAULT now(),  -- server clock at most recent occurrence
    occurrences integer NOT NULL DEFAULT 1,          -- bumped instead of inserting a duplicate
    resolved_at timestamptz                          -- NULL = still open
);

CREATE INDEX ON station_events (first_seen DESC);

-- At most one OPEN event per (station, kind): the dedup target for `portal event add`,
-- so a condition that recurs every daily session bumps occurrences instead of
-- spamming one row per day.
CREATE UNIQUE INDEX station_events_open_uniq
    ON station_events (station, kind)
    WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------------------
-- Router maintenance flag (read by `portal session-info`).
-- When '1', the router keeps itself powered after upload so an operator can SSH
-- in — still bounded by the STM32's battery-safety cap (firmware side).
-- ---------------------------------------------------------------------------

INSERT INTO config (key, value, description) VALUES
    ('router_maintenance_hold', '0',
     'When 1, the router stays powered after upload for operator access (bounded by the STM32 battery-safety cap).');
