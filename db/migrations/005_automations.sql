-- Phase 5: automations (F-AUTO-01/02). A trigger condition switches an
-- automation on or off; on each switch the matching action runs once.
--
-- trigger_kind / trigger_params:
--   surplus          {"watts": 1500, "minutes": 5}            export >= watts for every minute of the window
--   price_below      {"eur_kwh": 0.10, "basis": "all_in"}     current price below the threshold (basis spot|all_in)
--   cheapest_hours   {"hours": 3, "from": "22:00", "to": "07:00", "contiguous": false}
--                    on during the planned cheapest periods of each window
-- action / off_action: {"kind": "mqtt", "topic": "...", "payload": "ON", "retain": false}
--                      {"kind": "homeassistant", "url": "...", "token": "...", "service": "switch.turn_on",
--                       "data": {"entity_id": "switch.boiler"}}
--                      {"kind": "webhook", "url": "...", "method": "POST", "body": {...}, "secret": "..."}
CREATE TABLE automation (
    id              bigserial   PRIMARY KEY,
    name            text        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    enabled         boolean     NOT NULL DEFAULT true,
    trigger_kind    text        NOT NULL CHECK (trigger_kind IN ('surplus', 'price_below', 'cheapest_hours')),
    trigger_params  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    action          jsonb       NOT NULL,
    off_action      jsonb,
    min_on_minutes  integer     NOT NULL DEFAULT 0 CHECK (min_on_minutes >= 0),
    active          boolean     NOT NULL DEFAULT false,
    changed_at      timestamptz,
    last_result     text,
    state           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
