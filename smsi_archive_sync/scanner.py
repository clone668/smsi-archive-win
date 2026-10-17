from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .config import AppConfig, REMOTE_ROOT
from .models import ArchiveCatalog, ArchiveDay, ArchiveSummary, RemoteObject
from .protocol import (
    ManifestSnapshot,
    legacy_report_removal_item,
    parse_local_receipt,
    parse_manifest,
    parse_progress,
    progress_stage_label,
)
from .remote import RemoteArchiveClient, safe_remote_path


COLLECTOR_RE = re.compile(r"^collector=([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")
DATE_RE = re.compile(r"^date=(20\d{2}-\d{2}-\d{2})$")


def _archive_summary(
    snapshot: ManifestSnapshot,
    *,
    ubuntu_receipt: Mapping[str, Any] | None = None,
    windows_receipt: Mapping[str, Any] | None = None,
) -> ArchiveSummary:
    manifest = snapshot.manifest
    objects = snapshot.objects
    ledger = manifest.get("ledger_counts")
    ledger = ledger if isinstance(ledger, Mapping) else {}
    inventory = manifest.get("business_table_inventory")
    drive_results = (manifest.get("replicas") or {}).get("google_drive", [])
    starts = sorted(str(item.get("window_start")) for item in objects if item.get("window_start"))
    ends = sorted(str(item.get("window_end")) for item in objects if item.get("window_end"))
    ubuntu_verified_at = ""
    windows_verified_at = ""
    if isinstance(ubuntu_receipt, Mapping):
        ubuntu_verified_at = str(ubuntu_receipt.get("verified_at") or "")
    if isinstance(windows_receipt, Mapping):
        windows_verified_at = str(windows_receipt.get("verified_at") or "")
        ubuntu_verified_at = ubuntu_verified_at or str(
            windows_receipt.get("upstream_verified_at") or ""
        )
    return ArchiveSummary(
        created_at=str(manifest.get("created_at") or ""),
        business_table_count=len(inventory) if isinstance(inventory, list) else 0,
        batch_count=int(ledger.get("batch_count") or 0),
        ledger_record_count=int(ledger.get("record_count") or 0),
        unqualified_batch_count=int(ledger.get("unqualified_batch_count") or 0),
        kind_counts=dict(Counter(str(item.get("kind") or "unknown") for item in objects)),
        window_start=starts[0] if starts else "",
        window_end=ends[-1] if ends else "",
        remote_verified_count=sum(
            1
            for item in drive_results
            if isinstance(item, Mapping) and item.get("read_verified") is True
        ) if isinstance(drive_results, list) else 0,
        ubuntu_verified_at=ubuntu_verified_at,
        windows_verified_at=windows_verified_at,
    )


