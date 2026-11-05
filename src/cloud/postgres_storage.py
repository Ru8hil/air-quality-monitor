"""Azure Database for PostgreSQL backend (password authentication over TLS)."""

import logging
import time
from importlib import resources

import pandas as pd

from .. import config
from ..storage import READING_FIELDS, StorageBackend, StorageError, summarise

log = logging.getLogger(__name__)

MIGRATIONS_PACKAGE = "src.cloud.migrations"
# Arbitrary constant so only one process applies migrations at a time.
MIGRATION_LOCK_ID = 727_002
SEVERITY_RANK = "ARRAY['LOW','MEDIUM','HIGH','CRITICAL']"


def _conninfo(dbname=None):
    import psycopg.conninfo

    params = {
        "connect_timeout": config.PG_CONNECT_TIMEOUT,
        "application_name": f"airq-{config.SERVICE_NAME}",
        # Every session works in UTC so timestamps are consistent.
        "options": "-c TimeZone=UTC",
    }
    if config.DATABASE_URL:
        return psycopg.conninfo.make_conninfo(config.DATABASE_URL, **params)
    return psycopg.conninfo.make_conninfo(
        host=config.PGHOST,
        port=config.PGPORT,
        dbname=dbname or config.PGDATABASE,
        user=config.PGUSER,
        password=config.PGPASSWORD,
        sslmode=config.PGSSLMODE,
        **params,
    )


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


