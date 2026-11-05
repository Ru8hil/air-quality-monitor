import pytest

from src.aqi import category, evaluate_window, faults, rolling_average, sub_index
from conftest import make_reading

TS = "2026-11-04T10:02:00+00:00"


@pytest.mark.parametrize("pollutant,conc,expected", [
    ("pm25", 0, 0),
    ("pm25", 30, 50),
    ("pm25", 45, 75),          # halfway through Satisfactory
    ("pm25", 90, 200),
    ("pm25", 185, 350),        # halfway through Very Poor
    ("pm25", 999, 500),        # capped
    ("pm10", 100, 100),
    ("pm10", 300, 250),
])
def test_sub_index_follows_cpcb_breakpoints(pollutant, conc, expected):
    assert sub_index(pollutant, conc) == expected


@pytest.mark.parametrize("aqi,name,severity", [
    (40, "Good", None), (100, "Satisfactory", None), (150, "Moderate", None),
    (250, "Poor", "MEDIUM"), (350, "Very Poor", "HIGH"), (480, "Severe", "CRITICAL"),
    (None, "Unknown", None),
])
def test_category_and_severity(aqi, name, severity):
    assert category(aqi) == (name, severity)


def test_normal_reading_raises_no_alert():
    reading = make_reading("r1", TS)
    record = evaluate_window(reading, [reading])
    # PM2.5 20 -> sub-index 33, PM10 35 -> 35; the AQI is the larger one
    assert record["aqi"] == 35
    assert record["dominant_pollutant"] == "PM10"
    assert record["aqi_category"] == "Good"
    assert record["alert"] is False
    assert record["severity"] == "NONE"
    assert record["window"] == "2026-11-04T10:00:00+00:00"


def test_aqi_uses_the_rolling_average_not_one_noisy_reading():
    window = [make_reading(f"r{i}", TS, pm25=20, pm10=30) for i in range(9)]
    spike = make_reading("spike", TS, pm25=300, pm10=30)
    record = evaluate_window(spike, window + [spike])
    assert record["pm25_avg"] == 48.0
    assert record["alert"] is False      # average is still Satisfactory


def test_sustained_pollution_is_severe_and_critical():
    window = [make_reading(f"r{i}", TS, pm25=300, pm10=300) for i in range(5)]
    record = evaluate_window(window[-1], window)
    assert record["aqi_category"] == "Severe"
    assert record["severity"] == "CRITICAL"
    assert record["dominant_pollutant"] == "PM2.5"
    assert "Severe air quality" in record["alert_types"]


def test_co2_and_temperature_rules():
    hot = make_reading("r1", TS, co2=2500, temperature=43)
    record = evaluate_window(hot, [hot])
    assert set(record["alert_types"].split("; ")) == {"CO2 build-up", "Heat stress"}
    assert record["severity"] == "HIGH"

    stuffy = make_reading("r2", TS, co2=1200, temperature=39)
    record = evaluate_window(stuffy, [stuffy])
    assert set(record["alert_types"].split("; ")) == {"Poor ventilation", "High temperature"}
    assert record["severity"] == "MEDIUM"


def test_sensor_fault_is_flagged_and_bad_values_are_ignored():
    broken = make_reading("r1", TS, humidity=140, pm25=-1)
    assert faults(broken) == ["pm25", "humidity"]
    record = evaluate_window(broken, [broken])
    assert "Sensor fault" in record["alert_types"]
    assert record["pm25_avg"] is None            # the invalid value is not averaged
    assert record["aqi"] == sub_index("pm10", 35)


def test_missing_values_count_as_fault():
    reading = make_reading("r1", TS)
    del reading["co2"]
    assert faults(reading) == ["co2"]
    assert rolling_average([reading], "co2") is None