def _json_payload(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _local_paths(config: AppConfig, collector_id: str, archive_date: str) -> tuple[Path, Path]:
    collector_root = config.local_path / f"collector={collector_id}"
    return collector_root / f"date={archive_date}", collector_root / ".partial" / f"date={archive_date}"


def _verification_failure_detail(path: Path) -> str:
    """Describe a recorded verification failure without trusting the file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    failed_at = str(payload.get("failed_at") or "")[:32] or "--"
    error = str(payload.get("error") or "")[:200] or "未记录原因"
    return f"完整校验失败于 {failed_at}：{error}"


def _local_inventory(config: AppConfig, day: ArchiveDay) -> None:
    final, partial = _local_paths(config, day.collector_id, day.archive_date)
    root = final if final.is_dir() else partial if partial.is_dir() else None
    if root is None:
        day.local_status = "missing"
        day.local_detail = "本地没有副本"
        return
    local_bytes = 0
    missing = 0
    for obj in day.objects:
        parts = PurePosixPath(obj.relative_key).parts
        path = root.joinpath(*parts[1:])
        if path.is_file():
            obj.local_bytes = path.stat().st_size
            local_bytes += obj.local_bytes
        else:
            missing += 1
    day.bytes_local = local_bytes
    receipt_path = final / ".smsi-verified.json"
    manifest_path = final / "manifest.json"
    failure_path = final / ".smsi-verification-failed.json"
    if failure_path.is_file():
        # A failed deep verification is the whole point of the feature: it must
        # outrank the older receipt that is still sitting next to it, otherwise
        # the day goes back to green on the next scan and the finding is lost.
        day.local_status = "verification_failed"
        day.local_detail = _verification_failure_detail(failure_path)
        return
    if final.is_dir() and receipt_path.is_file() and manifest_path.is_file():
        try:
            local_manifest_raw = manifest_path.read_bytes()
            local_manifest_sha = hashlib.sha256(local_manifest_raw).hexdigest()
            if local_manifest_sha != day.manifest_sha256:
                old_snapshot = parse_manifest(local_manifest_raw, day.archive_date)
                new_snapshot = parse_manifest(day.manifest_raw, day.archive_date)
                old_receipt = parse_local_receipt(
                    receipt_path.read_bytes(), day.archive_date, old_snapshot.sha256
                )
                if (
                    legacy_report_removal_item(old_snapshot, new_snapshot) is not None
                    and int(old_receipt.get("object_count") or -1)
                    == old_snapshot.object_count
                    and int(old_receipt.get("row_count") or -1)
                    == old_snapshot.row_count
                ):
                    day.local_status = "metadata_update"
                    day.local_detail = "仅需同步报告清单，不会重新下载业务数据"
                else:
                    day.local_status = "manifest_changed"
                    day.local_detail = "本地 manifest 与 Ubuntu 当前清单不同"
                return
        except OSError:
            day.local_status = "local_unverified"
            day.local_detail = "本地 manifest 无法读取"
            return
        try:
            receipt = parse_local_receipt(
                receipt_path.read_bytes(), day.archive_date, day.manifest_sha256
            )
            if not missing:
                level = str(receipt.get("verification_level") or "deep")
                day.local_verification_level = level if level in {"transport", "deep"} else ""
                day.local_status = "verified"
                day.summary.windows_verified_at = str(receipt.get("verified_at") or "")
                day.summary.ubuntu_verified_at = (
                    day.summary.ubuntu_verified_at
                    or str(receipt.get("upstream_verified_at") or "")
                )
                label = "传输校验" if day.local_verification_level == "transport" else "完整校验"
                day.local_detail = f"本地{label}于 {receipt.get('verified_at') or '--'}"
                return
        except (OSError, RuntimeError, ValueError):
            pass
        day.local_status = "local_unverified"
        day.local_detail = "本地目录存在，但验证凭据无效"
        return
    day.local_status = "partial" if root == partial else "local_unverified"
    day.local_detail = f"已存在 {local_bytes:,} 字节，尚未发布验证结果"


def _local_only_days(config: AppConfig, known: set[str]) -> list[ArchiveDay]:
    result: list[ArchiveDay] = []
    root = config.local_path
    if not root.is_dir():
        return result
    for collector_dir in sorted(root.glob("collector=*"), key=lambda item: item.name.casefold()):
        match = COLLECTOR_RE.fullmatch(collector_dir.name)
        if not match or not collector_dir.is_dir():
            continue
        collector_id = match.group(1)
        for date_dir in sorted(collector_dir.glob("date=*"), key=lambda item: item.name.casefold()):
            date_match = DATE_RE.fullmatch(date_dir.name)
            if not date_match or not date_dir.is_dir():
                continue
            archive_date = date_match.group(1)
            key = f"{collector_id}/{archive_date}"
            if key in known:
                continue
            manifest_path = date_dir / "manifest.json"
            receipt_path = date_dir / ".smsi-verified.json"
            failure_path = date_dir / ".smsi-verification-failed.json"
            try:
                raw = manifest_path.read_bytes()
                snapshot = parse_manifest(raw, archive_date)
                receipt = parse_local_receipt(receipt_path.read_bytes(), archive_date, snapshot.sha256)
                if receipt.get("status") != "verified":
                    continue
                failed = failure_path.is_file()
                day = ArchiveDay(
                    collector_id=collector_id,
                    archive_date=archive_date,
                    remote_status="remote_missing",
                    remote_detail="Ubuntu 当前未发布此日期，本地副本保留",
                    local_status="verification_failed" if failed else "verified",
                    local_detail=(
                        _verification_failure_detail(failure_path)
                        if failed
                        else "本地传输校验通过"
                        if receipt.get("verification_level") == "transport"
                        else "本地完整校验通过"
                    ),
                    local_verification_level=(
                        "transport" if receipt.get("verification_level") == "transport" else "deep"
                    ),
                    manifest_sha256=snapshot.sha256,
                    object_count=snapshot.object_count,
                    row_count=snapshot.row_count,
                    bytes_total=sum(int(item.get("size_bytes") or 0) for item in snapshot.objects),
                    bytes_local=sum(int(item.get("size_bytes") or 0) for item in snapshot.objects),
                    objects=[RemoteObject.from_manifest(item) for item in snapshot.objects],
                    summary=_archive_summary(snapshot, windows_receipt=receipt),
                )
                day.set_manifest_raw(raw)
                result.append(day)
            except (OSError, RuntimeError, ValueError):
                continue
    return result


def scan_catalog(
    config: AppConfig,
    client: RemoteArchiveClient,
    progress: Callable[[str, int, int], None] | None = None,
    previous: ArchiveCatalog | None = None,
) -> ArchiveCatalog:
    root = config.remote_root.rstrip("/") or REMOTE_ROOT
    collectors = [
        match.group(1)
        for name in client.list_directories(root)
        if (match := COLLECTOR_RE.fullmatch(name))
    ]
    date_pairs: list[tuple[str, str]] = []
    for collector_id in collectors:
        collector_path = safe_remote_path(root, f"collector={collector_id}")
        for name in client.list_directories(collector_path):
            match = DATE_RE.fullmatch(name)
            if match:
                date_pairs.append((collector_id, match.group(1)))
    total = len(date_pairs)
    days: list[ArchiveDay] = []
    previous_days = {day.key: day for day in (previous.days if previous else [])}
    stable_before = datetime.now(timezone.utc).date() - timedelta(days=3)
    for index, (collector_id, archive_date) in enumerate(sorted(date_pairs), start=1):
        if progress:
            progress(f"扫描 {collector_id}/date={archive_date}", index - 1, total)
        day_path = safe_remote_path(root, f"collector={collector_id}", f"date={archive_date}")
        cached = previous_days.get(f"{collector_id}/{archive_date}")
        try:
            parsed_date = date.fromisoformat(archive_date)
        except ValueError:
            parsed_date = None
        if (
            cached is not None
            and parsed_date is not None
            and parsed_date < stable_before
            and cached.remote_status == "ready"
            and cached.manifest_sha256
        ):
            final, partial = _local_paths(config, collector_id, archive_date)
            cache_is_still_applicable = (
                cached.local_status == "missing"
                and not final.exists()
                and not partial.exists()
            )
            if cache_is_still_applicable:
                receipt_raw = client.read_small(
                    safe_remote_path(day_path, ".smsi-verified.json"),
                    1024 * 1024,
                    missing_ok=True,
                )
                try:
                    if receipt_raw is not None:
                        parse_local_receipt(
                            receipt_raw,
                            archive_date,
                            cached.manifest_sha256,
                        )
                        days.append(cached)
                        if progress:
                            progress(
                                f"复用缓存 {collector_id}/date={archive_date}",
                                index,
                                total,
                            )
                        continue
                except RuntimeError:
                    pass
        progress_raw = client.read_small(
            safe_remote_path(day_path, "_smsi-archive-progress.json"), 1024 * 1024, missing_ok=True
        )
        remote_status = "waiting_manifest"
        remote_detail = "Ubuntu 尚未发布 manifest"
        if progress_raw is not None:
            try:
                snapshot = parse_progress(progress_raw, archive_date)
            except RuntimeError as exc:
                # The marker is display-only and best-effort (it can be read
                # mid-write).  It must never block a sync: an unreadable marker
                # falls through to the manifest, which is the authority.
                remote_detail = f"归档进度标记无法解析（{exc}）"
            else:
                if snapshot.status == "running":
                    remote_status = "remote_running"
                    remote_detail = f"阶段：{progress_stage_label(snapshot.stage)}"
                elif snapshot.status == "failed":
                    remote_status = "remote_failed"
                    remote_detail = snapshot.error or progress_stage_label(snapshot.stage)
        manifest_raw = client.read_small(
            safe_remote_path(day_path, "manifest.json"), 32 * 1024 * 1024, missing_ok=True
        )
        objects: list[RemoteObject] = []
        manifest_sha = ""
        manifest_b64 = ""
        object_count = row_count = bytes_total = 0
        archive_summary = ArchiveSummary()
        if manifest_raw is not None:
            try:
                manifest = parse_manifest(manifest_raw, archive_date)
                manifest_sha = manifest.sha256
                manifest_b64 = __import__("base64").b64encode(manifest_raw).decode("ascii")
                objects = [RemoteObject.from_manifest(item) for item in manifest.objects]
                object_count, row_count = manifest.object_count, manifest.row_count
                bytes_total = sum(obj.size_bytes for obj in objects)
                receipt_raw = client.read_small(
                    safe_remote_path(day_path, ".smsi-verified.json"), 32 * 1024 * 1024, missing_ok=True
                )
                receipt_ok = False
                receipt_payload: dict[str, Any] | None = None
                if receipt_raw is not None:
                    try:
                        receipt_payload = parse_local_receipt(
                            receipt_raw, archive_date, manifest_sha
                        )
                        receipt_ok = True
                    except RuntimeError:
                        pass
                if receipt_ok and remote_status not in {"remote_running", "remote_failed", "invalid"}:
                    remote_status, remote_detail = "ready", "Ubuntu 已验证，可同步"
                elif not receipt_ok and remote_status == "waiting_manifest":
                    remote_status, remote_detail = "waiting_verification", "manifest 已发布，但 Ubuntu 验证凭据未完成"
                archive_summary = _archive_summary(
                    manifest, ubuntu_receipt=receipt_payload
                )
            except RuntimeError as exc:
                remote_status, remote_detail = "invalid", str(exc)
        day = ArchiveDay(
            collector_id=collector_id,
            archive_date=archive_date,
            remote_status=remote_status,
            remote_detail=remote_detail,
            manifest_sha256=manifest_sha,
            manifest_b64=manifest_b64,
            object_count=object_count,
            row_count=row_count,
            bytes_total=bytes_total,
            objects=objects,
            summary=archive_summary,
        )
        _local_inventory(config, day)
        if remote_status == "ready" and day.local_status == "verified" and day.manifest_sha256:
            day.remote_status = "ready"
        days.append(day)
        if progress:
            progress(f"扫描 {collector_id}/date={archive_date}", index, total)
    known = {day.key for day in days}
    days.extend(_local_only_days(config, known))
    days.sort(key=lambda item: (item.collector_id.casefold(), item.archive_date), reverse=False)
    return ArchiveCatalog(
        scanned_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        remote_root=REMOTE_ROOT,
        days=days,
    )
