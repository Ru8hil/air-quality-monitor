import json

import pytest

from src.pipeline import Pipeline
from src.storage import StorageError
from conftest import make_reading
from fakes import MemoryStorage, RecordingArchiver


def write(incoming, reading, name=None):
    path = incoming / (name or f"{reading['timestamp'].replace(':', '')}_{reading['reading_id']}.json")
    path.write_text(json.dumps(reading), encoding="utf-8")
    return path


def make_pipeline(local_data, archiver=None, storage=None):
    return Pipeline(storage or MemoryStorage(), archiver or RecordingArchiver())


def test_batch_is_archived_scored_persisted_and_deleted(local_data):
    pipe = make_pipeline(local_data)
    for i in range(3):
        write(local_data / "incoming", make_reading(f"r{i}", f"2026-11-04T10:00:0{i}+00:00"))
    assert pipe.poll_once() == 3
    assert list((local_data / "incoming").glob("*.json")) == []
    assert len(pipe.storage.readings) == 3
    (batch_id, archived), = pipe.archiver.batches.items()
    assert len(archived) == 3
    assert pipe.storage.batches[batch_id]["status"] == "processed"


def test_sustained_pollution_raises_one_alert_per_window(local_data):
    pipe = make_pipeline(local_data)
    for i in range(10):
        write(local_data / "incoming",
              make_reading(f"r{i}", f"2026-11-04T10:01:{i:02d}+00:00", pm25=300, pm10=450))
    pipe.poll_once()
    assert len(pipe.storage.alerts) == 1
    alert = next(iter(pipe.storage.alerts.values()))
    assert alert["severity"] == "CRITICAL" and alert["evaluation_count"] == 10


def test_windows_are_per_sensor(local_data):
    pipe = make_pipeline(local_data)
    for i in range(5):
        write(local_data / "incoming", make_reading(f"bad{i}", f"2026-11-04T10:01:0{i}+00:00",
                                                    sensor="S04", pm25=300, pm10=450))
    write(local_data / "incoming", make_reading("clean", "2026-11-04T10:01:09+00:00", sensor="S01"))
    pipe.poll_once()
    assert pipe.storage.readings["clean"]["alert"] is False
    assert pipe.storage.readings["clean"]["window_reading_count"] == 1


def test_invalid_reading_is_quarantined(local_data):
    pipe = make_pipeline(local_data)
    (local_data / "incoming" / "broken.json").write_text("{not json", encoding="utf-8")
    write(local_data / "incoming", {"reading_id": "x", "timestamp": "2026-11-04T10:00:00Z"})  # no sensor_id
    pipe.poll_once()
    names = sorted(p.name for p in (local_data / "quarantine").iterdir())
    assert len([n for n in names if n.endswith(".error.txt")]) == 2


def test_duplicates_are_skipped(local_data):
    pipe = make_pipeline(local_data)
    reading = make_reading("same", "2026-11-04T10:00:00+00:00")
    write(local_data / "incoming", reading, "a.json")
    write(local_data / "incoming", reading, "b.json")
    pipe.poll_once()
    assert len(pipe.storage.readings) == 1
    assert list((local_data / "incoming").glob("*.json")) == []


def test_blob_outage_keeps_files_for_retry(local_data):
    pipe = make_pipeline(local_data, archiver=RecordingArchiver(fail=True))
    write(local_data / "incoming", make_reading("r1", "2026-11-04T10:00:00+00:00"))
    with pytest.raises(Exception):
        pipe.poll_once()
    assert len(list((local_data / "incoming").glob("*.json"))) == 1
    assert pipe.storage.readings == {}


def test_database_outage_keeps_unsaved_files(local_data):
    storage = MemoryStorage()
    storage.fail_saves_after = 1
    pipe = make_pipeline(local_data, storage=storage)
    for i in range(3):
        write(local_data / "incoming", make_reading(f"r{i}", f"2026-11-04T10:00:0{i}+00:00"))
    with pytest.raises(StorageError):
        pipe.poll_once()
    assert len(storage.readings) == 1
    assert len(list((local_data / "incoming").glob("*.json"))) == 2
    assert next(iter(storage.batches.values()))["status"] == "partial"


def test_restore_windows_after_restart(local_data):
    from datetime import datetime, timezone

    storage = MemoryStorage()
    now = datetime.now(timezone.utc)
    storage.readings["old"] = make_reading("old", now.isoformat(), pm25=300, pm10=450)
    pipe = make_pipeline(local_data, storage=storage)
    assert pipe.restore_windows(now) == 1
    assert len(pipe.window_readings(make_reading("new", now.isoformat()))) == 2


def test_heartbeat(local_data):
    pipe = make_pipeline(local_data)
    pipe.write_heartbeat()
    beat = json.loads((local_data / "heartbeat.json").read_text())
    assert beat["status"] == "running"
