from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping


DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_CONTRACT = "smsi-long-term-archive-manifest/v3"
RUNTIME_REPORT_CONTRACT = "smsi-runtime-health-report/v1"
PROGRESS_CONTRACT = "smsi-archive-progress/v1"
LOCAL_RECEIPT_CONTRACT = "smsi-local-archive-verification/v1"
# The stage is a display-only label on a best-effort marker; nothing here
# branches on it.  It is therefore validated by *shape*, not by an allowlist:
# every time the collector published a new stage name the old allowlist made
# the day "无效" and skipped that sync cycle.  Terminal consistency (see
# parse_progress) is the invariant that actually has to hold.
PROGRESS_STAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PROGRESS_STAGE_LABELS = {
    "preparing": "准备中",
    "parquet_generation": "导出 Parquet",
    "remote_upload": "上传远端",
    "runtime_report_generation": "生成运行报告",
    "runtime_report_upload": "上传运行报告",
    "manifest_publication": "发布清单",
    "verified": "已验证",
    "failed": "失败",
}


def progress_stage_label(stage: str) -> str:
    """Human label for a stage, falling back to the raw name when unknown."""
    return PROGRESS_STAGE_LABELS.get(stage, stage)


class OperationCancelled(RuntimeError):
    """The user cancelled; whatever was in flight is incomplete, not broken.

    Defined here so that both the transfer layer and the verifier can raise the
    same type: a cancelled verification must never be recorded as a failed one.
    """


@dataclass(frozen=True)
class ProgressSnapshot:
    archive_date: str
    status: str
    stage: str
    error: str = ""


@dataclass(frozen=True)
class ManifestSnapshot:
    archive_date: str
    manifest: dict[str, Any]
    raw: bytes
    sha256: str

    @property
    def objects(self) -> list[dict[str, Any]]:
        return list(self.manifest["objects"])

    @property
    def object_count(self) -> int:
        return int(self.manifest["object_count"])

    @property
    def row_count(self) -> int:
        return int(self.manifest["row_count"])


def validate_relative_key(value: Any, archive_date: str) -> str:
    text = str(value or "").strip("/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in text
        or len(path.parts) < 2
        or path.parts[0] != f"date={archive_date}"
    ):
        raise RuntimeError(f"归档对象路径无效: {text or '--'}")
    return text


def parse_progress(raw: bytes, archive_date: str) -> ProgressSnapshot:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("归档进度文件格式无效") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("归档进度文件格式无效")
    status = str(payload.get("status") or "")
    stage = str(payload.get("stage") or "")
    if payload.get("contract_version") != PROGRESS_CONTRACT:
        raise RuntimeError("归档进度协议不受支持")
    if str(payload.get("archive_date") or "") != archive_date:
        raise RuntimeError("归档进度日期不匹配")
    if status not in {"running", "verified", "failed"}:
        raise RuntimeError("归档进度状态无效")
    if not PROGRESS_STAGE_RE.fullmatch(stage):
        raise RuntimeError("归档进度阶段无效")
    if (
        (status == "running" and stage in {"verified", "failed"})
        or (status == "verified" and stage != "verified")
        or (status == "failed" and stage != "failed")
    ):
        raise RuntimeError("归档进度终态不一致")
    return ProgressSnapshot(
        archive_date=archive_date,
        status=status,
        stage=stage,
        error=str(payload.get("error") or "")[:2000],
    )


