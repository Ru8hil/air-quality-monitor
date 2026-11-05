"""Air Quality Index calculation and alert rules.

The AQI follows the Indian National Air Quality Index (CPCB): a sub-index is
computed for PM2.5 and PM10 by linear interpolation between breakpoints, and
the AQI is the larger of the two. CPCB defines the index on 24-hour averages;
this project applies it to each sensor's rolling five-minute average so that
changes show up within minutes (a documented simplification).
"""

from datetime import datetime, timezone

from . import config
from .stream_utils import window_start

MEASUREMENTS = ("pm25", "pm10", "co2", "temperature", "humidity")

# Physically plausible ranges; anything outside is treated as a sensor fault.
VALID_RANGES = {
    "pm25": (0, 1000),
    "pm10": (0, 1200),
    "co2": (300, 10000),
    "temperature": (-10, 60),
    "humidity": (0, 100),
}

# (concentration low, concentration high, index low, index high) in ug/m3.
BREAKPOINTS = {
    "pm25": [(0, 30, 0, 50), (30, 60, 50, 100), (60, 90, 100, 200),
             (90, 120, 200, 300), (120, 250, 300, 400), (250, 380, 400, 500)],
    "pm10": [(0, 50, 0, 50), (50, 100, 50, 100), (100, 250, 100, 200),
             (250, 350, 200, 300), (350, 430, 300, 400), (430, 510, 400, 500)],
}

# (upper AQI bound, category, alert severity or None)
CATEGORIES = [
    (50, "Good", None),
    (100, "Satisfactory", None),
    (200, "Moderate", None),
    (300, "Poor", "MEDIUM"),
    (400, "Very Poor", "HIGH"),
    (500, "Severe", "CRITICAL"),
]

SEVERITY_ORDER = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def sub_index(pollutant, concentration):
    """CPCB sub-index: I = I_lo + (I_hi - I_lo) / (B_hi - B_lo) * (C - B_lo)."""
    if concentration is None:
        return None
    c = max(0.0, float(concentration))
    for b_lo, b_hi, i_lo, i_hi in BREAKPOINTS[pollutant]:
        if c <= b_hi:
            return round(i_lo + (i_hi - i_lo) / (b_hi - b_lo) * (c - b_lo))
    return 500  # beyond the last breakpoint the index is capped


def category(aqi):
    if aqi is None:
        return "Unknown", None
    for upper, name, severity in CATEGORIES:
        if aqi <= upper:
            return name, severity
    return "Severe", "CRITICAL"


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def faults(reading):
    """Names of measurements that are missing or outside the valid range."""
    bad = []
    for field in MEASUREMENTS:
        value = _number(reading.get(field))
        lo, hi = VALID_RANGES[field]
        if value is None or not lo <= value <= hi:
            bad.append(field)
    return bad


def rolling_average(window, field):
    """Mean of the valid values of ``field`` in the window (None if none are valid)."""
    lo, hi = VALID_RANGES[field]
    values = [v for v in (_number(r.get(field)) for r in window) if v is not None and lo <= v <= hi]
    return round(sum(values) / len(values), 1) if values else None


def evaluate_window(reading, window):
    """Score one reading using its sensor's recent readings (including itself).

    Returns the reading plus AQI fields, alert flag, severity and the reasons.
    """
    bad = faults(reading)
    pm25_avg = rolling_average(window, "pm25")
    pm10_avg = rolling_average(window, "pm10")
    sub = {"PM2.5": sub_index("pm25", pm25_avg), "PM10": sub_index("pm10", pm10_avg)}
    known = {k: v for k, v in sub.items() if v is not None}
    aqi = max(known.values()) if known else None
    dominant = max(known, key=known.get) if known else None
    aqi_category, aqi_severity = category(aqi)

    alerts = []  # (alert type, severity, evidence)
    if aqi_severity:
        alerts.append((
            f"{aqi_category} air quality", aqi_severity,
            f"AQI {aqi} ({aqi_category}) driven by {dominant}: 5-min average "
            f"PM2.5 {pm25_avg} ug/m3, PM10 {pm10_avg} ug/m3",
        ))

    co2 = _number(reading.get("co2"))
    if "co2" not in bad and co2 >= config.CO2_HIGH_PPM:
        alerts.append(("CO2 build-up", "HIGH", f"CO2 {co2:.0f} ppm >= {config.CO2_HIGH_PPM} ppm"))
    elif "co2" not in bad and co2 >= config.CO2_WARN_PPM:
        alerts.append(("Poor ventilation", "LOW", f"CO2 {co2:.0f} ppm >= {config.CO2_WARN_PPM} ppm"))

    temp = _number(reading.get("temperature"))
    if "temperature" not in bad and temp >= config.TEMP_HIGH_C:
        alerts.append(("Heat stress", "HIGH", f"temperature {temp:.1f} C >= {config.TEMP_HIGH_C} C"))
    elif "temperature" not in bad and temp >= config.TEMP_WARN_C:
        alerts.append(("High temperature", "MEDIUM", f"temperature {temp:.1f} C >= {config.TEMP_WARN_C} C"))

    if bad:
        alerts.append(("Sensor fault", "MEDIUM", "missing or out-of-range: " + ", ".join(bad)))

    severity = max((s for _, s, _ in alerts), key=SEVERITY_ORDER.get, default="NONE")
    return {
        **reading,
        "window": window_start(reading["timestamp"], config.WINDOW_MINUTES),
        "window_reading_count": len(window),
        "pm25_avg": pm25_avg,
        "pm10_avg": pm10_avg,
        "aqi": aqi,
        "aqi_category": aqi_category,
        "dominant_pollutant": dominant,
        "alert": bool(alerts),
        "severity": severity,
        "alert_types": "; ".join(name for name, _, _ in alerts),
        "reason": "; ".join(evidence for _, _, evidence in alerts) or "within normal limits",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
