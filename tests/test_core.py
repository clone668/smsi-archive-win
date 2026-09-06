from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from smsi_archive_sync.config import AppConfig, ConfigStore, REMOTE_ROOT
from smsi_archive_sync.models import ArchiveCatalog, ArchiveDay, ArchiveSummary, RemoteObject
from smsi_archive_sync.protocol import (
    parse_manifest,
    parse_local_receipt,
    parse_progress,
    progress_stage_label,
    validate_relative_key,
)
from smsi_archive_sync.remote import safe_remote_path
from smsi_archive_sync.scanner import _archive_summary
from smsi_archive_sync.state import StateStore
from smsi_archive_sync.secrets import protect_password, unprotect_password
from smsi_archive_sync.storage import GIB, directory_bytes, disk_status, usable_root


def manifest_payload(archive_date: str = "2026-08-09") -> dict:
    content = {
        "relative_key": f"date={archive_date}/control/sample.parquet",
        "size_bytes": 12,
        "sha256": "a" * 64,
        "schema_sha256": "b" * 64,
        "kind": "control",
        "format": "parquet",
        "row_count": 2,
    }
    return {
        "contract_version": "smsi-long-term-archive-manifest/v3",
        "status": "verified",
        "archive_date": archive_date,
        "retention_delete_allowed": True,
        "objects": [content],
        "object_count": 1,
        "row_count": 2,
        "replicas": {"google_drive": [{"read_verified": True}]},
    }


def _progress_payload(stage: str, status: str = "running", archive_date: str = "2026-08-09") -> bytes:
    return json.dumps({
        "contract_version": "smsi-archive-progress/v1",
        "archive_date": archive_date,
        "status": status,
        "stage": stage,
    }).encode()


def test_progress_accepts_stages_the_collector_adds_later() -> None:
    """The stage is display-only, so an unknown name must not be an error.

    The collector already publishes two stages an allowlist never knew about;
    rejecting them turned a healthy archive run into a failure.
    """
    for stage in (
        "preparing",
        "parquet_generation",
        "remote_upload",
        "runtime_report_generation",
        "runtime_report_upload",
        "manifest_publication",
        "some_stage_invented_in_2027",
    ):
        snapshot = parse_progress(_progress_payload(stage), "2026-08-09")
        assert snapshot.stage == stage
        assert snapshot.status == "running"

    assert progress_stage_label("runtime_report_upload") == "上传运行报告"
    assert progress_stage_label("some_stage_invented_in_2027") == "some_stage_invented_in_2027"


def test_progress_still_refuses_garbage_and_inconsistent_terminals() -> None:
    for stage in ("", "Remote Upload", "../etc", "x" * 65):
        with pytest.raises(RuntimeError):
            parse_progress(_progress_payload(stage), "2026-08-09")
    with pytest.raises(RuntimeError):
        parse_progress(_progress_payload("remote_upload", status="verified"), "2026-08-09")
    with pytest.raises(RuntimeError):
        parse_progress(_progress_payload("verified", status="running"), "2026-08-09")


def test_manifest_and_path_contract() -> None:
    raw = json.dumps(manifest_payload(), ensure_ascii=False).encode()
    snapshot = parse_manifest(raw, "2026-08-09")
    assert snapshot.object_count == 1
    assert snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert validate_relative_key("date=2026-08-09/control/sample.parquet", "2026-08-09")
    with pytest.raises(RuntimeError):
        validate_relative_key("date=2026-08-09/../secret", "2026-08-09")
    assert safe_remote_path(REMOTE_ROOT, "collector=tencent-paper", "date=2026-08-09") == (
        "/data/smsi-archive/collector=tencent-paper/date=2026-08-09"
    )


def test_local_receipt_requires_current_manifest() -> None:
    payload = {
        "contract_version": "smsi-local-archive-verification/v1",
        "status": "verified",
        "archive_date": "2026-08-09",
        "manifest_sha256": "a" * 64,
    }
    raw = json.dumps(payload).encode()
    assert parse_local_receipt(raw, "2026-08-09", "a" * 64)["status"] == "verified"
    with pytest.raises(RuntimeError):
        parse_local_receipt(raw, "2026-08-09", "b" * 64)


