-- 002_flow.sql — derived flow series + calibration view.
-- flow stores per-cam_pwr-window pulse sums; calibration K is applied in v_flow
-- so recalibrating never requires a rebuild. See portal/flow.py.

CREATE TABLE flow (
    time        timestamptz NOT NULL,   -- window midpoint
    pulses_sum  bigint,                 -- summed pulses over the stable part of the window
    valid       boolean NOT NULL DEFAULT false,
    n_samples   integer,                -- stable samples used
    window_s    double precision,       -- window span in seconds
    UNIQUE (time)
);

SELECT create_hypertable('flow', 'time', chunk_time_interval => INTERVAL '30 days');

-- Presentation view: applies the externalized calibration K (pulses per litre)
-- and yields flow in litres/minute. NULL flow where invalid or K unset.
CREATE VIEW v_flow AS
SELECT
    f.time,
    f.valid,
    f.pulses_sum,
    f.window_s,
    f.n_samples,
    CASE
        WHEN f.valid AND f.window_s > 0 AND k.k IS NOT NULL AND k.k > 0
        THEN (f.pulses_sum::double precision / k.k) / (f.window_s / 60.0)
        ELSE NULL
    END AS flow_l_per_min
FROM flow f
CROSS JOIN (
    SELECT NULLIF(value, '')::double precision AS k
    FROM config WHERE key = 'flow_pulses_per_litre'
) k;

INSERT INTO metric_meta (source, column_name, unit, description) VALUES
    ('flow', 'pulses_sum',     'count',  'Summed flow-meter pulses over the stable cam_pwr window.'),
    ('flow', 'flow_l_per_min', 'L/min',  'Derived flow rate (v_flow; calibration K from config).');
