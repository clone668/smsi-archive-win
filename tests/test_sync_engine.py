from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from smsi_archive_sync.config import AppConfig
from smsi_archive_sync.models import ArchiveDay, RemoteObject
from smsi_archive_sync.protocol import OperationCancelled
from smsi_archive_sync.scanner import _local_inventory
from smsi_archive_sync.sync_engine import SyncEngine


def _build_fixture(tmp_path: Path) -> tuple[ArchiveDay, dict[str, bytes]]:
    data_path = tmp_path / "source.parquet"
    table = pa.table({"value": [1, 2, 3]})
    pq.write_table(table, data_path)
    data = data_path.read_bytes()
    archive_date = "2026-08-09"
    relative = f"date={archive_date}/control/sample.parquet"
    manifest = {
        "contract_version": "smsi-long-term-archive-manifest/v3",
        "status": "verified",
        "archive_date": archive_date,
        "retention_delete_allowed": True,
        "objects": [{
            "relative_key": relative,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "schema_sha256": hashlib.sha256(str(table.schema).encode()).hexdigest(),
            "kind": "control",
            "format": "parquet",
            "row_count": 3,
        }],
        "object_count": 1,
        "row_count": 3,
        "replicas": {"google_drive": [{"read_verified": True}]},
    }
    manifest_raw = json.dumps(manifest, ensure_ascii=False).encode()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    receipt = json.dumps({
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": archive_date,
        "manifest_sha256": manifest_sha,
    }).encode()
    day = ArchiveDay(
        collector_id="tencent-paper",
        archive_date=archive_date,
        remote_status="ready",
        manifest_sha256=manifest_sha,
        object_count=1,
        row_count=3,
        bytes_total=len(data),
        objects=[RemoteObject.from_manifest(manifest["objects"][0])],
    )
    day.set_manifest_raw(manifest_raw)
    remote = {
        "/data/smsi-archive/collector=tencent-paper/date=2026-08-09/control/sample.parquet": data,
        "/data/smsi-archive/collector=tencent-paper/date=2026-08-09/manifest.json": manifest_raw,
        "/data/smsi-archive/collector=tencent-paper/date=2026-08-09/.smsi-verified.json": receipt,
    }
    return day, remote


def test_sync_downloads_verifies_and_publishes_atomically(tmp_path, monkeypatch) -> None:
    day, remote = _build_fixture(tmp_path)
    progress_events = []

    class FakeClient:
        def __init__(self, _config):
            self.sftp = object()

        def connect(self):
            return self

        def close(self):
            self.sftp = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def read_small(self, path, _maximum):
            return remote[path]

        def download(self, remote_path, destination, _expected_size, *, cancel, limiter, progress):
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = remote[remote_path]
            with destination.open("wb") as handle:
                handle.write(payload)
            progress(len(payload))
            return len(payload)

    monkeypatch.setattr("smsi_archive_sync.sync_engine.RemoteArchiveClient", FakeClient)
    monkeypatch.setattr(
        "smsi_archive_sync.sync_engine.verify_local_day",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary sync must not run deep verification")
        ),
    )
    config = AppConfig(local_root=str(tmp_path / "archive"), bandwidth_mib=0)
    engine = SyncEngine(config, progress=progress_events.append)
    result = engine.sync_day(day)
    final = tmp_path / "archive" / "collector=tencent-paper" / "date=2026-08-09"
    assert result["status"] == "verified"
    assert result["verification_level"] == "transport"
    assert result["transport_checks"] == {
        "size_verified": True,
        "sha256_verified": True,
        "manifest_unchanged": True,
        "upstream_receipt_verified": True,
    }
    verifying = [event for event in progress_events if event.get("phase") == "verifying"]
    assert verifying
    assert verifying[0]["verification_level"] == "transport"
    assert verifying[0]["current_object"].endswith("control/sample.parquet")
    assert any(
        event["current_object"] == "确认 manifest 与 Ubuntu 验证凭据"
        for event in verifying
    )
    assert (final / "control" / "sample.parquet").is_file()
    assert json.loads((final / ".smsi-verified.json").read_text())[
        "manifest_sha256"
    ] == day.manifest_sha256
    assert not (tmp_path / "archive" / "collector=tencent-paper" / ".partial").exists()

    rescanned_day, _ = _build_fixture(tmp_path)
    _local_inventory(config, rescanned_day)
    assert rescanned_day.local_status == "verified"
    assert rescanned_day.local_verification_level == "transport"
    assert "传输校验" in rescanned_day.local_detail


