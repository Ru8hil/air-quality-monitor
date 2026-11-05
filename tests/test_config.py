import pytest

from src import config
from src.logging_setup import redact


def test_azure_mode_requires_cloud_settings(monkeypatch):
    monkeypatch.setattr(config, "IS_AZURE", True)
    monkeypatch.setattr(config, "APP_ENV", "azure")
    monkeypatch.setattr(config, "STORAGE_BACKEND", "csv")
    monkeypatch.setattr(config, "ARCHIVE_BACKEND", "local")
    with pytest.raises(config.ConfigError) as exc:
        config.validate_settings()
    assert "STORAGE_BACKEND=postgres" in str(exc.value)
    assert "ARCHIVE_BACKEND=azure_blob" in str(exc.value)


def test_postgres_needs_credentials(monkeypatch):
    monkeypatch.setattr(config, "STORAGE_BACKEND", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(config, "PGHOST", "server.postgres.database.azure.com")
    monkeypatch.setattr(config, "PGUSER", "airqadmin")
    monkeypatch.setattr(config, "PGPASSWORD", None)
    with pytest.raises(config.ConfigError, match="PGPASSWORD"):
        config.validate_settings()


def test_local_defaults_are_valid():
    config.validate_settings()


def test_secrets_are_redacted_from_logs():
    text = redact("AccountKey=abc123==;EndpointSuffix=core.windows.net password=hunter2")
    assert "abc123" not in text and "hunter2" not in text
