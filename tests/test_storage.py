from src.aqi import evaluate_window
from src.storage import CsvStorage
from conftest import make_reading


def result(reading_id, ts, **kw):
    reading = make_reading(reading_id, ts, **kw)
    return evaluate_window(reading, [reading])


def test_csv_round_trip_and_alert_upsert(local_data):
    store = CsvStorage()
    assert store.health()["ok"]
    assert store.save_result(result("r1", "2026-11-04T10:00:00+00:00"), "b1")
    assert store.save_result(result("r2", "2026-11-04T10:01:00+00:00", co2=1200), "b1")
    assert store.save_result(result("r3", "2026-11-04T10:02:00+00:00", co2=2500), "b1")
    assert not store.save_result(result("r3", "2026-11-04T10:02:00+00:00", co2=2500), "b1")

    assert store.existing_ids(["r1", "zzz"]) == {"r1"}
    readings = store.load_readings()
    assert list(readings["reading_id"]) == ["r1", "r2", "r3"]

    alerts = store.load_alerts()
    assert len(alerts) == 1                    # same sensor, same 5-minute window
    assert alerts.iloc[0]["severity"] == "HIGH"  # escalated from LOW
    assert alerts.iloc[0]["evaluation_count"] == 2

    stats = store.stats()
    assert stats["readings_total"] == 3 and stats["sensors_total"] == 1
    assert stats["by_severity"]["HIGH"] == 1


def test_recent_readings_and_batches(local_data):
    store = CsvStorage()
    store.save_result(result("old", "2026-11-04T09:00:00+00:00"))
    store.save_result(result("new", "2026-11-04T10:00:00+00:00"))
    recent = store.recent_readings("2026-11-04T09:30:00+00:00")
    assert [r["reading_id"] for r in recent] == ["new"]

    store.record_batch("b1", "file:///x", 2, "archived")
    store.record_batch("b1", "file:///x", 2, "processed")
    assert store.last_batch()["status"] == "processed"
