"""Structured logging for all services.

On the Azure VM each service runs under systemd, so stdout goes to journald
(``sudo airq logs`` or ``journalctl -u airq-consumer -f``). JSON lines keep
each record machine-readable for later analysis.
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone

from . import config

# Context fields that may be passed through ``extra=`` and are worth keeping.
CONTEXT_FIELDS = (
    "operation", "reading_id", "batch_id", "sensor_id", "location", "blob_uri",
    "count", "duration_ms", "severity", "aqi", "aqi_category", "status", "error_type",
)

# Defence in depth: scrub anything that looks like a credential.
_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(password|passwd|pwd)=[^\s;&]+"), r"\1=***"),
    (re.compile(r"(?i)\b(AccountKey|SharedAccessSignature|sig)=[^\s;&]+"), r"\1=***"),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/\s@]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_.=]+"), r"\1***"),
]


def redact(text):
    text = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": config.SERVICE_NAME,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for field in CONTEXT_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record):
        base = super().format(record)
        extras = " ".join(
            f"{field}={getattr(record, field)}"
            for field in CONTEXT_FIELDS
            if hasattr(record, field)
        )
        return redact(f"{base} {extras}".rstrip())


def setup_logging(service_name=None):
    if service_name:
        config.SERVICE_NAME = service_name

    handler = logging.StreamHandler(sys.stdout)
    if config.LOG_FORMAT == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)

    # The Azure SDK logs every HTTP request at INFO; keep only problems.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("psycopg.pool").setLevel(logging.WARNING)
    return logging.getLogger(config.SERVICE_NAME)
