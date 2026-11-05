import pandas as pd
import plotly.express as px
import streamlit as st

from src import config
from src.storage import StorageError, get_storage

st.set_page_config(
    page_title="Campus Air Quality Monitor - MIT Manipal",
    page_icon="🌿",
    layout="wide",
)

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=config.DASHBOARD_REFRESH_SECONDS * 1000, key="dashboard_refresh")
except ImportError:
    st.caption("Install streamlit-autorefresh for automatic refresh.")

st.title("🌿 Smart Campus Air Quality Monitor - MIT Manipal")
st.caption(
    "Live IoT sensor readings | Indian National AQI (CPCB breakpoints, 5-minute rolling average) "
    f"| storage backend: {config.STORAGE_BACKEND} | raw archive: {config.ARCHIVE_BACKEND}"
)

# Official CPCB AQI category colours.
CATEGORY_COLORS = {
    "Good": "#00B050",
    "Satisfactory": "#92D050",
    "Moderate": "#FFD700",
    "Poor": "#FF9900",
    "Very Poor": "#FF0000",
    "Severe": "#C00000",
    "Unknown": "#A6A6A6",
}
SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]


def storage():
    # get_storage() returns one shared instance (and pool) per process.
    config.validate_settings()
    return get_storage()


def load_data():
    """Load from the configured backend.

    CSV (local): a transient read error shows the last valid data.
    PostgreSQL (cloud): an unavailable database stops the page with an error
    rather than showing stale data.
    """
    backend = storage()
    try:
        data = (
            backend.load_alerts(config.DASHBOARD_ALERT_LIMIT),
            backend.load_readings(config.DASHBOARD_READING_LIMIT),
            backend.stats(),
        )
        st.session_state["last_valid"] = data
        return data
    except StorageError as exc:
        if backend.name == "csv" and "last_valid" in st.session_state:
            st.warning(f"Temporary read error ({exc}); showing last valid data.")
            return st.session_state["last_valid"]
        st.error(
            f"**Storage backend unavailable** ({backend.name}): {exc}\n\n"
            "No data is shown to avoid displaying stale results."
        )
        st.stop()


try:
    alerts, readings, stats = load_data()
except config.ConfigError as exc:
    st.error(f"**Configuration error**\n\n```\n{exc}\n```")
    st.stop()
alerts, readings = alerts.copy(), readings.copy()

