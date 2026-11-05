-- 001: initial schema for the air quality monitoring platform.
-- Raw readings live in Azure Blob Storage; these tables hold structured,
-- queryable records. All timestamps are timestamptz stored in UTC.

CREATE TABLE IF NOT EXISTS ingestion_batches (
    batch_id       TEXT PRIMARY KEY,
    blob_uri       TEXT,
    reading_count  INTEGER     NOT NULL DEFAULT 0,
    status         TEXT        NOT NULL CHECK (status IN ('archived', 'processed', 'partial', 'failed')),
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_batches_created ON ingestion_batches (created_at DESC);

CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id            TEXT PRIMARY KEY,
    reading_ts            TIMESTAMPTZ NOT NULL,
    sensor_id             TEXT        NOT NULL,
    location              TEXT,
    pm25                  DOUBLE PRECISION,
    pm10                  DOUBLE PRECISION,
    co2                   DOUBLE PRECISION,
    temperature           DOUBLE PRECISION,
    humidity              DOUBLE PRECISION,
    window_start          TIMESTAMPTZ NOT NULL,
    window_reading_count  INTEGER,
    pm25_avg              DOUBLE PRECISION,
    pm10_avg              DOUBLE PRECISION,
    aqi                   INTEGER,
    aqi_category          TEXT        NOT NULL,
    dominant_pollutant    TEXT,
    is_alert              BOOLEAN     NOT NULL,
    severity              TEXT        NOT NULL CHECK (severity IN ('NONE', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    alert_types           TEXT,
    reason                TEXT,
    batch_id              TEXT REFERENCES ingestion_batches (batch_id),
    processed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_readings_ts           ON sensor_readings (reading_ts DESC);
CREATE INDEX IF NOT EXISTS ix_readings_sensor_ts    ON sensor_readings (sensor_id, reading_ts DESC);
CREATE INDEX IF NOT EXISTS ix_readings_processed_at ON sensor_readings (processed_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id           BIGSERIAL PRIMARY KEY,
    sensor_id          TEXT        NOT NULL,
    location           TEXT,
    window_start       TIMESTAMPTZ NOT NULL,
    first_detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    detected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity           TEXT        NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    aqi                INTEGER,
    peak_aqi           INTEGER,
    aqi_category       TEXT,
    alert_types        TEXT,
    evidence           TEXT,
    last_reading_id    TEXT REFERENCES sensor_readings (reading_id),
    evaluation_count   INTEGER     NOT NULL DEFAULT 1,
    status             TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    -- One alert per sensor and five-minute window; re-evaluations update it.
    CONSTRAINT uq_alert_sensor_window UNIQUE (sensor_id, window_start)
);
CREATE INDEX IF NOT EXISTS ix_alerts_detected_at ON alerts (detected_at DESC);
CREATE INDEX IF NOT EXISTS ix_alerts_severity    ON alerts (severity, detected_at DESC);