def _publish_local_copy(tmp_path: Path) -> tuple[ArchiveDay, AppConfig, Path]:
    """Lay down a verified local copy the way a finished sync leaves it."""
    day, remote = _build_fixture(tmp_path)
    config = AppConfig(local_root=str(tmp_path / "archive"), bandwidth_mib=0)
    final = config.local_path / "collector=tencent-paper" / "date=2026-08-09"
    (final / "control").mkdir(parents=True)
    (final / "manifest.json").write_bytes(day.manifest_raw)
    (final / "control" / "sample.parquet").write_bytes(
        remote[
            "/data/smsi-archive/collector=tencent-paper/"
            "date=2026-08-09/control/sample.parquet"
        ]
    )
    SyncEngine(config).verify_day(day)
    return day, config, final


def test_failed_deep_verification_survives_the_next_scan(tmp_path) -> None:
    """A detected bad copy must not go green again 15 minutes later.

    The failure marker was written but never read, so the stale receipt next to
    it put the day back to "本地完整校验通过" on the following scan — silently
    discarding the only finding the feature exists to produce.
    """
    day, config, final = _publish_local_copy(tmp_path)
    target = final / "control" / "sample.parquet"
    target.write_bytes(target.read_bytes() + b"corruption")

    with pytest.raises(RuntimeError):
        SyncEngine(config).verify_day(day)

    assert (final / ".smsi-verification-failed.json").is_file()
    assert (final / ".smsi-verified.json").is_file()  # the stale receipt is still there

    rescanned, _ = _build_fixture(tmp_path)
    _local_inventory(config, rescanned)
    assert rescanned.local_status == "verification_failed"
    assert "完整校验失败" in rescanned.local_detail


def test_cancelling_a_verification_is_not_recorded_as_a_failure(tmp_path) -> None:
    """Cancelling is a user action, not a verdict about the data."""
    day, config, final = _publish_local_copy(tmp_path)
    engine = SyncEngine(config)
    engine.cancel.set()

    with pytest.raises(OperationCancelled):
        engine.verify_day(day)

    assert not (final / ".smsi-verification-failed.json").exists()

    rescanned, _ = _build_fixture(tmp_path)
    _local_inventory(config, rescanned)
    assert rescanned.local_status == "verified"


def test_manual_verify_keeps_deep_audit(tmp_path) -> None:
    day, remote = _build_fixture(tmp_path)
    config = AppConfig(local_root=str(tmp_path / "archive"), bandwidth_mib=0)
    final = (
        config.local_path
        / "collector=tencent-paper"
        / "date=2026-08-09"
    )
    (final / "control").mkdir(parents=True)
    (final / "manifest.json").write_bytes(day.manifest_raw)
    (final / "control" / "sample.parquet").write_bytes(
        remote[
            "/data/smsi-archive/collector=tencent-paper/"
            "date=2026-08-09/control/sample.parquet"
        ]
    )
    progress_events = []

    report = SyncEngine(config, progress=progress_events.append).verify_day(day)

    assert report["status"] == "verified"
    assert report["verification_level"] == "deep"
    assert any(
        event.get("phase") == "verifying"
        and event.get("verification_level") == "deep"
        for event in progress_events
    )