for df in (alerts, readings):
    for col in ("processed_at", "timestamp"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True, format="ISO8601")
    for col in ("aqi", "peak_aqi", "pm25", "pm10", "pm25_avg", "pm10_avg", "co2", "temperature", "humidity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "severity" in df.columns:
        df["severity"] = df["severity"].fillna("NONE").astype(str).str.upper()


# --------------------------------------------------
# SIDEBAR: SYSTEM HEALTH AND FILTERS
# --------------------------------------------------

with st.sidebar.expander("System health", expanded=False):
    health = storage().health()
    st.write(("🟢" if health.get("ok") else "🔴") + f" Database: {health.get('detail', '')}")
    try:
        batch = storage().last_batch()
    except StorageError:
        batch = None
    if batch:
        st.write(f"Last ingestion batch: `{batch.get('batch_id')}` ({batch.get('status')})")
        if batch.get("blob_uri"):
            st.caption(f"Raw archive: {batch['blob_uri']}")
    else:
        st.write("No ingestion batches recorded yet.")

st.sidebar.header("Filters")
locations = sorted(readings["location"].dropna().unique()) if "location" in readings else []
selected_locations = st.sidebar.multiselect("Location", locations, default=locations)
selected_severities = st.sidebar.multiselect("Alert severity", SEVERITIES, default=SEVERITIES)

if not readings.empty:
    readings = readings[readings["location"].isin(selected_locations)]
if not alerts.empty:
    alerts = alerts[alerts["location"].isin(selected_locations) & alerts["severity"].isin(selected_severities)]


# --------------------------------------------------
# TOP-LEVEL METRICS
# --------------------------------------------------

by_severity = stats["by_severity"]
latest = (
    readings.sort_values("timestamp").groupby("sensor_id").tail(1).sort_values("sensor_id")
    if not readings.empty else pd.DataFrame()
)
worst = latest.sort_values("aqi", ascending=False).iloc[0] if not latest.empty and latest["aqi"].notna().any() else None

cols = st.columns(5)
cols[0].metric("Readings processed", f"{stats['readings_total']:,}")
cols[1].metric("Active sensors", stats["sensors_total"])
cols[2].metric("Alert records", f"{stats['alerts_total']:,}")
cols[3].metric("Critical / High", f"{by_severity.get('CRITICAL', 0)} / {by_severity.get('HIGH', 0)}")
cols[4].metric(
    "Worst location now",
    f"AQI {int(worst['aqi'])}" if worst is not None else "-",
    worst["location"] if worst is not None else None,
    delta_color="off",
)

st.divider()

if readings.empty:
    st.info(
        "No sensor readings yet. Start the simulator: on the VM run `sudo airq traffic on`, "
        "or locally `python producer.py`."
    )
    st.stop()


# --------------------------------------------------
# CURRENT STATUS AND TRENDS
# --------------------------------------------------

left, right = st.columns(2)

with left:
    st.subheader("Current AQI by location")
    fig = px.bar(
        latest, x="aqi", y="location", orientation="h", color="aqi_category",
        color_discrete_map=CATEGORY_COLORS, text="aqi",
        labels={"aqi": "AQI (5-min average)", "location": "", "aqi_category": "Category"},
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, xaxis_range=[0, 500])
    st.plotly_chart(fig, width="stretch")

with right:
    st.subheader("Latest reading per sensor")
    st.dataframe(
        latest[["sensor_id", "location", "aqi", "aqi_category", "pm25", "pm10", "co2", "temperature", "humidity"]],
        width="stretch",
        hide_index=True,
        column_config={
            "aqi": st.column_config.ProgressColumn("AQI", format="%d", min_value=0, max_value=500),
            "pm25": "PM2.5 (µg/m³)",
            "pm10": "PM10 (µg/m³)",
            "co2": "CO₂ (ppm)",
            "temperature": "Temp (°C)",
            "humidity": "Humidity (%)",
        },
    )

st.subheader("AQI trend")
trend = readings.dropna(subset=["aqi"]).sort_values("timestamp")
trend = trend[trend["timestamp"] >= trend["timestamp"].max() - pd.Timedelta(hours=1)]  # last hour
fig = px.line(
    trend, x="timestamp", y="aqi", color="location",
    labels={"timestamp": "Time (UTC)", "aqi": "AQI (5-min average)", "location": "Location"},
)
for upper, name in [(100, "Satisfactory"), (200, "Moderate"), (300, "Poor"), (400, "Very Poor")]:
    fig.add_hline(y=upper, line_dash="dot", line_color=CATEGORY_COLORS[name], opacity=0.6,
                  annotation_text=name, annotation_position="top left")
st.plotly_chart(fig, width="stretch")

st.divider()


# --------------------------------------------------
# ALERTS
# --------------------------------------------------

left, right = st.columns(2)
with left:
    st.subheader("Alerts by severity")
    severity_df = pd.DataFrame({"Severity": SEVERITIES, "Count": [by_severity.get(s, 0) for s in SEVERITIES]})
    fig = px.bar(severity_df, x="Severity", y="Count", color="Severity",
                 color_discrete_map={"CRITICAL": "#C00000", "HIGH": "#FF0000", "MEDIUM": "#FF9900", "LOW": "#FFD700"})
    st.plotly_chart(fig, width="stretch")

with right:
    st.subheader("Alerts by type")
    counts = {}
    for value in alerts.get("alert_types", pd.Series(dtype=str)).dropna().astype(str):
        for name in value.split(";"):
            if name.strip():
                counts[name.strip()] = counts.get(name.strip(), 0) + 1
    if counts:
        type_df = pd.DataFrame({"Alert type": list(counts), "Count": list(counts.values())})
        fig = px.bar(type_df, x="Count", y="Alert type", orientation="h")
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No alerts have been recorded yet.")

st.subheader("🚨 Alert log")
st.caption(f"{len(alerts):,} alert records match the current filters (one per sensor per 5-minute window).")
if alerts.empty:
    st.info("No alerts match the selected filters.")
else:
    st.dataframe(
        alerts.sort_values("processed_at", ascending=False)[
            ["processed_at", "location", "sensor_id", "severity", "alert_types", "peak_aqi", "reason", "evaluation_count"]
        ],
        width="stretch",
        hide_index=True,
        column_config={
            "processed_at": st.column_config.DatetimeColumn("Detected at (UTC)", format="YYYY-MM-DD HH:mm:ss"),
            "alert_types": st.column_config.TextColumn("Alert type", width="medium"),
            "peak_aqi": "Peak AQI",
            "reason": st.column_config.TextColumn("Evidence", width="large"),
            "evaluation_count": "Readings in window",
        },
    )

with st.expander("Recent processed readings"):
    st.dataframe(
        readings.sort_values("processed_at", ascending=False).head(200)[
            ["timestamp", "sensor_id", "location", "pm25", "pm10", "co2", "temperature",
             "humidity", "aqi", "aqi_category", "severity", "reason"]
        ],
        width="stretch",
        hide_index=True,
    )

st.divider()
st.caption(
    "Synthetic demonstration data from simulated sensors. CPCB defines the AQI on 24-hour "
    "averages; this dashboard uses a 5-minute rolling average so changes are visible quickly."
)
if st.button("Refresh now"):
    st.rerun()
