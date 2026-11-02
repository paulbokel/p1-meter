-- Phase 3: tariffs & costs (F-COST), base load (F-BASE), alerts (F-ALERT).

-- Tariff contracts with validity periods (F-COST-01/02) ----------------------
-- Prices are in EUR incl. VAT. For a dynamic contract, the elec_* and gas_price
-- fields are the surcharge on top of the day-ahead price (supplier mark-up +
-- energy tax, incl. VAT) and the export field is the deduction per exported
-- kWh; the day-ahead price itself is multiplied by (1 + vat_pct/100).
CREATE TABLE tariff_contract (
    id                     bigserial PRIMARY KEY,
    name                   text    NOT NULL,
    supplier               text,
    kind                   text    NOT NULL CHECK (kind IN ('fixed', 'daynight', 'dynamic')),
    valid_from             date    NOT NULL,
    valid_to               date,                        -- exclusive; NULL = open-ended
    elec_import_t1         numeric NOT NULL DEFAULT 0,  -- EUR/kWh (fixed: used for both tariffs)
    elec_import_t2         numeric NOT NULL DEFAULT 0,
    elec_export_t1         numeric NOT NULL DEFAULT 0,  -- feed-in compensation EUR/kWh
    elec_export_t2         numeric NOT NULL DEFAULT 0,
    netting                boolean NOT NULL DEFAULT true, -- NL "salderen" (until 2027)
    gas_price              numeric NOT NULL DEFAULT 0,  -- EUR/m3
    gas_dynamic            boolean NOT NULL DEFAULT false,
    vat_pct                numeric NOT NULL DEFAULT 21,
    fixed_monthly_elec     numeric NOT NULL DEFAULT 0,  -- standing charge + network costs EUR/month
    fixed_monthly_gas      numeric NOT NULL DEFAULT 0,
    energy_tax_rebate_year numeric NOT NULL DEFAULT 0,  -- "vermindering energiebelasting" EUR/year
    advance_monthly        numeric NOT NULL DEFAULT 0,  -- "termijnbedrag" EUR/month
    created_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX tariff_contract_valid_idx ON tariff_contract (valid_from);

-- Day-ahead prices (F-COST-03), EUR per kWh or m3, excl. VAT -----------------
CREATE TABLE price (
    ts       timestamptz NOT NULL,          -- start of the price period
    kind     text        NOT NULL CHECK (kind IN ('elec', 'gas')),
    source   text        NOT NULL,
    price    double precision NOT NULL,
    duration interval    NOT NULL DEFAULT interval '1 hour',
    fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, ts)
);

-- Daily base load (F-BASE-01) -------------------------------------------------
CREATE TABLE baseload_1d (
    day         date PRIMARY KEY,           -- local (Europe/Amsterdam) calendar day
    watts       double precision,
    method      text NOT NULL,
    confidence  double precision,           -- share of minutes with data, 0..1
    samples     integer NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now()
);

-- Notification channels (F-ALERT-05) -----------------------------------------
CREATE TABLE channel (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('webpush', 'email', 'ntfy', 'telegram', 'homeassistant', 'webhook')),
    name       text NOT NULL,
    config     jsonb NOT NULL DEFAULT '{}'::jsonb,
    enabled    boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE push_subscription (
    id         bigserial PRIMARY KEY,
    user_id    bigint REFERENCES app_user (id) ON DELETE CASCADE,
    endpoint   text NOT NULL UNIQUE,
    keys       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Alert rules (F-ALERT-01..04, 06) -------------------------------------------
-- kind / params:
--   anomaly_hour, anomaly_day   {"sigma": 3, "weeks": 8, "min_kwh": 0.2}
--   baseload_increase           {"watts": 30, "days": 3}
--   power_above                 {"watts": 3000, "minutes": 10}
--   daily_kwh_above             {"kwh": 15}
--   gas_while_away              {"from": "09:00", "to": "17:00", "m3": 0.05, "weekdays": [0,1,2,3,4]}
--   meter_offline               {"minutes": 2}
--   power_failure               {}
--   voltage_out_of_range        {"min": 207, "max": 253}
CREATE TABLE alert_rule (
    id             bigserial PRIMARY KEY,
    kind           text    NOT NULL,
    name           text    NOT NULL,
    enabled        boolean NOT NULL DEFAULT true,
    params         jsonb   NOT NULL DEFAULT '{}'::jsonb,
    channel_ids    bigint[] NOT NULL DEFAULT '{}',     -- empty = all enabled channels
    quiet_start    time,                               -- local time; no notifications in [start, end)
    quiet_end      time,
    snooze_until   timestamptz,
    cooldown_min   integer NOT NULL DEFAULT 60,        -- de-duplication window
    last_fired_at  timestamptz,
    state          jsonb   NOT NULL DEFAULT '{}'::jsonb, -- evaluator bookkeeping
    created_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE event ADD COLUMN rule_id bigint REFERENCES alert_rule (id) ON DELETE SET NULL;
ALTER TABLE event ADD COLUMN notified_at timestamptz;
ALTER TABLE event ADD COLUMN notify_result text;
ALTER TABLE event ADD COLUMN acknowledged boolean NOT NULL DEFAULT false;

-- Sensible defaults: the Energy+ alerts plus meter health, all channels.
INSERT INTO alert_rule (kind, name, params, cooldown_min) VALUES
    ('anomaly_day',          'Unusual daily consumption',   '{"sigma": 3, "weeks": 8, "min_kwh": 1}',   720),
    ('anomaly_hour',         'Unusual hourly consumption',  '{"sigma": 4, "weeks": 8, "min_kwh": 0.3}', 180),
    ('baseload_increase',    'Base load increased',         '{"watts": 30, "days": 3}',                 1440),
    ('meter_offline',        'Meter offline',               '{"minutes": 2}',                           60),
    ('power_failure',        'Power failure',               '{}',                                       0),
    ('voltage_out_of_range', 'Voltage out of range',        '{"min": 207, "max": 253}',                 60);

-- Worker heartbeat / job bookkeeping ------------------------------------------
CREATE TABLE job_status (
    name        text PRIMARY KEY,
    last_run_at timestamptz,
    last_ok_at  timestamptz,
    last_error  text
);