def parse_manifest(raw: bytes, archive_date: str) -> ManifestSnapshot:
    if not DATE_RE.fullmatch(archive_date):
        raise RuntimeError("归档日期无效")
    if len(raw) > 32 * 1024 * 1024:
        raise RuntimeError("manifest 超过安全大小")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("manifest 格式无效") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("manifest 格式无效")
    if payload.get("contract_version") != MANIFEST_CONTRACT:
        raise RuntimeError("manifest 协议不受支持")
    if payload.get("status") != "verified":
        raise RuntimeError("manifest 尚未验证完成")
    if str(payload.get("archive_date") or "") != archive_date:
        raise RuntimeError("manifest 日期不匹配")
    if payload.get("retention_delete_allowed") is not True:
        raise RuntimeError("manifest 未通过远端清理门禁")
    objects = payload.get("objects")
    if not isinstance(objects, list) or int(payload.get("object_count") or 0) != len(objects):
        raise RuntimeError("manifest 对象列表无效")
    keys: set[str] = set()
    row_total = 0
    runtime_report_count = 0
    for item in objects:
        if not isinstance(item, dict):
            raise RuntimeError("manifest 对象记录无效")
        key = validate_relative_key(item.get("relative_key"), archive_date)
        if key in keys:
            raise RuntimeError(f"manifest 对象路径重复: {key}")
        keys.add(key)
        if int(item.get("size_bytes") or -1) < 0:
            raise RuntimeError(f"manifest 对象大小无效: {key}")
        if not SHA_RE.fullmatch(str(item.get("sha256") or "")):
            raise RuntimeError(f"manifest 对象摘要无效: {key}")
        kind = str(item.get("kind") or "")
        item_format = str(item.get("format") or "parquet")
        if kind == "runtime_report":
            runtime_report_count += 1
            if (
                runtime_report_count > 1
                or item_format != "json"
                or PurePosixPath(key).name != "runtime-report.json"
                or item.get("report_contract_version") != RUNTIME_REPORT_CONTRACT
            ):
                raise RuntimeError(f"manifest 运行报告对象无效: {key}")
        elif not SHA_RE.fullmatch(str(item.get("schema_sha256") or "")):
            raise RuntimeError(f"manifest schema 摘要无效: {key}")
        if kind == "business" and not SHA_RE.fullmatch(str(item.get("content_sha256") or "")):
            raise RuntimeError(f"manifest 业务内容摘要无效: {key}")
        rows = int(item.get("row_count") or 0)
        if rows < 0:
            raise RuntimeError(f"manifest 对象行数无效: {key}")
        row_total += rows
    if int(payload.get("row_count") or 0) != row_total:
        raise RuntimeError("manifest 总行数不一致")
    replicas = payload.get("replicas")
    drive_results = replicas.get("google_drive") if isinstance(replicas, Mapping) else None
    if not isinstance(drive_results, list) or len(drive_results) != len(objects):
        raise RuntimeError("Google Drive 验证证据不完整")
    if any(not isinstance(item, Mapping) or item.get("read_verified") is not True for item in drive_results):
        raise RuntimeError("Google Drive 完整读回证据不完整")
    return ManifestSnapshot(
        archive_date=archive_date,
        manifest=payload,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def legacy_report_removal_item(
    old_snapshot: ManifestSnapshot,
    new_snapshot: ManifestSnapshot,
) -> dict[str, Any] | None:
    """Recognize the one allowed metadata-only manifest maintenance."""
    old_manifest = old_snapshot.manifest
    new_manifest = new_snapshot.manifest
    maintenance = new_manifest.get("maintenance")
    if (
        not isinstance(maintenance, Mapping)
        or maintenance.get("contract_version")
        != "smsi-archive-manifest-maintenance/v1"
        or maintenance.get("action") != "remove_legacy_runtime_report"
        or str(maintenance.get("previous_manifest_sha256") or "")
        != old_snapshot.sha256
    ):
        return None
    old_reports = [
        (index, item)
        for index, item in enumerate(old_snapshot.objects)
        if item.get("kind") == "runtime_report"
    ]
    if len(old_reports) != 1 or any(
        item.get("kind") == "runtime_report" for item in new_snapshot.objects
    ):
        return None
    report_index, report_item = old_reports[0]
    if (
        str(report_item.get("relative_key") or "")
        != f"date={old_snapshot.archive_date}/runtime-report.json"
        or report_item.get("report_contract_version") != RUNTIME_REPORT_CONTRACT
    ):
        return None
    if [
        item for index, item in enumerate(old_snapshot.objects)
        if index != report_index
    ] != new_snapshot.objects:
        return None
    ignored = {"objects", "object_count", "row_count", "replicas", "maintenance"}
    if (
        {key: value for key, value in old_manifest.items() if key not in ignored}
        != {key: value for key, value in new_manifest.items() if key not in ignored}
    ):
        return None
    old_replicas = old_manifest.get("replicas")
    new_replicas = new_manifest.get("replicas")
    if not isinstance(old_replicas, Mapping) or not isinstance(new_replicas, Mapping):
        return None
    if set(old_replicas) != set(new_replicas):
        return None
    for name, proofs in old_replicas.items():
        if not isinstance(proofs, list) or not isinstance(new_replicas[name], list):
            return None
        if [
            item for index, item in enumerate(proofs)
            if index != report_index
        ] != new_replicas[name]:
            return None
    return report_item


def parse_local_receipt(raw: bytes, archive_date: str, manifest_sha256: str) -> dict[str, Any]:
    if len(raw) > 32 * 1024 * 1024:
        raise RuntimeError("验证凭据超过安全大小")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("验证凭据格式无效") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("验证凭据格式无效")
    if (
        payload.get("contract_version") != LOCAL_RECEIPT_CONTRACT
        or payload.get("status") != "verified"
        or str(payload.get("archive_date") or "") != archive_date
        or str(payload.get("manifest_sha256") or "") != manifest_sha256
    ):
        raise RuntimeError("验证凭据与 manifest 不匹配")
    return payload