def test_legacy_report_removal_updates_only_metadata(tmp_path, monkeypatch) -> None:
    base_day, remote = _build_fixture(tmp_path)
    archive_date = base_day.archive_date
    old_manifest = json.loads(base_day.manifest_raw)
    report_raw = json.dumps({
        "contract_version": "smsi-runtime-health-report/v1",
        "archive_date": archive_date,
        "collector_node_id": base_day.collector_id,
        "overall_status": "critical",
    }, sort_keys=True).encode()
    report_item = {
        "relative_key": f"date={archive_date}/runtime-report.json",
        "size_bytes": len(report_raw),
        "sha256": hashlib.sha256(report_raw).hexdigest(),
        "kind": "runtime_report",
        "format": "json",
        "row_count": 1,
        "report_contract_version": "smsi-runtime-health-report/v1",
    }
    old_manifest["objects"].append(report_item)
    old_manifest["object_count"] = 2
    old_manifest["row_count"] = 4
    old_manifest["replicas"]["google_drive"].append({"read_verified": True})
    old_manifest_raw = json.dumps(old_manifest, ensure_ascii=False).encode()
    old_manifest_sha = hashlib.sha256(old_manifest_raw).hexdigest()

    new_manifest = deepcopy(old_manifest)
    new_manifest["objects"] = new_manifest["objects"][:1]
    new_manifest["object_count"] = 1
    new_manifest["row_count"] = 3
    new_manifest["replicas"]["google_drive"] = new_manifest["replicas"][
        "google_drive"
    ][:1]
    new_manifest["maintenance"] = {
        "contract_version": "smsi-archive-manifest-maintenance/v1",
        "action": "remove_legacy_runtime_report",
        "previous_manifest_sha256": old_manifest_sha,
    }
    new_manifest_raw = json.dumps(new_manifest, ensure_ascii=False).encode()
    new_manifest_sha = hashlib.sha256(new_manifest_raw).hexdigest()
    new_receipt = json.dumps({
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": archive_date,
        "manifest_sha256": new_manifest_sha,
        "verified_at": "2026-08-11T08:00:00Z",
    }).encode()

    day = ArchiveDay(
        collector_id=base_day.collector_id,
        archive_date=archive_date,
        remote_status="ready",
        manifest_sha256=new_manifest_sha,
        object_count=1,
        row_count=3,
        bytes_total=base_day.bytes_total,
        objects=[RemoteObject.from_manifest(new_manifest["objects"][0])],
    )
    day.set_manifest_raw(new_manifest_raw)
    config = AppConfig(local_root=str(tmp_path / "archive"), bandwidth_mib=0)
    final = config.local_path / f"collector={day.collector_id}" / f"date={archive_date}"
    (final / "control").mkdir(parents=True)
    business_path = final / "control" / "sample.parquet"
    business_raw = remote[
        f"/data/smsi-archive/collector={day.collector_id}/"
        f"date={archive_date}/control/sample.parquet"
    ]
    business_path.write_bytes(business_raw)
    (final / "runtime-report.json").write_bytes(report_raw)
    (final / "manifest.json").write_bytes(old_manifest_raw)
    (final / ".smsi-verified.json").write_text(json.dumps({
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": archive_date,
        "manifest_sha256": old_manifest_sha,
        "object_count": 2,
        "row_count": 4,
        "objects": [
            {
                "relative_key": item["relative_key"],
                "size_bytes": item["size_bytes"],
                "sha256": item["sha256"],
            }
            for item in old_manifest["objects"]
        ],
    }), encoding="utf-8")
    before_hash = hashlib.sha256(business_path.read_bytes()).hexdigest()
    before_mtime = business_path.stat().st_mtime_ns

    class FakeClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read_small(self, path, _maximum):
            if path.endswith("/manifest.json"):
                return new_manifest_raw
            if path.endswith("/.smsi-verified.json"):
                return new_receipt
            raise AssertionError(path)

        def download(self, *_args, **_kwargs):
            raise AssertionError("metadata migration must not download business files")

    monkeypatch.setattr("smsi_archive_sync.sync_engine.RemoteArchiveClient", FakeClient)

    result = SyncEngine(config).sync_day(day)

    assert result["manifest_sha256"] == new_manifest_sha
    assert result["downloaded_bytes_this_run"] == 0
    assert not (final / "runtime-report.json").exists()
    assert (final / "manifest.json").read_bytes() == new_manifest_raw
    assert json.loads((final / ".smsi-verified.json").read_text())["manifest_sha256"] == new_manifest_sha
    assert hashlib.sha256(business_path.read_bytes()).hexdigest() == before_hash
    assert business_path.stat().st_mtime_ns == before_mtime
