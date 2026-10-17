from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import AppConfig
from .models import ArchiveDay, RemoteObject
from .protocol import (
    ManifestSnapshot,
    legacy_report_removal_item,
    parse_local_receipt,
    parse_manifest,
)
from .remote import (
    BandwidthLimiter,
    OperationCancelled,
    RemoteArchiveClient,
    safe_remote_path,
)
from .storage import bytes_text, directory_bytes, free_bytes, reserve_bytes
from .verifier import (
    build_transport_verification_report,
    local_object_path,
    sha256_file,
    verify_local_day,
)


ProgressCallback = Callable[[dict[str, Any]], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


class SyncEngine:
    def __init__(
        self,
        config: AppConfig,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self.cancel = cancel or threading.Event()
        self.progress = progress
        self.limiter = BandwidthLimiter(config.bandwidth_mib)
        self._thread_local = threading.local()
        self._clients: list[RemoteArchiveClient] = []
        self._clients_lock = threading.Lock()

    def _emit(self, **payload: Any) -> None:
        if self.progress:
            self.progress(payload)

    def _client(self) -> RemoteArchiveClient:
        client = getattr(self._thread_local, "client", None)
        if client is None or client.sftp is None:
            client = RemoteArchiveClient(self.config).connect()
            self._thread_local.client = client
            with self._clients_lock:
                self._clients.append(client)
        return client

    def close(self) -> None:
        with self._clients_lock:
            clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    def _roots(self, day: ArchiveDay) -> tuple[Path, Path, Path]:
        collector = self.config.local_path / f"collector={day.collector_id}"
        return (
            collector / f"date={day.archive_date}",
            collector / ".partial" / f"date={day.archive_date}",
            collector / ".quarantine",
        )

    def _snapshot(self, day: ArchiveDay) -> ManifestSnapshot:
        if not day.manifest_raw:
            raise RuntimeError(f"{day.key} 缺少 manifest")
        snapshot = parse_manifest(day.manifest_raw, day.archive_date)
        if snapshot.sha256 != day.manifest_sha256:
            raise RuntimeError(f"{day.key} manifest 摘要不一致")
        return snapshot

    def _prepare_stage(self, day: ArchiveDay, snapshot: ManifestSnapshot) -> Path:
        _final, stage, quarantine = self._roots(day)
        if stage.exists():
            staged_manifest = stage / "manifest.json"
            if not staged_manifest.is_file() or staged_manifest.read_bytes() != snapshot.raw:
                quarantine.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                destination = quarantine / f"{stage.name}.{stamp}.manifest-changed"
                counter = 1
                while destination.exists():
                    destination = destination.with_name(f"{destination.name}.{counter}")
                    counter += 1
                os.replace(stage, destination)
        stage.mkdir(parents=True, exist_ok=True)
        temporary = stage / "manifest.json.tmp"
        temporary.write_bytes(snapshot.raw)
        temporary.replace(stage / "manifest.json")
        return stage

    def _remote_object_path(self, day: ArchiveDay, obj: RemoteObject) -> str:
        parts = PurePosixPath(obj.relative_key).parts
        if not parts or parts[0] != f"date={day.archive_date}":
            raise RuntimeError(f"归档对象路径与日期不匹配: {obj.relative_key}")
        return safe_remote_path(
            self.config.remote_root,
            f"collector={day.collector_id}",
            f"date={day.archive_date}",
            *parts[1:],
        )

    def _download_object(
        self,
        day: ArchiveDay,
        stage: Path,
        obj: RemoteObject,
        update: Callable[[str, int, bool, str], None],
    ) -> int:
        if self.cancel.is_set():
            raise OperationCancelled("同步已取消，未完成结果不会发布")
        destination = local_object_path(stage, obj.relative_key, day.archive_date)
        if destination.is_file() and destination.stat().st_size == obj.size_bytes:
            update(obj.relative_key, obj.size_bytes, False, "verifying")
            if sha256_file(destination, self.cancel) == obj.sha256:
                update(obj.relative_key, obj.size_bytes, True, "verifying")
                return 0
        temporary = destination.with_name(destination.name + ".downloading")
        remote_path = self._remote_object_path(day, obj)

        def on_progress(current: int) -> None:
            update(obj.relative_key, current, False, "syncing")

        observed = self._client().download(
            remote_path,
            temporary,
            obj.size_bytes,
            cancel=self.cancel,
            limiter=self.limiter,
            progress=on_progress,
        )
        update(obj.relative_key, observed, False, "verifying")
        if observed != obj.size_bytes or sha256_file(temporary, self.cancel) != obj.sha256:
            corrupt = temporary.with_name(
                temporary.name + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            )
            temporary.replace(corrupt)
            raise RuntimeError(f"下载文件 SHA-256 不一致: {obj.relative_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
        update(obj.relative_key, obj.size_bytes, True, "verifying")
        return obj.size_bytes

    def _confirm_remote_unchanged(
        self, day: ArchiveDay, snapshot: ManifestSnapshot
    ) -> dict[str, Any]:
        with RemoteArchiveClient(self.config) as client:
            day_root = safe_remote_path(
                self.config.remote_root,
                f"collector={day.collector_id}",
                f"date={day.archive_date}",
            )
            current_raw = client.read_small(
                safe_remote_path(day_root, "manifest.json"), 32 * 1024 * 1024
            )
            current = parse_manifest(bytes(current_raw), day.archive_date)
            if current.sha256 != snapshot.sha256:
                raise RuntimeError("同步期间 Ubuntu manifest 已变化，本次结果不会发布")
            receipt_raw = client.read_small(
                safe_remote_path(day_root, ".smsi-verified.json"), 32 * 1024 * 1024
            )
            return parse_local_receipt(
                bytes(receipt_raw), day.archive_date, snapshot.sha256
            )

    def _migrate_legacy_report_removal(
        self,
        day: ArchiveDay,
        snapshot: ManifestSnapshot,
    ) -> dict[str, Any] | None:
        final, _stage, _quarantine = self._roots(day)
        manifest_path = final / "manifest.json"
        receipt_path = final / ".smsi-verified.json"
        if not manifest_path.is_file() or not receipt_path.is_file():
            return None
        old_manifest_raw = manifest_path.read_bytes()
        old_snapshot = parse_manifest(old_manifest_raw, day.archive_date)
        report_item = legacy_report_removal_item(old_snapshot, snapshot)
        if report_item is None:
            return None
        old_receipt_raw = receipt_path.read_bytes()
        old_receipt = parse_local_receipt(
            old_receipt_raw, day.archive_date, old_snapshot.sha256
        )
        if (
            int(old_receipt.get("object_count") or -1) != old_snapshot.object_count
            or int(old_receipt.get("row_count") or -1) != old_snapshot.row_count
        ):
            return None
        old_objects = old_receipt.get("objects")
        if not isinstance(old_objects, list):
            return None
        old_evidence = {
            (
                str(item.get("relative_key") or ""),
                int(item.get("size_bytes") or -1),
                str(item.get("sha256") or ""),
            )
            for item in old_objects
            if isinstance(item, dict)
        }
        expected_evidence = {
            (
                str(item.get("relative_key") or ""),
                int(item.get("size_bytes") or -1),
                str(item.get("sha256") or ""),
            )
            for item in old_snapshot.objects
        }
        if old_evidence != expected_evidence:
            return None
        report_path = local_object_path(
            final, str(report_item["relative_key"]), day.archive_date
        )
        report_raw = report_path.read_bytes() if report_path.is_file() else None
        if report_raw is not None and (
            len(report_raw) != int(report_item["size_bytes"])
            or hashlib.sha256(report_raw).hexdigest() != str(report_item["sha256"])
        ):
            raise RuntimeError("旧运行报告与原 manifest 不一致，拒绝自动迁移")
        self._emit(
            phase="verifying",
            verification_level="transport",
            collector_id=day.collector_id,
            archive_date=day.archive_date,
            current_object="同步报告清单并确认 Ubuntu 验证凭据",
            object_count=day.object_count,
            objects_done=day.object_count,
            bytes_total=day.bytes_total,
            bytes_done=day.bytes_total,
        )
        upstream_receipt = self._confirm_remote_unchanged(day, snapshot)
        verification = build_transport_verification_report(
            final, snapshot, upstream_receipt
        )
        verification.update({
            "collector_id": day.collector_id,
            "source_type": "ubuntu_ssh",
            "source_root": self.config.remote_root,
            "downloaded_bytes_this_run": 0,
        })
        try:
            write_bytes_atomic(manifest_path, snapshot.raw)
            if report_path.exists():
                report_path.unlink()
            write_json_atomic(receipt_path, verification)
        except Exception:
            write_bytes_atomic(manifest_path, old_manifest_raw)
            write_bytes_atomic(receipt_path, old_receipt_raw)
            if report_raw is not None:
                write_bytes_atomic(report_path, report_raw)
            elif report_path.exists():
                report_path.unlink()
            raise
        # This path republishes in place, so a stale failure marker would
        # outlive the receipt that supersedes it.
        (final / ".smsi-verification-failed.json").unlink(missing_ok=True)
        self._emit(
            phase="verified",
            verification_level="transport",
            collector_id=day.collector_id,
            archive_date=day.archive_date,
            object_count=day.object_count,
            objects_done=day.object_count,
            bytes_total=day.bytes_total,
            bytes_done=day.bytes_total,
            current_object="",
        )
        return verification

    def _pending_bytes(self, day: ArchiveDay, stage: Path) -> int:
        """Bytes this run still has to write, ignoring what is already staged.

        Size alone decides here, exactly as the download fast path does: a
        staged file of the right length costs nothing more to keep, and if its
        digest turns out wrong the reserve absorbs the re-download.
        """
        pending = 0
        for obj in day.objects:
            destination = local_object_path(stage, obj.relative_key, day.archive_date)
            try:
                if destination.stat().st_size == obj.size_bytes:
                    continue
            except OSError:
                pass
            pending += obj.size_bytes
        return pending

    def _require_room(self, day: ArchiveDay, stage: Path, quarantine: Path) -> None:
        """Refuse a day that cannot fit before writing its first byte.

        Filling the volume mid-transfer fails inside one object and leaves the
        user to work out which of a hundred files ran out of room.  Publishing
        is a rename on the same volume and quarantining moves nothing off it, so
        the space a run needs is simply what it still has to download, plus the
        margin the volume must keep.
        """
        pending = self._pending_bytes(day, stage)
        reserve = reserve_bytes(self.config.minimum_free_gib)
        try:
            free = free_bytes(stage)
        except OSError as exc:
            raise RuntimeError(f"无法读取本地归档盘可用空间：{exc}") from exc
        if free >= pending + reserve:
            return
        held = directory_bytes(quarantine)
        hint = (
            f"；客户端隔离目录仍占用 {bytes_text(held)}，确认无用后可手动删除 {quarantine}"
            if held > 0
            else ""
        )
        raise RuntimeError(
            f"本地归档盘空间不足，{day.key} 未开始下载："
            f"仍需下载 {bytes_text(pending)}，另需保留 {bytes_text(reserve)}，"
            f"当前可用 {bytes_text(free)}{hint}"
        )

    def sync_day(self, day: ArchiveDay) -> dict[str, Any]:
        if not day.eligible:
            raise RuntimeError(f"{day.key} 当前不可同步")
        snapshot = self._snapshot(day)
        migrated = self._migrate_legacy_report_removal(day, snapshot)
        if migrated is not None:
            return migrated
        final, _stage_path, quarantine = self._roots(day)
        stage = self._prepare_stage(day, snapshot)
        self._require_room(day, stage, quarantine)
        object_progress = {obj.relative_key: 0 for obj in day.objects}
        object_sizes = {obj.relative_key: obj.size_bytes for obj in day.objects}
        object_done: set[str] = set()
        lock = threading.Lock()
        started = time.monotonic()

        def update(key: str, current: int, completed: bool, phase: str) -> None:
            with lock:
                object_progress[key] = max(0, min(int(current), next(
                    obj.size_bytes for obj in day.objects if obj.relative_key == key
                )))
                if completed:
                    object_done.add(key)
                bytes_done = sum(object_progress.values())
                elapsed = max(0.001, time.monotonic() - started)
                speed = bytes_done / elapsed
                eta = int((day.bytes_total - bytes_done) / speed) if speed > 0 else None
                self._emit(
                    phase=phase,
                    verification_level="transport" if phase == "verifying" else "",
                    collector_id=day.collector_id,
                    archive_date=day.archive_date,
                    current_object=key,
                    object_count=day.object_count,
                    objects_done=len(object_done),
                    bytes_total=day.bytes_total,
                    bytes_done=bytes_done,
                    speed_bytes_per_second=speed,
                    eta_seconds=eta,
                    current_object_bytes=object_progress[key],
                    current_object_total=object_sizes[key],
                )

        transferred = 0
        try:
            self._emit(
                phase="syncing",
                collector_id=day.collector_id,
                archive_date=day.archive_date,
                object_count=day.object_count,
                objects_done=0,
                bytes_total=day.bytes_total,
                bytes_done=0,
                current_object="",
                current_object_bytes=0,
                current_object_total=0,
            )
            with ThreadPoolExecutor(max_workers=self.config.download_workers) as executor:
                futures = {
                    executor.submit(self._download_object, day, stage, obj, update): obj
                    for obj in day.objects
                }
                for future in as_completed(futures):
                    transferred += future.result()
            if self.cancel.is_set():
                raise OperationCancelled("同步已取消，未完成结果不会发布")

            self._emit(
                phase="verifying",
                verification_level="transport",
                collector_id=day.collector_id,
                archive_date=day.archive_date,
                current_object="确认 manifest 与 Ubuntu 验证凭据",
                object_count=day.object_count,
                objects_done=day.object_count,
                bytes_total=day.bytes_total,
                bytes_done=day.bytes_total,
                current_object_bytes=0,
                current_object_total=0,
            )
            upstream_receipt = self._confirm_remote_unchanged(day, snapshot)
            report = build_transport_verification_report(
                stage, snapshot, upstream_receipt
            )
            report.update({
                "collector_id": day.collector_id,
                "source_type": "ubuntu_ssh",
                "source_root": self.config.remote_root,
                "downloaded_bytes_this_run": transferred,
            })
            write_json_atomic(stage / ".smsi-verified.json", report)
            if final.exists():
                quarantine.mkdir(parents=True, exist_ok=True)
                destination = quarantine / (
                    final.name + "." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    + ".replaced"
                )
                os.replace(final, destination)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, final)
            partial_parent = stage.parent
            if partial_parent.exists() and not any(partial_parent.iterdir()):
                partial_parent.rmdir()
            self._emit(
                phase="verified",
                verification_level="transport",
                collector_id=day.collector_id,
                archive_date=day.archive_date,
                object_count=day.object_count,
                objects_done=day.object_count,
                bytes_total=day.bytes_total,
                bytes_done=day.bytes_total,
                current_object="",
                speed_bytes_per_second=0,
                eta_seconds=0,
                current_object_bytes=0,
                current_object_total=0,
            )
            return report
        finally:
            self.close()

    def sync_days(self, days: list[ArchiveDay]) -> list[dict[str, Any]]:
        eligible = [day for day in days if day.eligible]
        if not eligible:
            raise RuntimeError("选中范围内没有可同步归档")
        results: list[dict[str, Any]] = []
        for day in eligible:
            if self.cancel.is_set():
                raise OperationCancelled("同步已取消，未完成结果不会发布")
            results.append(self.sync_day(day))
        return results

    def verify_day(self, day: ArchiveDay) -> dict[str, Any]:
        snapshot = self._snapshot(day)
        final, _stage, _quarantine = self._roots(day)
        if not final.is_dir():
            raise RuntimeError(f"{day.key} 本地正式目录不存在")

        def on_progress(key: str, index: int, total: int) -> None:
            self._emit(
                phase="verifying",
                verification_level="deep",
                collector_id=day.collector_id,
                archive_date=day.archive_date,
                current_object=key,
                object_count=total,
                objects_done=index,
                bytes_total=day.bytes_total,
                bytes_done=day.bytes_total,
            )

        try:
            report = verify_local_day(final, snapshot, on_progress, self.cancel)
        except OperationCancelled:
            # Cancelling is not a verdict.  Recording it as a failure would
            # accuse a healthy day of being corrupt.
            raise
        except Exception as exc:
            write_json_atomic(final / ".smsi-verification-failed.json", {
                "contract_version": "smsi-local-verification-failure/v1",
                "status": "failed",
                "archive_date": day.archive_date,
                "error": str(exc),
                "failed_at": utc_now(),
            })
            raise
        report.update({
            "collector_id": day.collector_id,
            "source_type": "ubuntu_ssh",
            "source_root": self.config.remote_root,
            "downloaded_bytes_this_run": 0,
        })
        write_json_atomic(final / ".smsi-verified.json", report)
        (final / ".smsi-verification-failed.json").unlink(missing_ok=True)
        self._emit(
            phase="verified",
            verification_level="deep",
            collector_id=day.collector_id,
            archive_date=day.archive_date,
            object_count=day.object_count,
            objects_done=day.object_count,
            bytes_total=day.bytes_total,
            bytes_done=day.bytes_total,
            current_object="",
        )
        return report

    def verify_days(self, days: list[ArchiveDay]) -> list[dict[str, Any]]:
        selected = [day for day in days if day.local_status in {"verified", "local_unverified"}]
        if not selected:
            raise RuntimeError("选中范围内没有可校验的本地归档")
        return [self.verify_day(day) for day in selected]
