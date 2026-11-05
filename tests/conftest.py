import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Unit tests never need Azure credentials or a real database.
os.environ.setdefault("APP_ENV", "local")

from src import config, storage  # noqa: E402


def make_reading(reading_id, ts, sensor="S01", **overrides):
    reading = {
        "reading_id": reading_id,
        "timestamp": ts,
        "sensor_id": sensor,
        "location": "Central Library",
        "pm25": 20.0,
        "pm10": 35.0,
        "co2": 600,
        "temperature": 26.0,
        "humidity": 60.0,
    }
    reading.update(overrides)
    return reading


@pytest.fixture
def local_data(tmp_path, monkeypatch):
    """Point every local path at a temporary directory."""
    for name, value in {
        "DATA_DIR": tmp_path,
        "INCOMING_DIR": tmp_path / "incoming",
        "QUARANTINE_DIR": tmp_path / "quarantine",
        "LOCAL_ARCHIVE_DIR": tmp_path / "archive",
        "HEARTBEAT_FILE": tmp_path / "heartbeat.json",
        "READINGS_CSV": tmp_path / "readings.csv",
        "ALERTS_CSV": tmp_path / "alerts.csv",
        "BATCHES_JSONL": tmp_path / "ingestion_batches.jsonl",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "BATCH_LINGER_SECONDS", 0)
    (tmp_path / "incoming").mkdir()
    monkeypatch.setattr(storage, "_storage", None)
    return tmp_path
