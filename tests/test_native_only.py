from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from smsi_archive_sync.config import AppConfig
from smsi_archive_sync.scanner import scan_catalog
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.sync_engine import SyncEngine
from smsi_archive_sync.ui import automatic_sync_days, archive_content_text
from test_sync_engine import _build_fixture


def test_scan_download_and_verify_ignore_retired_supplement(tmp_path, monkeypatch):
    day, remote = _build_fixture(tmp_path)
    requests, downloads = [], []
    root = "/data/smsi-archive"
    collector = root + "/collector=" + day.collector_id

    class Client:
        def __init__(self, _config=None):
            self.sftp = object()

        def connect(self):
            return self

        def close(self):
            self.sftp = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

        def list_directories(self, path):
            requests.append(path)
            assert "dependencies-v1" not in path
            return ["collector=" + day.collector_id] if path == root else ["date=" + day.archive_date, "dependencies-v1"]

        def read_small(self, key, maximum, *, missing_ok=False):
            requests.append(key)
            assert "dependencies-v1" not in key
            return remote.get(key)

        def download(self, key, target, size, *, cancel, limiter, progress):
            downloads.append(key)
            assert "dependencies-v1" not in key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(remote[key])
            progress(size)
            return size

    monkeypatch.setattr("smsi_archive_sync.sync_engine.RemoteArchiveClient", Client)
    config = AppConfig(local_root=str(tmp_path / "local"), minimum_free_gib=1, bandwidth_mib=0)
    leftover = config.local_path / f"collector={day.collector_id}/dependencies-v1/deliveries/broken.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"invalid retired supplement")
    catalog = scan_catalog(config, Client())
    assert len(catalog.days) == 1 and catalog.days[0].eligible
    assert SyncEngine(config).sync_day(catalog.days[0])["status"] == "verified"
    rescanned = scan_catalog(config, Client(), previous=catalog).days[0]
    assert rescanned.local_status == "verified" and not rescanned.eligible
    assert automatic_sync_days([rescanned], today=date(2026, 10, 17)) == []
    assert SyncEngine(config).verify_day(rescanned)["status"] == "verified"
    assert len(downloads) == 1 and all("dependencies-v1" not in key for key in requests)
    assert leftover.read_bytes() == b"invalid retired supplement"


@pytest.mark.parametrize("state", ["ready", "error", "verified", "cancelled"])
def test_old_cache_preserves_verified_day_without_scheduling_supplement(tmp_path, state):
    day, _ = _build_fixture(tmp_path)
    day.local_status = "verified"
    payload = day.to_dict()
    payload["dependency_summary"] = {"state": state, "catalog_key": "old", "bytes_total": 4096}
    store = StateStore(tmp_path / "state")
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO cache VALUES (?, ?, ?)",
                           ("catalog", json.dumps({"days": [payload]}), "2026-10-17"))
    restored = store.load_catalog().days[0]
    assert restored.local_status == "verified" and not restored.eligible
    assert automatic_sync_days([restored], today=date(2026, 10, 17)) == []
    assert "补包" not in archive_content_text(restored)
    assert "dependency_summary" not in restored.to_dict()
