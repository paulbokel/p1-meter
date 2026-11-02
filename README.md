# P1 Energy Monitor

Self-hosted replacement for the HomeWizard app + Energy+ subscription. It reads a
HomeWizard P1 meter over its **local API**, stores every reading in TimescaleDB
and serves dashboards from your own Proxmox server. No cloud, no subscription.

Design: *Functional Design – Local P1 Energy Monitor* (Claude Doc).

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 1 – Collect | Compose stack, collector (v1 driver), TimescaleDB schema, status page, nightly backup | **this release** |
| 2 – See | Live dashboard, history views, CSV export, users | planned |
| 3 – Energy+ parity | Tariffs & costs, base-load analysis, anomaly alerts | planned |
| 4 – Integrate | API tokens, MQTT/HA discovery, v2 driver, power quality | planned |
| 5 – Act | Solar surplus rules, cheapest-hours planner, kiosk mode | planned |

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
* **backend** – FastAPI. Phase 1: `/` status page, `/api/status`,
  `/api/live`, OpenAPI docs at `/api/docs`.
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
4. Open `http://<container-ip>:8000/`. The status page should turn green
   within a few seconds and show live power per phase.

**Portainer:** add a stack from this Git repository (compose path
`docker-compose.yml`) and set the variables from `.env.example` in the stack's
environment section.

**HTTPS:** `docker compose --profile proxy up -d` starts Caddy on 80/443 with
its internal CA for `P1_HOSTNAME`. If you already run a reverse proxy, point
it at port 8000 instead.

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

Tests:

```sh
pip install -r collector/requirements.txt -r backend/requirements.txt pytest
(cd collector && pytest -q)
# schema tests need a throwaway TimescaleDB; the database is wiped
(cd backend && TEST_DATABASE_URL=postgresql://p1:pw@localhost:5432/p1test pytest -q)
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | — (required) | Database password |
| `P1_HOST` | `192.168.178.187` | Meter IP or host[:port] |
| `P1_DRIVER` | `v1` | `v1` (REST). `v2` follows in phase 4 |
| `P1_POLL_INTERVAL` | `1` | Seconds between polls (min 0.5) |
| `BACKEND_PORT` | `8000` | Port the web app listens on |
| `P1_HOSTNAME` | `localhost` | Hostname for Caddy (profile `proxy`) |
| `BACKUP_HOUR` / `BACKUP_KEEP` / `BACKUP_DIR` | `3` / `14` / `./backups` | Nightly backup |
