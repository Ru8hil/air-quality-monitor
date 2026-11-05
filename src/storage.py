"""Storage interface with a local CSV backend and a PostgreSQL backend.

The consumer and dashboard use ``get_storage()`` and never touch a specific
backend. Configuration selects the backend; in Azure mode there is no silent
fallback to CSV (see config.validate_settings).
"""

import csv
import json
import os
import tempfile
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config

# Fields of a raw sensor reading.
READING_FIELDS = (
    "reading_id", "timestamp", "sensor_id", "location",
    "pm25", "pm10", "co2", "temperature", "humidity",
)
# Fields added by the AQI evaluation on top of the raw reading.
RESULT_FIELDS = (
    "window", "window_reading_count", "pm25_avg", "pm10_avg", "aqi", "aqi_category",
    "dominant_pollutant", "alert", "severity", "alert_types", "reason", "processed_at",
    "batch_id",
)
READING_COLUMNS = READING_FIELDS + RESULT_FIELDS
ALERT_COLUMNS = (
    "sensor_id", "location", "window", "first_detected_at", "processed_at", "severity",
    "aqi", "peak_aqi", "aqi_category", "alert_types", "reason", "last_reading_id",
    "evaluation_count",
)
SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


class StorageError(RuntimeError):
    """Persistence failed; the caller must not treat the reading as processed."""


def summarise(alerts, readings_total, sensors_total):
    """Dashboard statistics computed from an alerts frame."""
    severity = alerts.get("severity", pd.Series(dtype=str)).astype(str).str.upper()
    return {
        "readings_total": int(readings_total),
        "sensors_total": int(sensors_total),
        "alerts_total": int(len(alerts)),
        "by_severity": {s: int((severity == s).sum()) for s in SEVERITIES},
    }


class StorageBackend(ABC):
    name = "abstract"

    def init_schema(self):
        """Create tables or files if needed (idempotent)."""

    @abstractmethod
    def health(self):
        """Return {'ok': bool, 'backend': str, ...}; never raises."""

    @abstractmethod
    def existing_ids(self, reading_ids):
        """Subset of ``reading_ids`` that has already been processed."""

    @abstractmethod
    def save_result(self, record, batch_id=None):
        """Persist a processed reading and, if it raised an alert, upsert the alert."""

    @abstractmethod
    def record_batch(self, batch_id, blob_uri, reading_count, status, error=None):
        """Insert or update an ingestion batch."""

    @abstractmethod
    def recent_readings(self, since):
        """Raw readings with timestamp >= since (to rebuild windows on restart)."""

    @abstractmethod
    def load_alerts(self, limit=None):
        """Alerts as a DataFrame (columns: ALERT_COLUMNS)."""

    @abstractmethod
    def load_readings(self, limit=None):
        """Most recent processed readings as a DataFrame (columns: READING_COLUMNS)."""

    @abstractmethod
    def stats(self):
        """Aggregate counts for the dashboard (see summarise)."""

    def last_batch(self):
        return None

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Local CSV backend (development and unit tests)
# ---------------------------------------------------------------------------

