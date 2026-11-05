"""Durable ingestion pipeline used by consumer.py.

For each poll of the incoming directory:

1. read and validate up to BATCH_SIZE reading files (invalid -> quarantine/)
2. archive the raw batch (Blob Storage in Azure)          - before any deletion
3. record the batch in storage
4. per reading, in timestamp order: skip duplicates, build the sensor's
   five-minute window, compute the AQI and apply the alert rules, persist the
   reading and alert in one transaction
5. delete an incoming file only after its reading has been persisted

If archiving or persistence fails, the remaining files stay in the incoming
directory and are retried on the next poll, so readings are never lost.
"""

import bisect
import json
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .aqi import SEVERITY_ORDER, evaluate_window
from .cloud.blob_storage import ArchiveError
from .storage import StorageError
from .stream_utils import InvalidReadingError, parse_timestamp, validate_reading

log = logging.getLogger(__name__)


def new_batch_id():
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"


class Pipeline:
    def __init__(self, storage, archiver, incoming_dir=None, quarantine_dir=None):
        self.storage = storage
        self.archiver = archiver
        self.incoming_dir = Path(incoming_dir or config.INCOMING_DIR)
        self.quarantine_dir = Path(quarantine_dir or config.QUARANTINE_DIR)
        self.window = timedelta(minutes=config.WINDOW_MINUTES)
        self.retention = self.window + timedelta(seconds=config.ALLOWED_LATENESS_SECONDS)

        # sensor_id -> sorted list of (timestamp, reading_id, reading)
        self.windows = defaultdict(list)
        self.max_seen = None
        # (sensor, window) -> highest severity already logged as an air quality alert
        self._notified = {}
        self.processed_total = 0
        self.last_batch_id = None

    # -- state -------------------------------------------------------------------
    def restore_windows(self, now=None):
        """Rebuild recent sensor windows from storage after a restart."""
        now = now or datetime.now(timezone.utc)
        readings = self.storage.recent_readings(now - self.retention)
        for reading in readings:
            self._remember(reading)
        log.info(
            "Restored %d recent readings into sensor windows", len(readings),
            extra={"operation": "restore_windows", "count": len(readings)},
        )
        return len(readings)

    def _remember(self, reading):
        ts = parse_timestamp(reading["timestamp"])
        bisect.insort(
            self.windows[reading["sensor_id"]],
            (ts, str(reading["reading_id"]), reading),
            key=lambda e: (e[0], e[1]),
        )
        if self.max_seen is None or ts > self.max_seen:
            self.max_seen = ts
        self._prune()

    def _prune(self):
        cutoff = self.max_seen - self.retention
        for sensor in list(self.windows):
            entries = self.windows[sensor]
            keep = bisect.bisect_left(entries, cutoff, key=lambda e: e[0])
            if keep:
                del entries[:keep]
            if not entries:
                del self.windows[sensor]

    def window_readings(self, reading):
        ts = parse_timestamp(reading["timestamp"])
        cutoff = ts - self.window
        readings = [
            r for t, _, r in self.windows.get(reading["sensor_id"], [])
            if cutoff <= t <= ts and str(r["reading_id"]) != str(reading["reading_id"])
        ]
        return readings + [reading]

    # -- processing --------------------------------------------------------------
    def process_reading(self, reading, batch_id=None):
        record = evaluate_window(reading, self.window_readings(reading))

        # Raises StorageError; in-memory state is only updated after success.
        self.storage.save_result(record, batch_id)

        self._remember(reading)
        self.processed_total += 1
        self._log_result(record, batch_id)
        return record

    def _log_result(self, record, batch_id):
        level = logging.INFO if record["alert"] else logging.DEBUG
        log.log(
            level,
            "%s | %s | AQI=%s (%s) | %s | %s",
            "ALERT" if record["alert"] else "OK", record["sensor_id"],
            record["aqi"], record["aqi_category"], record["severity"], record["reason"],
            extra={
                "operation": "reading_processed", "reading_id": record["reading_id"],
                "batch_id": batch_id, "sensor_id": record["sensor_id"],
                "location": record.get("location"), "severity": record["severity"],
                "aqi": record["aqi"], "aqi_category": record["aqi_category"],
            },
        )

        # One notification-worthy log line per window each time it escalates
        # to HIGH/CRITICAL.
        if record["alert"] and SEVERITY_ORDER[record["severity"]] >= SEVERITY_ORDER["HIGH"]:
            key = (record["sensor_id"], record["window"])
            previous = self._notified.get(key, "NONE")
            if SEVERITY_ORDER[record["severity"]] > SEVERITY_ORDER[previous]:
                self._notified[key] = record["severity"]
                log.warning(
                    "Air quality alert: %s at %s (%s)",
                    record["alert_types"], record.get("location", record["sensor_id"]),
                    record["severity"],
                    extra={
                        "operation": "air_quality_alert", "reading_id": record["reading_id"],
                        "sensor_id": record["sensor_id"], "location": record.get("location"),
                        "severity": record["severity"], "aqi": record["aqi"],
                    },
                )
            if len(self._notified) > 10_000:
                self._notified.clear()

    def _quarantine(self, path, reason):
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = self.quarantine_dir / path.name
        shutil.move(str(path), target)
        target.with_suffix(target.suffix + ".error.txt").write_text(reason, encoding="utf-8")
        log.error(
            "Quarantined %s: %s", path.name, reason,
            extra={"operation": "quarantine", "status": "invalid"},
        )

    def poll_once(self):
        """Process one batch. Returns the number of files handled.

        Raises ArchiveError/StorageError when the batch must be retried later.
        """
        files = sorted(self.incoming_dir.glob("*.json"))[: config.BATCH_SIZE]
        if not files:
            return 0
        if len(files) < config.BATCH_SIZE and config.BATCH_LINGER_SECONDS > 0:
            try:
                oldest = min(path.stat().st_mtime for path in files)
            except FileNotFoundError:
                return 0
            if time.time() - oldest < config.BATCH_LINGER_SECONDS:
                return 0  # let the batch fill up a little longer

        raw, valid = [], []
        for path in files:
            try:
                original = json.loads(path.read_text(encoding="utf-8"))
                valid.append((path, validate_reading(original)))
                raw.append(original)
            except (json.JSONDecodeError, UnicodeDecodeError, InvalidReadingError) as exc:
                self._quarantine(path, f"{exc.__class__.__name__}: {exc}")
            except FileNotFoundError:
                continue

        if not valid:
            return len(files)

        batch_id = new_batch_id()
        # Archive the untouched originals before anything can delete them.
        blob_uri = self.archiver.archive_batch(batch_id, raw)
        self.storage.record_batch(batch_id, blob_uri, len(valid), "archived")
        self.last_batch_id = batch_id

        already = self.storage.existing_ids([r["reading_id"] for _, r in valid])
        valid.sort(key=lambda item: (parse_timestamp(item[1]["timestamp"]), item[0].name))

        handled = len(files) - len(valid)
        duplicates = 0
        seen = set()
        try:
            for path, reading in valid:
                reading_id = reading["reading_id"]
                if reading_id in already or reading_id in seen:
                    duplicates += 1
                    log.info(
                        "Skipping duplicate reading", extra={
                            "operation": "duplicate_reading", "reading_id": reading_id,
                            "batch_id": batch_id,
                        },
                    )
                else:
                    try:
                        self.process_reading(reading, batch_id)
                    except StorageError:
                        raise
                    except Exception as exc:  # a poison reading must not block the queue
                        log.exception(
                            "Reading processing failed", extra={
                                "operation": "reading_failed", "reading_id": reading_id,
                                "batch_id": batch_id,
                            },
                        )
                        self._quarantine(path, f"processing error: {exc!r}")
                        handled += 1
                        continue
                seen.add(reading_id)
                path.unlink(missing_ok=True)
                handled += 1
        except StorageError as exc:
            try:
                self.storage.record_batch(batch_id, blob_uri, len(valid), "partial", str(exc)[:500])
            except StorageError:
                pass
            raise

        self.storage.record_batch(batch_id, blob_uri, len(valid), "processed")
        log.info(
            "Batch processed", extra={
                "operation": "batch_processed", "batch_id": batch_id,
                "count": len(valid) - duplicates, "blob_uri": blob_uri, "status": "processed",
            },
        )
        return handled

    # -- liveness ----------------------------------------------------------------
    def write_heartbeat(self, path=None, status="running", error=None):
        path = Path(path or config.HEARTBEAT_FILE)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "status": status,
            "processed_total": self.processed_total,
            "last_batch_id": self.last_batch_id,
            "error": error,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)


__all__ = ["Pipeline", "ArchiveError", "StorageError", "new_batch_id"]
