"""Synthetic IoT sensor simulator.

Six campus sensors each send a reading (PM2.5, PM10, CO2, temperature,
humidity) as one JSON file into the incoming directory. Every
``--episode-interval`` seconds one sensor goes through a pollution episode.

    python producer.py --interval 1.0                # continuous readings
    python producer.py --episode pollution_spike     # one episode now, then exit
"""

import argparse
import json
import random
import time
import uuid
from datetime import datetime, timezone

from src.config import INCOMING_DIR

# sensor_id: (location, indoor?)
SENSORS = {
    "S01": ("Central Library", True),
    "S02": ("Academic Block 5 Classroom", True),
    "S03": ("Food Court", False),
    "S04": ("Main Gate", False),
    "S05": ("Hostel Block 14", True),
    "S06": ("Sports Complex", False),
}

EPISODES = ["pollution_spike", "co2_buildup", "heatwave", "sensor_fault"]
# Which sensor each episode affects (a plausible place for it).
EPISODE_SENSOR = {
    "pollution_spike": "S04",   # traffic and construction dust at the gate
    "co2_buildup": "S02",       # a crowded classroom with closed windows
    "heatwave": "S06",          # an open sports ground in the afternoon
    "sensor_fault": "S03",      # a damaged or disconnected sensor
}


def make_reading(sensor_id, **overrides):
    location, indoor = SENSORS[sensor_id]
    pm25 = random.uniform(12, 32) if indoor else random.uniform(20, 50)
    reading = {
        "reading_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sensor_id": sensor_id,
        "location": location,
        "pm25": round(pm25, 1),
        "pm10": round(pm25 * random.uniform(1.4, 1.9), 1),
        "co2": round(random.uniform(480, 900) if indoor else random.uniform(410, 460)),
        "temperature": round(random.uniform(24, 29) if indoor else random.uniform(27, 33), 1),
        "humidity": round(random.uniform(55, 80), 1),
    }
    reading.update(overrides)
    return reading


def make_episode(kind=None):
    """Return a burst of readings from one sensor showing the episode."""
    kind = kind or random.choice(EPISODES)
    sensor = EPISODE_SENSOR[kind]
    readings = []
    for _ in range(random.randint(35, 45)):
        if kind == "pollution_spike":
            pm25 = random.uniform(220, 320)
            readings.append(make_reading(sensor, pm25=round(pm25, 1),
                                         pm10=round(pm25 * random.uniform(1.5, 1.8), 1)))
        elif kind == "co2_buildup":
            readings.append(make_reading(sensor, co2=round(random.uniform(2100, 2900))))
        elif kind == "heatwave":
            readings.append(make_reading(sensor, temperature=round(random.uniform(42.5, 45.5), 1)))
        else:
            readings.append(make_reading(sensor, humidity=random.choice([140.0, -5.0]),
                                         pm25=random.choice([-1.0, 1500.0])))
    return kind, readings


def write_reading(reading):
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.fromisoformat(reading["timestamp"].replace("Z", "+00:00"))
    filename = f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}_{reading['reading_id']}.json"
    destination = INCOMING_DIR / filename
    temporary = INCOMING_DIR / f".{filename}.tmp"
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(reading, f, ensure_ascii=False)
        f.flush()
    # Atomic rename: the consumer never sees a half-written file.
    temporary.replace(destination)


def write_episode(kind, delay):
    kind, readings = make_episode(kind)
    for reading in readings:
        write_reading(reading)
        time.sleep(delay)
    return kind, readings


def main():
    parser = argparse.ArgumentParser(description="Synthetic IoT air quality sensor simulator")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Seconds between rounds of readings (one per sensor)")
    parser.add_argument("--episode-interval", type=float, default=90.0,
                        help="Seconds between simulated episodes")
    parser.add_argument("--episode-delay", type=float, default=0.05,
                        help="Delay between readings within an episode")
    parser.add_argument("--episode", choices=EPISODES,
                        help="Write one episode of this kind and exit (for live demos)")
    args = parser.parse_args()

    if args.episode:
        kind, readings = write_episode(args.episode, args.episode_delay)
        sensor = readings[0]["sensor_id"]
        print(f"Wrote {len(readings)} readings for a '{kind}' episode at "
              f"{SENSORS[sensor][0]} ({sensor}). The alert appears on the dashboard in a few seconds.")
        return

    print(f"Sensor simulator writing to {INCOMING_DIR} (Ctrl+C to stop)")
    last_episode = time.monotonic()
    while True:
        for sensor in SENSORS:
            write_reading(make_reading(sensor))
        if time.monotonic() - last_episode >= args.episode_interval:
            kind, readings = write_episode(None, args.episode_delay)
            print(f"Episode: {kind} at {readings[0]['location']} ({len(readings)} readings)")
            last_episode = time.monotonic()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