def _serialize(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _read_rows(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_rows(path, columns, rows):
    """Write the complete CSV to a temporary file, then replace the original."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.stem}_", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: _serialize(row.get(c)) for c in columns})
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _append_row(path, columns, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow({c: _serialize(row.get(c)) for c in columns})


def _read_frame(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    last_error = None
    for _ in range(3):
        try:
            return pd.read_csv(path)
        except (pd.errors.EmptyDataError, pd.errors.ParserError, PermissionError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise StorageError(f"Could not read {path.name}: {last_error}")


def _severity_rank(value):
    order = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    return order.get(str(value).upper(), 0)


class CsvStorage(StorageBackend):
    """Local development backend: CSV + JSONL files under DATA_DIR."""

    name = "csv"

    def __init__(self):
        self._ids = None

    def health(self):
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            writable = os.access(config.DATA_DIR, os.W_OK)
            return {"ok": writable, "backend": self.name, "detail": str(config.DATA_DIR)}
        except OSError as exc:
            return {"ok": False, "backend": self.name, "detail": str(exc)}

    def _known_ids(self):
        if self._ids is None:
            self._ids = {row["reading_id"] for row in _read_rows(config.READINGS_CSV)}
        return self._ids

    def existing_ids(self, reading_ids):
        known = self._known_ids()
        return {str(r) for r in reading_ids if str(r) in known}

    def save_result(self, record, batch_id=None):
        record = {**record, "batch_id": batch_id or record.get("batch_id")}
        if str(record["reading_id"]) in self._known_ids():
            return False
        try:
            _append_row(config.READINGS_CSV, READING_COLUMNS, record)
            if record.get("alert"):
                self._upsert_alert(record)
        except OSError as exc:
            raise StorageError(f"CSV write failed: {exc}") from exc
        self._known_ids().add(str(record["reading_id"]))
        return True

    def _upsert_alert(self, record):
        """Keep one alert per sensor and five-minute window, updating its latest state."""
        rows = _read_rows(config.ALERTS_CSV)
        key = (str(record["sensor_id"]), str(record["window"]))
        for row in rows:
            if (row["sensor_id"], row["window"]) == key:
                previous_peak = float(row.get("peak_aqi") or 0)
                row.update({
                    "processed_at": record["processed_at"],
                    "severity": max(row["severity"], record["severity"], key=_severity_rank),
                    "aqi": record["aqi"],
                    "peak_aqi": max(previous_peak, float(record["aqi"] or 0)),
                    "aqi_category": record["aqi_category"],
                    "alert_types": record["alert_types"],
                    "reason": record["reason"],
                    "last_reading_id": record["reading_id"],
                    "evaluation_count": int(row.get("evaluation_count") or 1) + 1,
                })
                break
        else:
            rows.append({
                "sensor_id": record["sensor_id"], "location": record.get("location"),
                "window": record["window"], "first_detected_at": record["processed_at"],
                "processed_at": record["processed_at"], "severity": record["severity"],
                "aqi": record["aqi"], "peak_aqi": record["aqi"],
                "aqi_category": record["aqi_category"], "alert_types": record["alert_types"],
                "reason": record["reason"], "last_reading_id": record["reading_id"],
                "evaluation_count": 1,
            })
        _write_rows(config.ALERTS_CSV, ALERT_COLUMNS, rows)

    def record_batch(self, batch_id, blob_uri, reading_count, status, error=None):
        entry = {
            "batch_id": batch_id, "blob_uri": blob_uri, "reading_count": reading_count,
            "status": status, "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with config.BATCHES_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def last_batch(self):
        path = config.BATCHES_JSONL
        if not path.exists() or path.stat().st_size == 0:
            return None
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        try:
            return json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            return None

    def recent_readings(self, since):
        readings = self.load_readings(limit=None)
        if readings.empty:
            return []
        ts = pd.to_datetime(readings["timestamp"], utc=True, errors="coerce", format="ISO8601")
        recent = readings[ts >= pd.Timestamp(since)]
        return [
            {k: v for k, v in row.items() if not (isinstance(v, float) and pd.isna(v))}
            for row in recent[list(READING_FIELDS)].to_dict("records")
        ]

    def load_alerts(self, limit=None):
        alerts = _read_frame(config.ALERTS_CSV)
        if limit and len(alerts) > limit:
            alerts = alerts.sort_values("processed_at").tail(limit)
        return alerts

    def load_readings(self, limit=None):
        readings = _read_frame(config.READINGS_CSV)
        if limit and len(readings) > limit:
            readings = readings.tail(limit)
        return readings

    def stats(self):
        readings = _read_frame(config.READINGS_CSV)
        sensors = readings["sensor_id"].nunique() if not readings.empty else 0
        return summarise(self.load_alerts(), len(readings), sensors)


_storage = None


def get_storage():
    """Return the configured backend (one instance per process)."""
    global _storage
    if _storage is None:
        if config.STORAGE_BACKEND == "postgres":
            from .cloud.postgres_storage import PostgresStorage

            _storage = PostgresStorage()
        elif config.STORAGE_BACKEND == "csv":
            _storage = CsvStorage()
        else:
            raise config.ConfigError(f"Unknown STORAGE_BACKEND {config.STORAGE_BACKEND!r}")
    return _storage
