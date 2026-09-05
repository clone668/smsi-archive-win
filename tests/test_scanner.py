from __future__ import annotations

import hashlib
import json

from smsi_archive_sync.config import AppConfig
from smsi_archive_sync.models import ArchiveCatalog, ArchiveDay
from smsi_archive_sync.scanner import scan_catalog


def test_incomplete_ubuntu_archive_is_not_eligible(tmp_path) -> None:
    archive_date = "2026-08-09"
    root = "/data/smsi-archive"
    collector = f"{root}/collector=tencent-paper"
    day_root = f"{collector}/date={archive_date}"
    progress = json.dumps({
        "contract_version": "smsi-archive-progress/v1",
        "archive_date": archive_date,
        "status": "running",
        "stage": "remote_upload",
    }).encode()

    class FakeClient:
        def list_directories(self, path):
            if path == root:
                return ["collector=tencent-paper"]
            if path == collector:
                return [f"date={archive_date}"]
            return []

        def read_small(self, path, _maximum, *, missing_ok=False):
            if path == f"{day_root}/_smsi-archive-progress.json":
                return progress
            return None

    catalog = scan_catalog(AppConfig(local_root=str(tmp_path / "archive")), FakeClient())
    assert len(catalog.days) == 1
    assert catalog.days[0].remote_status == "remote_running"
    assert catalog.days[0].eligible is False


def test_unreadable_progress_marker_does_not_block_the_day(tmp_path) -> None:
    """The marker is best-effort; only the manifest decides.

    A marker read mid-write used to make the day "invalid", which skipped that
    whole sync cycle even though the archive was published and verified.
    """
    archive_date = "2026-08-11"
    root = "/data/smsi-archive"
    collector = f"{root}/collector=tencent-paper"
    day_root = f"{collector}/date={archive_date}"
    manifest = {
        "contract_version": "smsi-long-term-archive-manifest/v3",
        "status": "verified",
        "archive_date": archive_date,
        "retention_delete_allowed": True,
        "objects": [],
        "object_count": 0,
        "row_count": 0,
        "replicas": {"google_drive": []},
    }
    manifest_raw = json.dumps(manifest, sort_keys=True).encode()
    receipt = json.dumps({
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": archive_date,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
    }).encode()

    class FakeClient:
        def list_directories(self, path):
            if path == root:
                return ["collector=tencent-paper"]
            if path == collector:
                return [f"date={archive_date}"]
            return []

        def read_small(self, path, _maximum, *, missing_ok=False):
            if path == f"{day_root}/_smsi-archive-progress.json":
                return b'{"contract_version": "smsi-archive-pro'
            if path == f"{day_root}/manifest.json":
                return manifest_raw
            if path == f"{day_root}/.smsi-verified.json":
                return receipt
            return None

    catalog = scan_catalog(AppConfig(local_root=str(tmp_path / "archive")), FakeClient())

    assert catalog.days[0].remote_status == "ready"


def test_stable_published_history_reuses_cached_manifest(tmp_path) -> None:
    archive_date = "2026-07-01"
    root = "/data/smsi-archive"
    collector = f"{root}/collector=tencent-paper"
    day_root = f"{collector}/date={archive_date}"
    cached_sha = "a" * 64
    receipt = json.dumps({
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": archive_date,
        "manifest_sha256": cached_sha,
    }).encode()

    class FakeClient:
        reads = 0

        def list_directories(self, path):
            if path == root:
                return ["collector=tencent-paper"]
            if path == collector:
                return [f"date={archive_date}"]
            return []

        def read_small(self, path, _maximum, *, missing_ok=False):
            self.reads += 1
            assert path == f"{day_root}/.smsi-verified.json"
            return receipt

    cached = ArchiveDay(
        collector_id="tencent-paper",
        archive_date=archive_date,
        remote_status="ready",
        local_status="missing",
        manifest_sha256=cached_sha,
    )
    previous = ArchiveCatalog(days=[cached])
    client = FakeClient()

    catalog = scan_catalog(
        AppConfig(local_root=str(tmp_path / "archive")),
        client,
        previous=previous,
    )

    assert catalog.days == [cached]
    assert client.reads == 1


def test_stable_cache_receipt_change_forces_manifest_rescan(tmp_path) -> None:
    archive_date = "2026-07-01"
    root = "/data/smsi-archive"
    collector = f"{root}/collector=tencent-paper"
    day_root = f"{collector}/date={archive_date}"
    manifest = {
        "contract_version": "smsi-long-term-archive-manifest/v3",
        "status": "verified",
        "archive_date": archive_date,
        "retention_delete_allowed": True,
        "objects": [],
        "object_count": 0,
        "row_count": 0,
        "replicas": {"google_drive": []},
    }
    manifest_raw = json.dumps(manifest, sort_keys=True).encode()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    receipt = json.dumps({
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": archive_date,
        "manifest_sha256": manifest_sha,
    }).encode()

    class FakeClient:
        paths: list[str] = []

        def list_directories(self, path):
            if path == root:
                return ["collector=tencent-paper"]
            if path == collector:
                return [f"date={archive_date}"]
            return []

        def read_small(self, path, _maximum, *, missing_ok=False):
            self.paths.append(path)
            if path.endswith("/_smsi-archive-progress.json"):
                return None
            if path.endswith("/manifest.json"):
                return manifest_raw
            if path.endswith("/.smsi-verified.json"):
                return receipt
            raise AssertionError(path)

    cached = ArchiveDay(
        collector_id="tencent-paper",
        archive_date=archive_date,
        remote_status="ready",
        local_status="missing",
        manifest_sha256="a" * 64,
    )
    client = FakeClient()

    catalog = scan_catalog(
        AppConfig(local_root=str(tmp_path / "archive")),
        client,
        previous=ArchiveCatalog(days=[cached]),
    )

    assert catalog.days[0].manifest_sha256 == manifest_sha
    assert catalog.days[0].remote_status == "ready"
    assert f"{day_root}/manifest.json" in client.paths
