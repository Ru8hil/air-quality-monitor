import json
from datetime import timezone
from pathlib import Path

import pandas as pd

# Fields without which a reading cannot be windowed or de-duplicated. The
# measurement fields are optional: a missing value is reported as a sensor fault.
REQUIRED_FIELDS = ("reading_id", "timestamp", "sensor_id")


class InvalidReadingError(ValueError):
    """The reading cannot be processed and should be quarantined."""


def parse_timestamp(value):
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise InvalidReadingError(f"Invalid timestamp: {value!r}")
    ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def read_reading(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_reading(reading):
    """Check the minimum schema and normalise the timestamp to UTC ISO-8601."""
    if not isinstance(reading, dict):
        raise InvalidReadingError("Reading must be a JSON object")

    missing = [
        field for field in REQUIRED_FIELDS
        if reading.get(field) in (None, "")
    ]
    if missing:
        raise InvalidReadingError(f"Missing required field(s): {', '.join(missing)}")

    try:
        ts = parse_timestamp(reading["timestamp"])
    except (ValueError, TypeError) as exc:
        raise InvalidReadingError(f"Invalid timestamp {reading['timestamp']!r}") from exc

    normalised = dict(reading)
    normalised["reading_id"] = str(reading["reading_id"])
    normalised["sensor_id"] = str(reading["sensor_id"])
    normalised["timestamp"] = ts.isoformat()
    return normalised


def window_start(timestamp, minutes=5):
    ts = parse_timestamp(timestamp)
    minute = (ts.minute // minutes) * minutes
    return ts.replace(minute=minute, second=0, microsecond=0).isoformat()
