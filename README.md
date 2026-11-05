# Cloud-Based Smart Campus Air Quality Monitoring and Alert System

Cloud Computing and DevOps Lab mini-project (School of Computer Engineering, 2026-27).

Simulated IoT sensors at six campus locations send PM2.5, PM10, CO₂, temperature
and humidity readings. Every raw batch is archived in **Azure Blob Storage**. Each
sensor's five-minute rolling average is turned into an **Indian National Air Quality
Index (AQI)** value, alert rules assign a severity, and readings and alerts are stored
in **Azure Database for PostgreSQL**. A Streamlit dashboard shows them live.
Everything runs on an **Azure Linux VM** and is tested and deployed automatically by
**GitHub Actions**.

```
simulated sensors ─► incoming/ ─► consumer ─┬─► Blob Storage (raw JSONL archive)
   (producer.py)                            ├─► 5-min average ─► AQI + alert rules
                                            └─► PostgreSQL (readings, alerts, batches) ─► dashboard
git push ─► GitHub Actions CI (tests) ─► Deploy (SSH to VM, install, health check, rollback)
```

> **Setting this up for the first time?** Don't start here. Follow the guides in the
> folder above this one, in order: `1_AZURE_SETUP_GUIDE`, `2_GITHUB_AND_CICD_GUIDE`,
> `3_PRESENTATION_GUIDE`.

## How an alert is raised

| Rule | Severity |
|---|---|
| AQI category *Poor* (201–300) | MEDIUM |
| AQI category *Very Poor* (301–400) | HIGH |
| AQI category *Severe* (401–500) | CRITICAL |
| CO₂ ≥ 1000 ppm (poor ventilation) | LOW |
| CO₂ ≥ 2000 ppm (CO₂ build-up) | HIGH |
| Temperature ≥ 38 °C / ≥ 42 °C (high temperature / heat stress) | MEDIUM / HIGH |
| A value missing or physically impossible (sensor fault) | MEDIUM |

The AQI is the larger of the PM2.5 and PM10 sub-indices, each found by linear
interpolation between the CPCB breakpoints (`src/aqi.py`). CPCB defines the index on
24-hour averages. This project uses a 5-minute rolling average so the demo reacts
within minutes, and the dashboard says so. There is one alert per sensor per
five-minute window. Later readings in the same window update it.

## Quick start (local, no Azure needed)

Requires Python 3.12 (3.10–3.13 work).

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q                                     # unit tests
```

Then run each of these in its own terminal:

```bash
python consumer.py                       # archive -> AQI -> store
python producer.py                       # simulated sensors (an episode every 90 s)
streamlit run app.py                     # http://localhost:8501
python producer.py --episode pollution_spike   # also: co2_buildup, heatwave, sensor_fault
```

Local mode stores data in CSV files under `data/` and archives raw batches to
`data/archive/` with the same folder layout as Blob Storage.

Optional cloud emulation (the same code paths as Azure): `docker compose up -d`
starts PostgreSQL and Azurite (the Blob emulator). Uncomment block 2 of `.env.example`
in a copy named `.env`, then run `python manage.py init-db && python manage.py init-archive`.

## Deployment (summary)

1. Create in the Azure Portal: a resource group, a storage account with a
   `raw-readings` container, a PostgreSQL Flexible Server, and an Ubuntu 24.04 VM
   with ports 22 and 80 open.
2. Add 7 repository secrets in GitHub: `VM_HOST`, `VM_USERNAME`, `VM_PASSWORD`,
   `PGHOST`, `PGUSER`, `PGPASSWORD` and `AZURE_STORAGE_CONNECTION_STRING`.
3. Push to `main`. **CI** runs the tests against PostgreSQL and Azurite containers.
   **Deploy** then runs only if CI passed. It copies the tested commit to the VM over
   SSH, writes `/etc/airq/airq.env` from the secrets, installs a fresh virtualenv,
   creates the database and container if needed, restarts the services, and runs a
   health check. If the health check fails, it rolls back to the previous release.
4. Open `http://<VM public IP>`. On the VM, `sudo airq traffic on` starts the simulated sensors.

## Operating on the VM

```bash
sudo airq status                  # services + full health check
sudo airq traffic on | off        # simulated sensors
sudo airq episode pollution_spike # one episode now (co2_buildup, heatwave, sensor_fault)
sudo airq stats                   # totals in PostgreSQL
sudo airq alerts                  # latest alerts
sudo airq blobs                   # newest raw files in Blob Storage
sudo airq logs                    # live consumer log (Ctrl+C to stop)
sudo airq rollback                # previous release
```

Layout on the VM: code in `/opt/airq/releases/<id>`, the `/opt/airq/current`
symlink, config in `/etc/airq/airq.env` (readable only by root and the service
user), data in `/var/lib/airq`, and the service user `airq` (no login shell).

## Repository layout

```
app.py              Streamlit dashboard
consumer.py         consumer service (loop, retries, graceful shutdown, heartbeat)
producer.py         simulated IoT sensors and pollution episodes
manage.py           init-db | init-archive | health
src/config.py       environment-based settings + validation
src/aqi.py          CPCB AQI sub-indices, categories, alert rules
src/pipeline.py     archive -> dedupe -> window -> evaluate -> persist -> delete
src/storage.py      storage interface + local CSV backend
src/cloud/          blob_storage.py, postgres_storage.py, migrations/
tests/              unit + integration tests (41)
deploy/             VM install, health check, rollback, systemd units, airq helper
.github/workflows/  ci.yml, deploy.yml
```

This uses synthetic data from simulated sensors. It is a student project, not a
certified air-quality monitoring system.
