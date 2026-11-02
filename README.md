# P1 Energy Monitor

Self-hosted replacement for the HomeWizard app + Energy+ subscription. It reads a
HomeWizard P1 meter over its **local API**, stores every reading in TimescaleDB
and serves dashboards from your own Proxmox server. No cloud, no subscription.

Design: *Functional Design – Local P1 Energy Monitor* (Claude Doc).

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 – Collect | Compose stack, collector (v1 driver), TimescaleDB schema, status page, nightly backup | done |
| 2 – See | Live dashboard, history views, CSV export, users | done |
| 3 – Energy+ parity | Tariffs & costs, base-load analysis, anomaly alerts | done |
| 4 – Integrate | API tokens, Prometheus, MQTT/HA discovery, v2 driver, power quality | done |
| 5 – Act | Automations (solar surplus, price, cheapest hours), planner, kiosk and e-paper display | **this release** |

## Architecture

```
P1 meter ──HTTP 1 s──▶ collector ──▶ TimescaleDB ◀── backend (FastAPI) ◀── browser
 192.168.178.187                     │                     ▲
                                     └── backup (pg_dump)  └── caddy (optional HTTPS)
```

* **collector** – the only component that talks to the meter. Polls
  `/api/v1/data` every second (2 s timeout), buffers up to 6 h of readings in
  memory while the DB is unavailable, marks the meter offline after 10 failed
  polls, logs power failures and starts a new *meter epoch* when a counter
  goes backwards (meter swap), so deltas never go negative.
* **db** – PostgreSQL 16 + TimescaleDB.
  * `reading_raw` – every 1 s reading, compressed after 2 days, kept 30 days.
  * `reading_1m` – 1-minute aggregate (avg/min/max power, voltages, counters), kept 2 years.
  * `counter_15m` – counter snapshot per 15 min, kept forever.
  * `energy_15m` / `energy_1h` / `energy_1d` – energy from **counter deltas**
    (exact, never integrated power). Energy during a data gap lands in the
    first bucket after the gap. Days follow Europe/Amsterdam (23/25 h on DST days).
  * `event`, `meter_epoch`, `collector_status`, `backup_run`, `setting`.
* **backend** – FastAPI. Serves the web app and the API (OpenAPI docs at
  `/api/docs`): login and users, `/api/live` + `/api/live/stream` (SSE, 1 s),
  `/api/today`, `/api/history`, `/api/history/totals`, `/api/power`,
  `/api/export.csv`, `/api/status`.
* **web app** (`frontend/`) – React + Vite + ECharts, installable PWA, light/dark,
  Dutch/English. Pages: *Live* (net power, per-phase W/V/A, today, rolling
  hour), *History* (day/week/month/year/all, T1/T2 split, gas, comparison with
  the previous period and the same period last year, power detail with zoom
  down to 1 s, CSV download), *Status* and *Settings* (language, password,
  users with roles admin/viewer).
* **worker** – background jobs (same image, `python -m app.worker`): alert
  rules every minute, base load per finished day (with backfill), and
  day-ahead prices from EnergyZero every hour when a dynamic contract exists
  (or `FETCH_PRICES=true`). Job health is at `/api/jobs`.
* **migrate** – one-shot service that applies `db/migrations/*.sql` in order
  before the collector and backend start.
* **backup** – nightly `pg_dump -Fc` at `BACKUP_HOUR`, keeps `BACKUP_KEEP` dumps.

## Before you start

1. In the HomeWizard app: *Settings → Meters → P1 meter → Local API* → on.
   The HomeWizard app keeps working in parallel.
2. Give the P1 meter a DHCP reservation for **192.168.178.187** in your router.
3. Check the API from any machine on the LAN:
   `curl http://192.168.178.187/api/v1/data`
   (and `curl http://192.168.178.187/api` for the firmware version; v2 needs ≥ 6.00).

## Deploy on Proxmox

1. Create an LXC: Debian 12, unprivileged, **nesting=1** (needed for Docker),
   1 vCPU, 1 GB RAM, 10 GB disk, static IP. Install Docker:
   ```sh
   apt update && apt install -y curl git && curl -fsSL https://get.docker.com | sh
   ```
2. Get the code and configure it:
   ```sh
   git clone https://github.com/paulbokel/p1-meter.git /opt/p1-meter && cd /opt/p1-meter
   cp .env.example .env && nano .env      # set POSTGRES_PASSWORD at least
   ```
3. Start it:
   ```sh
   docker compose up -d --build
   ```
