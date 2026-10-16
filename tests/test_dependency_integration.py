from __future__ import annotations

import hashlib
import shutil
from datetime import date

from smsi_archive_sync.config import AppConfig
from smsi_archive_sync.dependencies import DependencyReceiver
from smsi_archive_sync.scanner import scan_catalog
from smsi_archive_sync.sync_engine import SyncEngine
from smsi_archive_sync.ui import automatic_sync_days
from test_dependencies import Source, publish
from test_sync_engine import _build_fixture


def test_scanner_and_engine_receive_late_historical_supplements(tmp_path, monkeypatch):
    day, files = _build_fixture(tmp_path)
    ubuntu = tmp_path / "ubuntu"
    for key, raw in files.items():
        path = ubuntu / key.removeprefix("/data/smsi-archive/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    downloads = []

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
        def path(self, key):
            return ubuntu / key.removeprefix("/data/smsi-archive/")
        def list_directories(self, key):
            return [p.name for p in self.path(key.rstrip("/") + "/").iterdir() if p.is_dir()]
        def list_delivery_files(self, key):
            return [p.name for p in self.path(key).glob("*.json")]
        def read_small(self, key, maximum, *, missing_ok=False):
            path = self.path(key)
            return path.read_bytes() if path.exists() else None
        def download(self, key, target, size, *, cancel, limiter, progress):
            downloads.append(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.path(key), target)
            progress(size)
            return size

    monkeypatch.setattr("smsi_archive_sync.sync_engine.RemoteArchiveClient", Client)
    config = AppConfig(local_root=str(tmp_path / "windows"), minimum_free_gib=1, bandwidth_mib=0)
    native_catalog = scan_catalog(config, Client())
    SyncEngine(config).sync_day(native_catalog.days[0])
    assert len(downloads) == 1
    downloads.clear()
    base = hashlib.sha256(day.manifest_raw).hexdigest()
    cloud = tmp_path / "cloud"
    entry, _ = publish(cloud, base, node=day.collector_id, day=day.archive_date)
    DependencyReceiver(ubuntu / ("collector=" + day.collector_id), day.collector_id, Source(cloud)).sync(entry)
    catalog = scan_catalog(config, Client(), previous=native_catalog)
    selected = catalog.days[0]
    assert selected.local_status == "verified"
    assert selected.dependency_pending and selected.eligible
    # Late material for an already-held historical day must escape the native
    # seven-day automatic-sync cutoff.
    assert automatic_sync_days(catalog.days, today=date(2026, 10, 16)) == [selected]
    SyncEngine(config).sync_day(selected)
    assert len(downloads) == 2 and all("dependencies-v1/objects/" in key for key in downloads)
    rescanned = scan_catalog(config, Client()).days[0]
    assert rescanned.dependency_summary["state"] == "verified"
    assert rescanned.dependency_summary["dependency_status"] == "partial"
    assert not rescanned.eligible
    downloads.clear()
    newer, _ = publish(cloud, base, node=day.collector_id, day=day.archive_date,
                       generation=2, rows=(1, 2, 3))
    DependencyReceiver(ubuntu / ("collector=" + day.collector_id), day.collector_id, Source(cloud)).sync(newer)
    SyncEngine(config).sync_day(scan_catalog(config, Client()).days[0])
    assert len(downloads) == 1
    # A corrupt later supplement does not damage the native verification.
    later, later_objects = publish(cloud, base, node=day.collector_id, day=day.archive_date,
                                  generation=3, rows=(1, 2, 3, 4))
    DependencyReceiver(ubuntu / ("collector=" + day.collector_id), day.collector_id, Source(cloud)).sync(later)
    (ubuntu / ("collector=" + day.collector_id) / later_objects[-1]["relative_key"]).write_bytes(b"corrupt")
    selected = scan_catalog(config, Client()).days[0]
    result = SyncEngine(config).sync_day(selected)
    assert result["dependency_summary"]["state"] == "error"
    assert selected.local_status == "verified"
    assert scan_catalog(config, Client()).days[0].local_status == "verified"


def test_unverified_ubuntu_supplement_cannot_be_selected(tmp_path):
    # Even valid producer readbacks do not substitute for Ubuntu's own receipt.
    from test_dependencies import native
    ubuntu, windows = tmp_path / "ubuntu", tmp_path / "windows"
    base = native(windows)
    entry, _ = publish(ubuntu, base)
    receiver = DependencyReceiver(windows, "collector-a", Source(ubuntu), require_upstream_receipt=True)
    import pytest
    with pytest.raises(ValueError, match="upstream_not_verified"):
        receiver.inspect(entry)
