"""Sensor reading consumer service.

Watches the incoming directory, archives raw batches, computes each sensor's
five-minute AQI and applies the alert rules, and persists readings and alerts
through the configured storage backend. See src/pipeline.py.
"""

import logging
import signal
import time

from src import config
from src.cloud.blob_storage import ArchiveError, get_archiver
from src.logging_setup import setup_logging
from src.pipeline import Pipeline
from src.storage import StorageError, get_storage

log = logging.getLogger("consumer")
_stop = False


def _request_stop(signum, _frame):
    global _stop
    _stop = True
    log.info("Received signal %s; stopping after the current batch", signum)


def build_pipeline():
    config.validate_settings()
    storage = get_storage()
    storage.init_schema()
    return Pipeline(storage, get_archiver())


def main():
    setup_logging("consumer")
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    log.info("Starting consumer", extra={"operation": "startup", "status": str(config.describe())})
    try:
        pipeline = build_pipeline()
    except (config.ConfigError, StorageError) as exc:
        log.critical("Consumer cannot start: %s", exc, extra={"operation": "startup"})
        raise SystemExit(1)

    while not _stop:
        try:
            pipeline.restore_windows()
            break
        except StorageError as exc:
            log.error("Could not restore window state, retrying: %s", exc)
            time.sleep(config.RETRY_BACKOFF_SECONDS)

    log.info("Consumer watching %s", config.INCOMING_DIR, extra={"operation": "startup"})
    backoff = config.RETRY_BACKOFF_SECONDS
    last_heartbeat = 0.0

    while not _stop:
        try:
            handled = pipeline.poll_once()
            backoff = config.RETRY_BACKOFF_SECONDS
            if time.monotonic() - last_heartbeat >= 5:
                pipeline.write_heartbeat()
                last_heartbeat = time.monotonic()
            if not handled:
                time.sleep(config.POLL_INTERVAL_SECONDS)
        except (ArchiveError, StorageError) as exc:
            # Files stay in the incoming directory; nothing is lost.
            log.error(
                "Batch will be retried in %.0fs: %s", backoff, exc,
                extra={"operation": "batch_retry", "error_type": exc.__class__.__name__},
            )
            pipeline.write_heartbeat(status="degraded", error=str(exc)[:300])
            last_heartbeat = time.monotonic()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as exc:
            log.exception("Unexpected consumer error", extra={"operation": "consumer_loop"})
            pipeline.write_heartbeat(status="degraded", error=repr(exc)[:300])
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    pipeline.write_heartbeat(status="stopped")
    get_storage().close()
    log.info("Consumer stopped", extra={"operation": "shutdown"})


if __name__ == "__main__":
    main()