4. Open `http://<container-ip>:8000/`. The first visit asks you to create the
   admin account. After that, *Status* should turn green within a few seconds
   and *Live* shows power per phase. Add household members under *Settings*.

**Portainer:** add a stack from this Git repository (compose path
`docker-compose.yml`) and set the variables from `.env.example` in the stack's
environment section.

**HTTPS:** `docker compose --profile proxy up -d` starts Caddy on 80/443 with
its internal CA for `P1_HOSTNAME`. If you already run a reverse proxy, point
it at port 8000 instead.

## Costs, base load and alerts (Energy+ parity)

* **Contracts** (*Settings → Energy contracts*): fixed, day/night or dynamic,
  each with a validity period, so history keeps the right prices after a
  supplier switch. Per contract: import price(s), feed-in compensation,
  netting (*salderen*), gas price, fixed monthly costs, energy-tax rebate
  and the monthly advance payment. For a dynamic contract the prices are the
  surcharge on top of the day-ahead price (which gets VAT added).
* **Costs** appear on *Live* (today) and *History* (per period, with a cost
  chart). Fixed and day/night contracts are priced per day; dynamic days per
  15 minutes. With netting, export is credited at the import price up to the
  imported kWh in the period; the excess gets the feed-in price.
* **Insights**: base load (5th percentile of 1-minute import power per day,
  night weighted, export minutes ignored) in W, kWh/year, €/year and as a
  share of grid import, with a trend chart that marks step changes; the
  projected year total against your advance payments; and with dynamic
  prices the cheapest upcoming block for flexible loads.
* **Alerts** (*Alerts* page): unusual hour/day consumption (vs. the same
  weekday/hour over the last 8 weeks), base load increase, power above X W for
  Y min, daily kWh above Z, gas use while away, meter offline, power failure,
  voltage outside 207–253 V. Each rule has quiet hours, snooze and a
  repeat window. Channels you can pick (one or more): web push (needs HTTPS),
  email (SMTP), ntfy, Telegram, Home Assistant notify and a generic webhook;
  each has a *Test* button.

## Integrations and power quality

* **Meter settings** (*Settings → Meter*, admin): host, driver and poll
  interval. Values saved here override `.env`; the collector restarts itself
  within 30 s to apply them.
* **API v2 driver**: meters with firmware 6.00 or newer push readings over a
  WebSocket (`wss://<meter>/api/ws`). Click *Pair for API v2*, press the
  button on the meter within 30 s, then pick *API v2* and save. Without the
  web app: `docker compose run --rm collector python -m p1collector.pair
  <meter-ip>` prints a token for `P1_TOKEN` (with `P1_DRIVER=v2`). The
  meter's certificate is issued by HomeWizard for a serial-based name, so the
  connection is encrypted but the name is not verified; set `P1_CA_FILE` to
  the HomeWizard CA to verify the chain.
* **API tokens** (*Settings → API tokens*, admin): read-only bearer tokens
  for scripts, Grafana or Prometheus. A token only allows GET requests and
  never has admin rights; revoke it at any time. The full API is documented at
  `/api/docs`.
* **Prometheus**: `GET /metrics` (with a token) exposes power, voltage and
  current per phase, the meter counters (import/export per tariff, gas,
  sags/swells, power failures), collector health and database size.

  ```yaml
  - job_name: p1
    scrape_interval: 15s
    static_configs: [{ targets: ["p1-monitor:8000"] }]
    authorization: { type: Bearer, credentials: p1_... }
  ```
* **MQTT + Home Assistant**: set `MQTT_HOST` (and `MQTT_USER` /
  `MQTT_PASSWORD`) in `.env` and run `docker compose --profile mqtt up -d`.
  The bridge publishes the latest reading on `p1monitor/state` every
  `MQTT_INTERVAL` seconds, today's totals on `p1monitor/today`, availability
  on `p1monitor/status`, and Home Assistant discovery configs under
  `homeassistant/sensor/p1monitor/…`. The import/export/gas counters are
  `total_increasing` energy/gas sensors, so they can be added to the HA
  Energy dashboard directly.
* **Power quality** (*Power quality* page): voltage per phase with a min/max
  band (5 min for up to 3 days, hourly up to 3 months, daily beyond), a
  per-day table with min/max voltage, sags, swells and outages, and a log of
  power failures, sags/swells and meter outages. Hourly voltage data is kept
  without a time limit.

## Automations, planner and displays

