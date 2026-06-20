-- 003_aggregates.sql — TimescaleDB continuous aggregates for fast long-range panels.
-- Raw stays sacred; these are downsampled, incrementally-refreshed rollups.

-- Hourly lab rollup: housekeeping (battery/power) + summed pulses + per-slot temps.
CREATE MATERIALIZED VIEW lab_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    count(*)                    AS n_samples,
    avg(batt_voltage)           AS batt_voltage_avg,
    min(batt_voltage)           AS batt_voltage_min,
    max(batt_voltage)           AS batt_voltage_max,
    avg(vin_voltage)            AS vin_voltage_avg,
    avg(vin_current)            AS vin_current_avg,
    avg(batt_current)           AS batt_current_avg,
    avg(vin_voltage * vin_current)  AS power_in_avg_w,
    sum(pulses)                 AS pulses_sum,
    avg(therm0_temp)            AS therm0_temp_avg,
    avg(therm1_temp)            AS therm1_temp_avg,
    avg(therm2_temp)            AS therm2_temp_avg,
    avg(therm3_temp)            AS therm3_temp_avg
FROM lab
GROUP BY bucket
WITH NO DATA;

SELECT add_continuous_aggregate_policy('lab_hourly',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');

-- Daily weather rollup.
CREATE MATERIALIZED VIEW weather_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time)  AS bucket,
    source,
    station,
    avg(temperature_c)          AS temperature_c_avg,
    min(temperature_c)          AS temperature_c_min,
    max(temperature_c)          AS temperature_c_max,
    sum(precip_mm)              AS precip_mm_sum,
    avg(pressure_hpa)           AS pressure_hpa_avg,
    avg(humidity_pct)           AS humidity_pct_avg
FROM weather
GROUP BY bucket, source, station
WITH NO DATA;

SELECT add_continuous_aggregate_policy('weather_daily',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '1 day',
    schedule_interval => INTERVAL '6 hours');
