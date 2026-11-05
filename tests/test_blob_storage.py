import json

import pytest

from src.cloud.blob_storage import ArchiveError, LocalArchiver, blob_name, serialize_batch


def test_blob_name_layout():
    from datetime import datetime, timezone

    when = datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc)
    assert blob_name("b1", when, prefix="raw") == "raw/2026/11/04/09/b1.jsonl"


def test_serialize_is_one_json_object_per_line():
    data = serialize_batch([{"a": 1}, {"b": "µg"}]).decode("utf-8")
    assert [json.loads(line) for line in data.splitlines()] == [{"a": 1}, {"b": "µg"}]


def test_local_archiver_never_overwrites_different_content(tmp_path):
    archiver = LocalArchiver(tmp_path)
    uri = archiver.archive_batch("b1", [{"a": 1}])
    assert uri.startswith("file://")
    archiver.archive_batch("b1", [{"a": 1}])        # identical retry is fine
    with pytest.raises(ArchiveError):
        archiver.archive_batch("b1", [{"a": 2}])
    assert archiver.health()["ok"]