class PostgresStorage(StorageBackend):
    name = "postgres"

    def __init__(self, pool=None):
        self._pool = pool

    # -- connection management ---------------------------------------------
    @property
    def pool(self):
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                _conninfo(),
                min_size=config.PG_POOL_MIN,
                max_size=config.PG_POOL_MAX,
                max_lifetime=1800,
                check=ConnectionPool.check_connection,
                open=False,
                name="airq",
            )
            self._pool.open(wait=False)
        return self._pool

    def _run(self, operation, fn):
        """Run fn(conn) in one transaction, translating driver errors."""
        import psycopg
        from psycopg_pool import PoolTimeout

        try:
            with self.pool.connection(timeout=config.PG_CONNECT_TIMEOUT + 5) as conn:
                with conn.transaction():
                    return fn(conn)
        except PoolTimeout as exc:
            log.error(
                "PostgreSQL unavailable during %s", operation,
                extra={"operation": f"db_{operation}", "error_type": "PoolTimeout"},
            )
            raise StorageError(
                f"PostgreSQL is unavailable ({operation}: no connection within "
                f"{config.PG_CONNECT_TIMEOUT + 5}s)"
            ) from exc
        except psycopg.Error as exc:
            log.error(
                "PostgreSQL %s failed: %s", operation, exc.__class__.__name__,
                extra={"operation": f"db_{operation}", "error_type": exc.__class__.__name__},
            )
            raise StorageError(f"PostgreSQL {operation} failed: {exc}") from exc

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    # -- schema ----------------------------------------------------------------
    def ensure_database(self):
        """Create PGDATABASE if it does not exist (Azure: run once by init-db).

        A new Azure server only has the built-in "postgres" database; the
        server admin may create more. Skipped for DATABASE_URL (local Docker).
        """
        import psycopg
        from psycopg import sql

        if config.DATABASE_URL:
            return False
        with psycopg.connect(_conninfo(dbname="postgres"), autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (config.PGDATABASE,)
            ).fetchone()
            if exists:
                return False
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(config.PGDATABASE)))
            log.info("Created database %s", config.PGDATABASE, extra={"operation": "db_create"})
            return True

    def init_schema(self):
        files = sorted(
            f for f in resources.files(MIGRATIONS_PACKAGE).iterdir()
            if f.name.endswith(".sql")
        )

        def migrate(conn):
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version TEXT PRIMARY KEY,"
                " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            newly = []
            for f in files:
                version = f.name.removesuffix(".sql")
                if version in applied:
                    continue
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                newly.append(version)
            return newly

        newly = self._run("migrate", migrate)
        log.info(
            "Database schema up to date (applied: %s)", ", ".join(newly) or "none",
            extra={"operation": "db_migrate"},
        )
        return newly

    def health(self):
        started = time.monotonic()
        try:
            version = self._run(
                "health",
                lambda c: c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0],
            )
            return {
                "ok": version is not None,
                "backend": self.name,
                "schema_version": version,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "detail": "connected" if version else "schema not initialised (run manage.py init-db)",
            }
        except Exception as exc:  # health checks report, never raise
            return {"ok": False, "backend": self.name, "detail": str(exc).splitlines()[0]}

    # -- ingestion -------------------------------------------------------------
    def existing_ids(self, reading_ids):
        ids = [str(r) for r in reading_ids]
        if not ids:
            return set()
        rows = self._run(
            "lookup_readings",
            lambda c: c.execute(
                "SELECT reading_id FROM sensor_readings WHERE reading_id = ANY(%s)", (ids,)
            ).fetchall(),
        )
        return {r[0] for r in rows}

    def record_batch(self, batch_id, blob_uri, reading_count, status, error=None):
        self._run(
            "record_batch",
            lambda c: c.execute(
                """
                INSERT INTO ingestion_batches (batch_id, blob_uri, reading_count, status, error)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (batch_id) DO UPDATE SET
                    blob_uri = COALESCE(EXCLUDED.blob_uri, ingestion_batches.blob_uri),
                    reading_count = EXCLUDED.reading_count,
                    status = EXCLUDED.status,
                    error = EXCLUDED.error,
                    updated_at = now()
                """,
                (batch_id, blob_uri, reading_count, status, error),
            ),
        )

    def save_result(self, record, batch_id=None):
        batch_id = batch_id or record.get("batch_id")

        def write(conn):
            inserted = conn.execute(
                """
                INSERT INTO sensor_readings (
                    reading_id, reading_ts, sensor_id, location, pm25, pm10, co2,
                    temperature, humidity, window_start, window_reading_count, pm25_avg,
                    pm10_avg, aqi, aqi_category, dominant_pollutant, is_alert, severity,
                    alert_types, reason, batch_id, processed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (reading_id) DO NOTHING
                RETURNING reading_id
                """,
                (
                    str(record["reading_id"]), record["timestamp"], str(record["sensor_id"]),
                    record.get("location"),
                    *(_float_or_none(record.get(f)) for f in ("pm25", "pm10", "co2", "temperature", "humidity")),
                    record["window"], record.get("window_reading_count"),
                    record.get("pm25_avg"), record.get("pm10_avg"), record.get("aqi"),
                    record["aqi_category"], record.get("dominant_pollutant"),
                    bool(record["alert"]), record["severity"], record.get("alert_types"),
                    record.get("reason"), batch_id, record["processed_at"],
                ),
            ).fetchone()

            # A duplicate (retried) reading must not re-count its alert.
            if inserted is None or not record.get("alert"):
                return inserted is not None

            conn.execute(
                f"""
                INSERT INTO alerts (
                    sensor_id, location, window_start, first_detected_at, detected_at,
                    severity, aqi, peak_aqi, aqi_category, alert_types, evidence, last_reading_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sensor_id, window_start) DO UPDATE SET
                    detected_at = EXCLUDED.detected_at,
                    severity = CASE
                        WHEN array_position({SEVERITY_RANK}, EXCLUDED.severity)
                           > array_position({SEVERITY_RANK}, alerts.severity)
                        THEN EXCLUDED.severity ELSE alerts.severity END,
                    aqi = EXCLUDED.aqi,
                    peak_aqi = GREATEST(alerts.peak_aqi, EXCLUDED.peak_aqi),
                    aqi_category = EXCLUDED.aqi_category,
                    alert_types = EXCLUDED.alert_types,
                    evidence = EXCLUDED.evidence,
                    last_reading_id = EXCLUDED.last_reading_id,
                    evaluation_count = alerts.evaluation_count + 1
                """,
                (
                    str(record["sensor_id"]), record.get("location"), record["window"],
                    record["processed_at"], record["processed_at"], record["severity"],
                    record.get("aqi"), record.get("aqi"), record["aqi_category"],
                    record.get("alert_types"), record.get("reason"), str(record["reading_id"]),
                ),
            )
            return True

        return self._run("save_result", write)

    # -- state for the consumer ------------------------------------------------
    def recent_readings(self, since):
        columns = ", ".join(
            "reading_ts AS timestamp" if f == "timestamp" else f for f in READING_FIELDS
        )

        def query(conn):
            cur = conn.execute(
                f"SELECT {columns} FROM sensor_readings WHERE reading_ts >= %s "
                "ORDER BY reading_ts, reading_id",
                (since,),
            )
            names = [d.name for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

        readings = self._run("recent_readings", query)
        for reading in readings:
            reading["timestamp"] = _iso(reading["timestamp"])
        return readings

    # -- dashboard ---------------------------------------------------------------
    def _frame(self, operation, sql, params=()):
        def query(conn):
            cur = conn.execute(sql, params)
            names = [d.name for d in cur.description]
            return pd.DataFrame(cur.fetchall(), columns=names)

        return self._run(operation, query)

    def load_alerts(self, limit=None):
        return self._frame(
            "load_alerts",
            """
            SELECT sensor_id, location, window_start AS "window", first_detected_at,
                   detected_at AS processed_at, severity, aqi, peak_aqi, aqi_category,
                   alert_types, evidence AS reason, last_reading_id, evaluation_count
            FROM alerts ORDER BY detected_at DESC LIMIT %s
            """,
            (limit or config.DASHBOARD_ALERT_LIMIT,),
        )

    def load_readings(self, limit=None):
        return self._frame(
            "load_readings",
            """
            SELECT reading_id, reading_ts AS timestamp, sensor_id, location, pm25, pm10, co2,
                   temperature, humidity, window_start AS "window", window_reading_count,
                   pm25_avg, pm10_avg, aqi, aqi_category, dominant_pollutant,
                   is_alert AS alert, severity, alert_types, reason, processed_at, batch_id
            FROM sensor_readings ORDER BY processed_at DESC LIMIT %s
            """,
            (limit or config.DASHBOARD_READING_LIMIT,),
        )

    def stats(self):
        def query(conn):
            readings, sensors = conn.execute(
                "SELECT count(*), count(DISTINCT sensor_id) FROM sensor_readings"
            ).fetchone()
            rows = conn.execute("SELECT severity, count(*) FROM alerts GROUP BY 1").fetchall()
            return readings, sensors, rows

        readings, sensors, rows = self._run("stats", query)
        stats = summarise(pd.DataFrame(), readings, sensors)
        for severity, count in rows:
            stats["alerts_total"] += count
            stats["by_severity"][severity] = stats["by_severity"].get(severity, 0) + count
        return stats

    def last_batch(self):
        def query(conn):
            cur = conn.execute(
                "SELECT batch_id, blob_uri, reading_count, status, error, updated_at "
                "FROM ingestion_batches ORDER BY updated_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(zip([d.name for d in cur.description], row)) if row else None

        return self._run("last_batch", query)
