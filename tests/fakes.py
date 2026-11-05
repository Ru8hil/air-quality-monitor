"""In-memory test doubles for the storage backend and archive."""

import pandas as pd

from src.storage import StorageBackend, StorageError, summarise


class MemoryStorage(StorageBackend):
    name = "memory"

    def __init__(self):
        self.readings = {}
        self.alerts = {}
        self.batches = {}
        self.fail_saves_after = None
        self.saves = 0

    def health(self):
        return {"ok": True, "backend": self.name}

    def existing_ids(self, reading_ids):
        return {r for r in reading_ids if r in self.readings}

    def save_result(self, record, batch_id=None):
        if self.fail_saves_after is not None and self.saves >= self.fail_saves_after:
            raise StorageError("simulated database outage")
        self.saves += 1
        if record["reading_id"] in self.readings:
            return False
        self.readings[record["reading_id"]] = {**record, "batch_id": batch_id}
        if record["alert"]:
            key = (record["sensor_id"], record["window"])
            previous = self.alerts.get(key)
            count = previous["evaluation_count"] + 1 if previous else 1
            self.alerts[key] = {**record, "evaluation_count": count}
        return True

    def record_batch(self, batch_id, blob_uri, reading_count, status, error=None):
        self.batches[batch_id] = {
            "blob_uri": blob_uri, "reading_count": reading_count, "status": status, "error": error,
        }

    def recent_readings(self, since):
        return [r for r in self.readings.values() if pd.Timestamp(r["timestamp"]) >= pd.Timestamp(since)]

    def load_alerts(self, limit=None):
        return pd.DataFrame(list(self.alerts.values()))

    def load_readings(self, limit=None):
        return pd.DataFrame(list(self.readings.values()))

    def stats(self):
        sensors = {r["sensor_id"] for r in self.readings.values()}
        return summarise(self.load_alerts(), len(self.readings), len(sensors))


class RecordingArchiver:
    name = "recording"

    def __init__(self, fail=False):
        self.batches = {}
        self.fail = fail

    def archive_batch(self, batch_id, readings):
        from src.cloud.blob_storage import ArchiveError

        if self.fail:
            raise ArchiveError("simulated blob outage")
        self.batches[batch_id] = list(readings)
        return f"memory://raw/{batch_id}.jsonl"

    def health(self):
        return {"ok": not self.fail, "backend": self.name}