* **Automations** (*Automations* page, admin): switch a device when
  * there is **solar surplus**: export of at least X W in every minute of the
    last Y minutes (off again when every minute is below X: no flapping);
  * the **price** is below X €/kWh (all-in with a dynamic contract, or spot);
  * it is one of the **cheapest hours** in a daily window, e.g. the cheapest
    3 hours between 22:00 and 07:00, as one block or as separate hours. The
    plan is made as soon as the day-ahead prices for the whole window are
    published and shown next to the automation.

  Actions: an MQTT publish (broker from `MQTT_HOST`, or a host per action), a
  Home Assistant service call (e.g. `switch.turn_on` with
  `{"entity_id": "switch.boiler"}`, using a long-lived access token) or a
  webhook. An optional off action runs when the condition ends; *keep on at
  least* stops an appliance being switched off mid-cycle. Each switch is
  logged as an event, and *Test on/off* runs an action right away. Disabling
  an automation that is on runs its off action. The worker evaluates them
  every 30 s and fetches day-ahead prices whenever a price automation exists.
* **Planner** (*Insights → Dynamic prices*): the cheapest N hours from now,
  optionally *ready by* a time, as one block or separate hours, marked on the
  price chart.
* **Kiosk mode**: open `#/kiosk` (or the ⛶ button) on a wall tablet logged in
  as a viewer: big live power, today's totals and cost, the last hour.
  Double-tap for fullscreen; it keeps the screen awake and reconnects by
  itself.
* **E-paper / custom displays**: `GET /api/display` (compact JSON) and
  `GET /api/display.png?w=800&h=480` (1-bit PNG; `&grey=true` for 8-bit grey).
  Both take an API token as a Bearer header or, for devices that can only
  fetch a URL, `?token=…` (keep such URLs on your LAN).

## Backup and restore

Dumps land in `./backups` (or `BACKUP_DIR`). Make a backup on demand:

```sh
docker compose run --rm backup now
```

Restore into a fresh stack:

```sh
docker compose up -d db
docker compose exec -T db psql -U p1 -d p1 -c "SELECT timescaledb_pre_restore();"
docker compose exec -T db pg_restore -U p1 -d p1 --clean --if-exists < backups/p1-YYYYMMDDTHHMMSSZ.dump
docker compose exec -T db psql -U p1 -d p1 -c "SELECT timescaledb_post_restore();"
docker compose up -d
```

Also include the LXC in the regular Proxmox vzdump schedule.

## Development

Run everything against a simulated meter (counters advance in real time,
solar export around midday):

```sh
cp .env.example .env    # set POSTGRES_PASSWORD, and P1_HOST=fake-meter
docker compose --profile dev up -d --build
```

Web app with hot reload (proxies `/api` to the backend on port 8000):

```sh
cd frontend && npm install && npm run dev
```

Synthetic history for trying the history views (use a separate database):

```sh
DATABASE_URL=postgresql://p1:pw@localhost:5432/p1demo python devtools/seed_history.py --days 400
```

Tests:

```sh
pip install -r collector/requirements.txt -r backend/requirements.txt pytest httpx
(cd collector && pytest -q)
# schema and API tests need a throwaway TimescaleDB; the database is wiped
(cd backend && TEST_DATABASE_URL=postgresql://p1:pw@localhost:5432/p1test pytest -q)
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | — (required) | Database password |
| `P1_HOST` | `192.168.178.187` | Meter IP or host[:port] |
| `P1_DRIVER` | `v1` | `v1` (REST) or `v2` (WebSocket, needs `P1_TOKEN`); overridden by *Settings → Meter* |
| `P1_TOKEN` | — | API v2 token from pairing |
| `P1_CA_FILE` | — | HomeWizard CA file to verify the v2 certificate chain |
| `P1_POLL_INTERVAL` | `1` | Seconds between polls (min 0.5) |
| `BACKEND_PORT` | `8000` | Port the web app listens on |
| `P1_HOSTNAME` | `localhost` | Hostname for Caddy (profile `proxy`) |
| `FETCH_PRICES` | `false` | Fetch day-ahead prices even without a dynamic contract |
| `BACKUP_HOUR` / `BACKUP_KEEP` / `BACKUP_DIR` | `3` / `14` / `./backups` | Nightly backup |
| `MQTT_HOST` / `MQTT_PORT` | — / `1883` | Broker for the `mqtt` profile |
| `MQTT_USER` / `MQTT_PASSWORD` | — | Broker login |
| `MQTT_TOPIC` / `MQTT_DISCOVERY_PREFIX` / `MQTT_INTERVAL` | `p1monitor` / `homeassistant` / `5` | Topics and publish interval (s) |
