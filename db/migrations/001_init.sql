-- Phase 1 schema: raw readings, aggregates, events, settings, collector status.
-- All timestamps are UTC (timestamptz). Energy is always derived from counter
-- deltas, never from integrated power.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Raw 1 s readings (retention 30 days) ---------------------------------------
CREATE TABLE reading_raw (
    ts                    timestamptz      NOT NULL,
    epoch                 integer          NOT NULL DEFAULT 1,
    active_tariff         smallint,
    import_t1_kwh         double precision,
    import_t2_kwh         double precision,
    export_t1_kwh         double precision,
    export_t2_kwh         double precision,
    power_w               double precision,
    power_l1_w            double precision,
    power_l2_w            double precision,
    power_l3_w            double precision,
    voltage_l1_v          double precision,
    voltage_l2_v          double precision,
    voltage_l3_v          double precision,
    current_l1_a          double precision,
    current_l2_a          double precision,
    current_l3_a          double precision,
    sag_l1_count          integer,
    sag_l2_count          integer,
    sag_l3_count          integer,
    swell_l1_count        integer,
    swell_l2_count        integer,
    swell_l3_count        integer,
    power_fail_count      integer,
    long_power_fail_count integer,
    gas_m3                double precision,
    gas_ts                timestamptz
);
SELECT create_hypertable('reading_raw', 'ts', chunk_time_interval => INTERVAL '1 day');
CREATE UNIQUE INDEX reading_raw_ts_uq ON reading_raw (ts);

ALTER TABLE reading_raw SET (timescaledb.compress, timescaledb.compress_orderby = 'ts');
SELECT add_compression_policy('reading_raw', INTERVAL '2 days');
SELECT add_retention_policy('reading_raw', INTERVAL '30 days');

-- 1 minute aggregate (retention 2 years) -------------------------------------
CREATE MATERIALIZED VIEW reading_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    last(epoch, ts)                      AS epoch,
    avg(power_w)                         AS power_avg_w,
    min(power_w)                         AS power_min_w,
    max(power_w)                         AS power_max_w,
    avg(power_l1_w)                      AS power_l1_avg_w,
    avg(power_l2_w)                      AS power_l2_avg_w,
    avg(power_l3_w)                      AS power_l3_avg_w,
    min(least(voltage_l1_v, voltage_l2_v, voltage_l3_v))       AS voltage_min_v,
    max(greatest(voltage_l1_v, voltage_l2_v, voltage_l3_v))    AS voltage_max_v,
    avg(voltage_l1_v)                    AS voltage_l1_avg_v,
    avg(voltage_l2_v)                    AS voltage_l2_avg_v,
    avg(voltage_l3_v)                    AS voltage_l3_avg_v,
    last(import_t1_kwh, ts)              AS import_t1_kwh,
    last(import_t2_kwh, ts)              AS import_t2_kwh,
    last(export_t1_kwh, ts)              AS export_t1_kwh,
    last(export_t2_kwh, ts)              AS export_t2_kwh,
    last(gas_m3, ts)                     AS gas_m3,
    count(*)                             AS samples
FROM reading_raw
GROUP BY 1
WITH NO DATA;

SELECT add_continuous_aggregate_policy('reading_1m',
    start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');
SELECT add_retention_policy('reading_1m', INTERVAL '2 years');

-- 15 minute counter snapshots (unlimited) -------------------------------------
-- Counter value at the end of each 15 min bucket. Deltas are computed in the
-- energy_* views with lag(), so energy during a data gap is attributed to the
-- first bucket after the gap instead of being lost or interpolated as zero.
CREATE MATERIALIZED VIEW counter_15m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '15 minutes', ts) AS bucket,
    last(epoch, ts)                        AS epoch,
    last(import_t1_kwh, ts)                AS import_t1_kwh,
    last(import_t2_kwh, ts)                AS import_t2_kwh,
    last(export_t1_kwh, ts)                AS export_t1_kwh,
    last(export_t2_kwh, ts)                AS export_t2_kwh,
    last(gas_m3, ts)                       AS gas_m3,
    avg(power_w)                           AS power_avg_w,
    count(*)                               AS samples
FROM reading_raw
GROUP BY 1
WITH NO DATA;

SELECT add_continuous_aggregate_policy('counter_15m',
    start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '5 minutes');

-- Energy per 15 min from counter deltas (unlimited) ---------------------------
-- Deltas never cross a meter epoch (meter swap) and are never negative.
CREATE VIEW energy_15m AS
SELECT
    bucket,
    epoch,
    greatest(0, import_t1_kwh - lag(import_t1_kwh) OVER w) AS import_t1_kwh,
    greatest(0, import_t2_kwh - lag(import_t2_kwh) OVER w) AS import_t2_kwh,
    greatest(0, export_t1_kwh - lag(export_t1_kwh) OVER w) AS export_t1_kwh,
    greatest(0, export_t2_kwh - lag(export_t2_kwh) OVER w) AS export_t2_kwh,
    greatest(0, gas_m3        - lag(gas_m3)        OVER w) AS gas_m3,
    power_avg_w,
    samples
FROM counter_15m
WINDOW w AS (PARTITION BY epoch ORDER BY bucket);

CREATE VIEW energy_1h AS
SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket,
       sum(import_t1_kwh) AS import_t1_kwh, sum(import_t2_kwh) AS import_t2_kwh,
       sum(export_t1_kwh) AS export_t1_kwh, sum(export_t2_kwh) AS export_t2_kwh,
       sum(gas_m3) AS gas_m3, avg(power_avg_w) AS power_avg_w
FROM energy_15m GROUP BY 1;

-- Daily buckets follow the local (Europe/Amsterdam) calendar day, so DST days
-- are 23 or 25 hours long.
CREATE VIEW energy_1d AS
SELECT time_bucket(INTERVAL '1 day', bucket, 'Europe/Amsterdam') AS bucket,
       sum(import_t1_kwh) AS import_t1_kwh, sum(import_t2_kwh) AS import_t2_kwh,
       sum(export_t1_kwh) AS export_t1_kwh, sum(export_t2_kwh) AS export_t2_kwh,
       sum(gas_m3) AS gas_m3, avg(power_avg_w) AS power_avg_w
FROM energy_15m GROUP BY 1;

-- Events: alerts, power failures, meter offline/online, meter epochs ---------
CREATE TABLE event (
    id        bigserial PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    kind      text        NOT NULL,
    severity  text        NOT NULL DEFAULT 'info',
    message   text        NOT NULL,
    data      jsonb       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX event_ts_idx ON event (ts DESC);
CREATE INDEX event_kind_idx ON event (kind, ts DESC);

-- Meter epochs: a new epoch starts whenever a counter decreases --------------
CREATE TABLE meter_epoch (
    epoch       integer PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    reason      text
);
INSERT INTO meter_epoch (epoch, reason) VALUES (1, 'initial');

-- Key/value settings, editable from the UI later ----------------------------
CREATE TABLE setting (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Collector heartbeat (single row) -------------------------------------------
CREATE TABLE collector_status (
    id                   smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    started_at           timestamptz,
    driver               text,
    meter_host           text,
    online               boolean NOT NULL DEFAULT false,
    last_reading_at      timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0,
    last_error           text,
    buffered_readings    integer NOT NULL DEFAULT 0,
    meter_info           jsonb NOT NULL DEFAULT '{}'::jsonb
);
INSERT INTO collector_status (id) VALUES (1);

-- Backup status (written by the backup service) ------------------------------
CREATE TABLE backup_run (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    ok          boolean,
    file        text,
    size_bytes  bigint,
    message     text
);
