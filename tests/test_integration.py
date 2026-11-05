"""Integration tests against real PostgreSQL and Azurite (the Blob emulator).

Run with `docker compose up -d` and:
    TEST_DATABASE_URL=postgresql://airq:localdev@127.0.0.1:55433/airquality
    TEST_AZURITE_CONNECTION_STRING=<Azurite connection string, see .env.example>
They are skipped when these variables are absent. CI provides both services.
"""

import json
import os
import uuid

import pytest

from src import config
from src.aqi import evaluate_window
from src.pipeline import Pipeline
from conftest import make_reading

DB_URL = os.getenv("TEST_DATABASE_URL")
AZURITE = os.getenv("TEST_AZURITE_CONNECTION_STRING")

needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
needs_cloud = pytest.mark.skipif(not (DB_URL and AZURITE), reason="PostgreSQL/Azurite not configured")


@pytest.fixture
def pg(monkeypatch, local_data):
    from src.cloud.postgres_storage import PostgresStorage

    monkeypatch.setattr(config, "STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", DB_URL)
    store = PostgresStorage()
    store._run("reset", lambda c: c.execute(
        "DROP TABLE IF EXISTS alerts, sensor_readings, ingestion_batches, schema_migrations CASCADE"
    ))
    assert store.init_schema() == ["001_initial_schema"]
    assert store.init_schema() == []          # idempotent
    yield store
    store.close()


@pytest.fixture
def blob(monkeypatch):
    from src.cloud.blob_storage import BlobArchiver

    monkeypatch.setattr(config, "ARCHIVE_BACKEND", "azure_blob")
    monkeypatch.setattr(config, "AZURE_STORAGE_CONNECTION_STRING", AZURITE)
    monkeypatch.setattr(config, "AZURE_STORAGE_CONTAINER", f"test-{uuid.uuid4().hex[:12]}")
    archiver = BlobArchiver()
    archiver.container.create_container()
    yield archiver
    archiver.container.delete_container()


def result(reading_id, ts, **kw):
    reading = make_reading(reading_id, ts, **kw)
    return evaluate_window(reading, [reading])


@needs_db
def test_postgres_save_upsert_and_stats(pg):
    pg.record_batch("b1", "memory://b1", 3, "archived")
    assert pg.save_result(result("r1", "2026-11-04T10:00:00+00:00"), "b1")
    assert pg.save_result(result("r2", "2026-11-04T10:01:00+00:00", co2=1200), "b1")
    assert pg.save_result(result("r3", "2026-11-04T10:02:00+00:00", co2=2500), "b1")
    assert not pg.save_result(result("r3", "2026-11-04T10:02:00+00:00", co2=2500), "b1")

    assert pg.existing_ids(["r1", "zzz"]) == {"r1"}
    alerts = pg.load_alerts()
    assert len(alerts) == 1
    assert alerts.iloc[0]["severity"] == "HIGH"
    assert alerts.iloc[0]["evaluation_count"] == 2
    assert len(pg.load_readings()) == 3
    stats = pg.stats()
    assert stats["readings_total"] == 3 and stats["by_severity"]["HIGH"] == 1
    assert pg.health()["ok"]
    assert [r["reading_id"] for r in pg.recent_readings("2026-11-04T10:01:30+00:00")] == ["r3"]


@needs_cloud
def test_end_to_end_blob_and_postgres(pg, blob, local_data):
    pipe = Pipeline(pg, blob)
    for i in range(5):
        reading = make_reading(f"e{i}", f"2026-11-04T10:00:0{i}+00:00", sensor="S04", pm25=300, pm10=450)
        (local_data / "incoming" / f"{i}.json").write_text(json.dumps(reading), encoding="utf-8")
    assert pipe.poll_once() == 5

    blobs = list(blob.container.list_blobs())
    assert len(blobs) == 1
    lines = blob.container.download_blob(blobs[0].name).readall().decode().splitlines()
    assert len(lines) == 5
    assert pg.last_batch()["status"] == "processed"
    assert pg.load_alerts().iloc[0]["severity"] == "CRITICAL"
    assert blob.health()["ok"]
