-- Phase 4: API tokens (F-EXP-02) and power-quality aggregate (F-QUAL).

-- Read-only API tokens. Only a SHA-256 of the token is stored; the prefix
-- (first characters) is kept so a token can be recognised in the UI.
CREATE TABLE api_token (
    id           bigserial   PRIMARY KEY,
    name         text        NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    token_hash   bytea       NOT NULL UNIQUE,
    prefix       text        NOT NULL,
    user_id      bigint      NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    last_used_at timestamptz,
    revoked_at   timestamptz
);

-- Voltage per phase per hour with min/max, plus the sag/swell/failure
-- counters at the end of the hour. Kept without retention (24 rows a day),
-- so voltage history outlives the 30-day raw retention.
CREATE MATERIALIZED VIEW quality_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 hour', ts) AS bucket,
    min(voltage_l1_v) AS l1_min_v, avg(voltage_l1_v) AS l1_avg_v, max(voltage_l1_v) AS l1_max_v,
    min(voltage_l2_v) AS l2_min_v, avg(voltage_l2_v) AS l2_avg_v, max(voltage_l2_v) AS l2_max_v,
    min(voltage_l3_v) AS l3_min_v, avg(voltage_l3_v) AS l3_avg_v, max(voltage_l3_v) AS l3_max_v,
    max(current_l1_a) AS l1_max_a, max(current_l2_a) AS l2_max_a, max(current_l3_a) AS l3_max_a,
    last(sag_l1_count, ts)   AS sag_l1_count,   last(sag_l2_count, ts)   AS sag_l2_count,
    last(sag_l3_count, ts)   AS sag_l3_count,
    last(swell_l1_count, ts) AS swell_l1_count, last(swell_l2_count, ts) AS swell_l2_count,
    last(swell_l3_count, ts) AS swell_l3_count,
    last(power_fail_count, ts)      AS power_fail_count,
    last(long_power_fail_count, ts) AS long_power_fail_count,
    count(*) AS samples
FROM reading_raw
GROUP BY 1
WITH NO DATA;

SELECT add_continuous_aggregate_policy('quality_1h',
    start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes');
