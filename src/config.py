"""Application configuration loaded from environment variables.

Local development works with no environment at all (CSV storage, local raw
archive). Azure deployments set APP_ENV=azure and the settings validated in
``validate_settings``. Secrets are never hard-coded here: on the VM they live in
/etc/airq/airq.env, which the GitHub Actions deployment writes from GitHub secrets.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parents[1]


def _env(name, default=None):
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name, default):
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name, default):
    value = _env(name)
    return float(value) if value is not None else default


APP_ENV = (_env("APP_ENV", "local") or "local").lower()
IS_AZURE = APP_ENV == "azure"
SERVICE_NAME = _env("SERVICE_NAME", "airq")

# --- Paths -----------------------------------------------------------------
DATA_DIR = Path(_env("DATA_DIR", str(ROOT_DIR / "data")))
INCOMING_DIR = Path(_env("INCOMING_DIR", str(DATA_DIR / "incoming")))
QUARANTINE_DIR = Path(_env("QUARANTINE_DIR", str(DATA_DIR / "quarantine")))
LOCAL_ARCHIVE_DIR = Path(_env("LOCAL_ARCHIVE_DIR", str(DATA_DIR / "archive")))
HEARTBEAT_FILE = Path(_env("HEARTBEAT_FILE", str(DATA_DIR / "consumer_heartbeat.json")))

# Local (CSV) backend files.
READINGS_CSV = DATA_DIR / "readings.csv"
ALERTS_CSV = DATA_DIR / "alerts.csv"
BATCHES_JSONL = DATA_DIR / "ingestion_batches.jsonl"

# --- Backends ----------------------------------------------------------------
# storage: "csv" (local development) or "postgres" (Azure Database for PostgreSQL)
STORAGE_BACKEND = (_env("STORAGE_BACKEND", "postgres" if IS_AZURE else "csv")).lower()
# raw archive: "local" (data/archive) or "azure_blob" (Azure Blob Storage)
ARCHIVE_BACKEND = (_env("ARCHIVE_BACKEND", "azure_blob" if IS_AZURE else "local")).lower()

# --- Air quality evaluation ----------------------------------------------------
# AQI is computed on the rolling average of each sensor's last WINDOW_MINUTES,
# so one noisy reading cannot raise an alert on its own.
WINDOW_MINUTES = _env_int("WINDOW_MINUTES", 5)
ALLOWED_LATENESS_SECONDS = _env_int("ALLOWED_LATENESS_SECONDS", 60)
CO2_WARN_PPM = _env_int("CO2_WARN_PPM", 1000)
CO2_HIGH_PPM = _env_int("CO2_HIGH_PPM", 2000)
TEMP_WARN_C = _env_float("TEMP_WARN_C", 38.0)
TEMP_HIGH_C = _env_float("TEMP_HIGH_C", 42.0)

# --- Consumer --------------------------------------------------------------
BATCH_SIZE = _env_int("BATCH_SIZE", 200)
# Wait up to this long for a batch to fill before archiving it, so a slow
# trickle of readings produces a few larger blobs instead of one per reading.
BATCH_LINGER_SECONDS = _env_float("BATCH_LINGER_SECONDS", 5.0)
POLL_INTERVAL_SECONDS = _env_float("POLL_INTERVAL_SECONDS", 0.5)
RETRY_BACKOFF_SECONDS = _env_float("RETRY_BACKOFF_SECONDS", 5.0)
HEARTBEAT_MAX_AGE_SECONDS = _env_int("HEARTBEAT_MAX_AGE_SECONDS", 90)

# --- PostgreSQL --------------------------------------------------------------
# Either DATABASE_URL (local Docker) or the PG* parts (Azure).
DATABASE_URL = _env("DATABASE_URL")
PGHOST = _env("PGHOST")
PGPORT = _env_int("PGPORT", 5432)
PGDATABASE = _env("PGDATABASE", "airquality")
PGUSER = _env("PGUSER")
PGPASSWORD = _env("PGPASSWORD")
PGSSLMODE = _env("PGSSLMODE", "require" if IS_AZURE else "prefer")
PG_POOL_MIN = _env_int("PG_POOL_MIN", 1)
PG_POOL_MAX = _env_int("PG_POOL_MAX", 5)
PG_CONNECT_TIMEOUT = _env_int("PG_CONNECT_TIMEOUT", 10)

# --- Azure Blob Storage -------------------------------------------------------
# The connection string is copied from the storage account's "Access keys" page.
AZURE_STORAGE_CONNECTION_STRING = _env("AZURE_STORAGE_CONNECTION_STRING")
AZURE_STORAGE_CONTAINER = _env("AZURE_STORAGE_CONTAINER", "raw-readings")
BLOB_PREFIX = _env("BLOB_PREFIX", "raw")
BLOB_MAX_RETRIES = _env_int("BLOB_MAX_RETRIES", 5)

# --- Dashboard / logging -----------------------------------------------------
DASHBOARD_REFRESH_SECONDS = _env_int("DASHBOARD_REFRESH_SECONDS", 5)
DASHBOARD_ALERT_LIMIT = _env_int("DASHBOARD_ALERT_LIMIT", 2000)
DASHBOARD_READING_LIMIT = _env_int("DASHBOARD_READING_LIMIT", 3000)
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = (_env("LOG_FORMAT", "json" if IS_AZURE else "text")).lower()


class ConfigError(RuntimeError):
    """Raised when the configuration is incomplete or inconsistent."""


def validate_settings():
    """Fail fast with a clear message instead of running half-configured."""
    problems = []

    if APP_ENV not in {"local", "azure"}:
        problems.append(f"APP_ENV must be 'local' or 'azure', got {APP_ENV!r}")
    if STORAGE_BACKEND not in {"csv", "postgres"}:
        problems.append(f"STORAGE_BACKEND must be 'csv' or 'postgres', got {STORAGE_BACKEND!r}")
    if ARCHIVE_BACKEND not in {"local", "azure_blob"}:
        problems.append(f"ARCHIVE_BACKEND must be 'local' or 'azure_blob', got {ARCHIVE_BACKEND!r}")

    if IS_AZURE:
        # A cloud deployment must never quietly write to local CSV files.
        if STORAGE_BACKEND != "postgres":
            problems.append("APP_ENV=azure requires STORAGE_BACKEND=postgres")
        if ARCHIVE_BACKEND != "azure_blob":
            problems.append("APP_ENV=azure requires ARCHIVE_BACKEND=azure_blob")
        if PGSSLMODE not in {"require", "verify-ca", "verify-full"}:
            problems.append("APP_ENV=azure requires PGSSLMODE=require (or stricter)")

    if STORAGE_BACKEND == "postgres":
        if not DATABASE_URL and not (PGHOST and PGUSER and PGPASSWORD):
            problems.append("PostgreSQL storage needs DATABASE_URL, or PGHOST, PGUSER and PGPASSWORD")

    if ARCHIVE_BACKEND == "azure_blob":
        if not AZURE_STORAGE_CONNECTION_STRING:
            problems.append("ARCHIVE_BACKEND=azure_blob needs AZURE_STORAGE_CONNECTION_STRING")
        if not AZURE_STORAGE_CONTAINER:
            problems.append("AZURE_STORAGE_CONTAINER must not be empty")

    if BATCH_SIZE < 1:
        problems.append("BATCH_SIZE must be at least 1")

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))


def ensure_local_dirs():
    for path in (DATA_DIR, INCOMING_DIR, QUARANTINE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def describe():
    """Non-secret summary of the active configuration, safe to log."""
    return {
        "app_env": APP_ENV,
        "storage_backend": STORAGE_BACKEND,
        "archive_backend": ARCHIVE_BACKEND,
        "incoming_dir": str(INCOMING_DIR),
        "pg_host": PGHOST or ("<DATABASE_URL>" if DATABASE_URL else None),
        "pg_database": PGDATABASE,
        "pg_sslmode": PGSSLMODE,
        "blob_connection_string_set": bool(AZURE_STORAGE_CONNECTION_STRING),
        "blob_container": AZURE_STORAGE_CONTAINER,
        "window_minutes": WINDOW_MINUTES,
    }


ensure_local_dirs()
