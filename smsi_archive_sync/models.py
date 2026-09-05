from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


STATUS_LABELS = {
    "ready": "可同步",
    "verified": "验证通过",
    "remote_running": "Ubuntu 归档中",
    "remote_failed": "Ubuntu 归档失败",
    "waiting_manifest": "等待发布",
    "waiting_verification": "等待 Ubuntu 验证",
    "invalid": "远端资料无效",
    "remote_missing": "仅本地保留",
    "missing": "本地缺失",
    "partial": "同步未完成",
    "local_unverified": "本地未验证",
    "verification_failed": "本地校验失败",
    "manifest_changed": "远端清单已变化",
    "metadata_update": "待同步清单维护",
    "syncing": "同步中",
    "verifying": "校验中",
    "cancelled": "已取消",
    "error": "失败",
    "unknown": "未检查",
}


@dataclass
class ArchiveSummary:
    created_at: str = ""
    business_table_count: int = 0
    batch_count: int = 0
    ledger_record_count: int = 0
    unqualified_batch_count: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    window_start: str = ""
    window_end: str = ""
    remote_verified_count: int = 0
    ubuntu_verified_at: str = ""
    windows_verified_at: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ArchiveSummary":
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            created_at=str(value.get("created_at") or ""),
            business_table_count=int(value.get("business_table_count") or 0),
            batch_count=int(value.get("batch_count") or 0),
            ledger_record_count=int(value.get("ledger_record_count") or 0),
            unqualified_batch_count=int(value.get("unqualified_batch_count") or 0),
            kind_counts={
                str(key): int(count or 0)
                for key, count in (value.get("kind_counts") or {}).items()
            } if isinstance(value.get("kind_counts"), Mapping) else {},
            window_start=str(value.get("window_start") or ""),
            window_end=str(value.get("window_end") or ""),
            remote_verified_count=int(value.get("remote_verified_count") or 0),
            ubuntu_verified_at=str(value.get("ubuntu_verified_at") or ""),
            windows_verified_at=str(value.get("windows_verified_at") or ""),
        )


@dataclass
class RemoteObject:
    relative_key: str
    size_bytes: int
    sha256: str
    kind: str = ""
    table_name: str = ""
    row_count: int = 0
    schema_sha256: str = ""
    content_sha256: str = ""
    item_format: str = "parquet"
    report_contract_version: str = ""
    local_state: str = "missing"
    local_bytes: int = 0

    @classmethod
    def from_manifest(cls, value: Mapping[str, Any]) -> "RemoteObject":
        return cls(
            relative_key=str(value.get("relative_key") or ""),
            size_bytes=int(value.get("size_bytes") or 0),
            sha256=str(value.get("sha256") or ""),
            kind=str(value.get("kind") or ""),
            table_name=str(value.get("table_name") or ""),
            row_count=int(value.get("row_count") or 0),
            schema_sha256=str(value.get("schema_sha256") or ""),
            content_sha256=str(value.get("content_sha256") or ""),
            item_format=str(value.get("format") or "parquet"),
            report_contract_version=str(value.get("report_contract_version") or ""),
        )

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "relative_key": self.relative_key,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "kind": self.kind,
            "table_name": self.table_name,
            "row_count": self.row_count,
            "schema_sha256": self.schema_sha256,
            "content_sha256": self.content_sha256,
            "format": self.item_format,
            "report_contract_version": self.report_contract_version,
        }


@dataclass
class ArchiveDay:
    collector_id: str
    archive_date: str
    remote_status: str
    remote_detail: str = ""
    local_status: str = "missing"
    local_detail: str = ""
    local_verification_level: str = ""
    manifest_sha256: str = ""
    manifest_b64: str = ""
    object_count: int = 0
    row_count: int = 0
    bytes_total: int = 0
    bytes_local: int = 0
    objects: list[RemoteObject] = field(default_factory=list)
    summary: ArchiveSummary = field(default_factory=ArchiveSummary)

    @property
    def key(self) -> str:
        return f"{self.collector_id}/{self.archive_date}"

    @property
    def eligible(self) -> bool:
        return self.remote_status == "ready" and self.local_status in {
            "missing",
            "partial",
            "metadata_update",
        }

    @property
    def manifest_raw(self) -> bytes:
        return base64.b64decode(self.manifest_b64) if self.manifest_b64 else b""

    def set_manifest_raw(self, raw: bytes) -> None:
        self.manifest_b64 = base64.b64encode(raw).decode("ascii")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArchiveDay":
        payload = dict(value)
        payload["objects"] = [
            RemoteObject(**dict(item))
            for item in value.get("objects") or []
            if isinstance(item, Mapping)
        ]
        payload["summary"] = ArchiveSummary.from_mapping(value.get("summary"))
        return cls(**payload)


@dataclass
class ArchiveCatalog:
    scanned_at: str = ""
    remote_root: str = "/data/smsi-archive"
    days: list[ArchiveDay] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at,
            "remote_root": self.remote_root,
            "days": [day.to_dict() for day in self.days],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArchiveCatalog":
        return cls(
            scanned_at=str(value.get("scanned_at") or ""),
            remote_root=str(value.get("remote_root") or "/data/smsi-archive"),
            days=[
                ArchiveDay.from_dict(item)
                for item in value.get("days") or []
                if isinstance(item, Mapping)
            ],
        )