def test_archive_summary_uses_manifest_and_receipts() -> None:
    payload = manifest_payload()
    payload.update({
        "created_at": "2026-08-10T02:46:53Z",
        "business_table_inventory": ["price_data", "ticker_data"],
        "ledger_counts": {
            "batch_count": 123,
            "record_count": 456,
            "unqualified_batch_count": 0,
        },
    })
    payload["objects"][0].update({
        "window_start": "2026-08-09T00:00:00Z",
        "window_end": "2026-08-10T00:00:00Z",
    })
    snapshot = parse_manifest(json.dumps(payload).encode(), "2026-08-09")
    summary = _archive_summary(
        snapshot,
        ubuntu_receipt={"verified_at": "2026-08-10T03:00:00Z"},
        windows_receipt={"verified_at": "2026-08-10T04:00:00Z"},
    )
    assert summary.business_table_count == 2
    assert summary.batch_count == 123
    assert summary.ledger_record_count == 456
    assert summary.kind_counts == {"control": 1}
    assert summary.remote_verified_count == 1
    assert summary.window_start == "2026-08-09T00:00:00Z"
    assert summary.window_end == "2026-08-10T00:00:00Z"
    assert summary.ubuntu_verified_at == "2026-08-10T03:00:00Z"
    assert summary.windows_verified_at == "2026-08-10T04:00:00Z"


def test_config_and_state_round_trip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config")
    config = store.load()
    assert config.remote_root == REMOTE_ROOT
    assert config.bandwidth_mib == 20
    config.local_root = str(tmp_path / "archive")
    config.auto_sync_enabled = True
    config.floating_window_enabled = True
    store.save(config)
    loaded_config = ConfigStore(tmp_path / "config").load()
    assert loaded_config.local_root == str(tmp_path / "archive")
    assert loaded_config.auto_sync_enabled is True
    assert loaded_config.floating_window_enabled is True

    day = ArchiveDay(
        collector_id="tencent-paper",
        archive_date="2026-08-09",
        remote_status="ready",
        manifest_sha256="a" * 64,
        objects=[RemoteObject(relative_key="date=2026-08-09/control/a.parquet", size_bytes=10, sha256="b" * 64)],
        object_count=1,
        bytes_total=10,
        summary=ArchiveSummary(batch_count=25, business_table_count=3),
    )
    catalog = ArchiveCatalog(scanned_at="2026-08-10T00:00:00Z", days=[day])
    state = StateStore(tmp_path / "state")
    state.save_catalog(catalog)
    loaded = state.load_catalog()
    assert loaded is not None
    assert loaded.days[0].key == day.key
    assert loaded.days[0].summary.batch_count == 25
    assert loaded.days[0].summary.business_table_count == 3


def test_a_damaged_config_file_is_reported_as_such(tmp_path: Path) -> None:
    """The launcher catches RuntimeError to report this, so it has to be one.

    A windowed process cannot print a traceback, so a config file that will not
    parse has to arrive as a message the entry point can put in a dialog rather
    than as whatever the JSON reader happened to raise.
    """
    store = ConfigStore(tmp_path / "config")
    store.load()
    store.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError) as failure:
        store.load()
    assert "损坏" in str(failure.value)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows DPAPI only")
def test_password_is_dpapi_protected() -> None:
    encrypted = protect_password("test-password")
    assert encrypted.startswith("dpapi:v1:")
    assert "test-password" not in encrypted
    assert unprotect_password(encrypted) == "test-password"


def test_free_space_is_read_before_the_archive_directory_exists(tmp_path: Path) -> None:
    """A first run has no archive directory yet, but its volume has the space."""
    missing = tmp_path / "archive" / "collector=tencent-paper" / ".partial"

    assert usable_root(missing) == tmp_path
    status = disk_status(missing, 1)
    assert status["ok"] is True
    assert status["free_bytes"] > 0
    assert status["reserve_bytes"] == GIB
    assert status["low"] is False


def test_an_unreadable_volume_is_reported_instead_of_assumed_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """"No reading" must never be allowed to look like "there is room"."""

    def refuse(_path):
        raise OSError(21, "设备未就绪")

    monkeypatch.setattr("smsi_archive_sync.storage.shutil.disk_usage", refuse)

    status = disk_status(tmp_path, 10)
    assert status["ok"] is False
    assert status["low"] is False
    assert status["free_bytes"] == 0
    assert "设备未就绪" in status["error"]


def test_held_bytes_are_counted_so_a_shortage_can_be_acted_on(tmp_path: Path) -> None:
    """The shortage message names the client's own leftovers to reclaim first."""
    quarantine = tmp_path / ".quarantine" / "date=2026-08-09.replaced"
    (quarantine / "control").mkdir(parents=True)
    (quarantine / "control" / "a.parquet").write_bytes(b"x" * 1500)
    (quarantine / "manifest.json").write_bytes(b"y" * 500)

    assert directory_bytes(quarantine) == 2000
    assert directory_bytes(tmp_path / "never-created") == 0


def test_the_disk_reserve_is_validated_and_survives_a_reload(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config")
    config = store.load()
    assert config.minimum_free_gib == 10

    config.local_root = str(tmp_path / "archive")
    config.minimum_free_gib = 25
    store.save(config)
    assert ConfigStore(tmp_path / "config").load().minimum_free_gib == 25

    config.minimum_free_gib = 0
    with pytest.raises(ValueError, match="磁盘保留空间"):
        store.save(config)
