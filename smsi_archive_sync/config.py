from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .secrets import protect_password, unprotect_password
from .themes import DEFAULT_THEME_NAME, normalize_theme_name


APP_DATA_NAME = "SMSIArchiveSync"
CONFIG_VERSION = 1
REMOTE_ROOT = "/data/smsi-archive"
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
# A pasted token is the most likely alerting mistake, so its shape is refused
# here instead of being discovered as a rejected message hours later.
TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,64}$")
TELEGRAM_CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{5,32})$")
ALERT_LEVELS = ("warning", "error")


def app_data_dir() -> Path:
    override = os.getenv("SMSI_ARCHIVE_SYNC_DATA", "").strip()
    if override:
        root = Path(override).expanduser()
    else:
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local")
        root /= APP_DATA_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def default_local_root() -> Path:
    drive = Path("D:/")
    if drive.exists():
        return drive / "SMSI-Archive"
    return Path.home() / "Documents" / "SMSI-Archive"


def validate_alert_token(token: str) -> str:
    """Check a pasted token's shape before it is encrypted and stored.

    Once protected the blob cannot be inspected, so this is the only chance to
    catch a truncated paste - otherwise the mistake surfaces hours later as a
    message Telegram silently refused.
    """
    text = str(token or "").strip()
    if text and not TELEGRAM_TOKEN_RE.fullmatch(text):
        raise ValueError("Telegram 机器人令牌格式无效，应形如 123456789:AA…")
    return text


@dataclass
class AppConfig:
    config_version: int = CONFIG_VERSION
    host: str = "192.168.2.240"
    port: int = 22
    username: str = "ubuntu"
    password_blob: str = ""
    host_fingerprint: str = ""
    remote_root: str = REMOTE_ROOT
    local_root: str = ""
    minimum_free_gib: int = 10
    download_workers: int = 2
    bandwidth_mib: int = 20
    auto_sync_enabled: bool = False
    floating_window_enabled: bool = False
    alert_enabled: bool = False
    alert_token_blob: str = ""
    alert_chat_id: str = ""
    alert_min_level: str = "warning"
    alert_stale_hours: int = 6
    alert_upstream_lag_days: int = 3
    theme: str = DEFAULT_THEME_NAME

    def __post_init__(self) -> None:
        if not self.local_root:
            self.local_root = str(default_local_root())
        self.remote_root = REMOTE_ROOT
        # Normalised here rather than validated: an unknown theme is the one
        # setting that must never make the file unloadable, because load() turns
        # any validation error into a refusal to start.  A name this build does
        # not have is a downgrade or a hand edit, not a reason to lock the user
        # out of the window that would let them fix it.
        self.theme = normalize_theme_name(self.theme)

    @property
    def local_path(self) -> Path:
        return Path(self.local_root).expanduser().resolve()

    def password(self) -> str:
        return unprotect_password(self.password_blob) if self.password_blob else ""

    def set_password(self, password: str) -> None:
        self.password_blob = protect_password(password) if password else ""

    def alert_token(self) -> str:
        return unprotect_password(self.alert_token_blob) if self.alert_token_blob else ""

    def set_alert_token(self, token: str) -> None:
        self.alert_token_blob = protect_password(token) if token else ""

    def _alert_errors(self) -> list[str]:
        """Validated without decrypting: a damaged token must not block startup."""
        errors: list[str] = []
        if self.alert_min_level not in ALERT_LEVELS:
            errors.append("告警级别必须是 warning 或 error")
        if not 1 <= int(self.alert_stale_hours) <= 168:
            errors.append("无成功扫描告警阈值必须为 1 至 168 小时")
        if not 2 <= int(self.alert_upstream_lag_days) <= 14:
            errors.append("数据源停更告警阈值必须为 2 至 14 天")
        chat_id = self.alert_chat_id.strip()
        if chat_id and not TELEGRAM_CHAT_RE.fullmatch(chat_id):
            errors.append("Telegram 会话 ID 无效")
        if self.alert_enabled and not (self.alert_token_blob and chat_id):
            errors.append("启用告警前必须填写机器人令牌与会话 ID")
        return errors

    def validate(self, *, require_connection: bool = True) -> list[str]:
        errors: list[str] = []
        if self.config_version != CONFIG_VERSION:
            errors.append("配置版本不受支持")
        if not HOST_RE.fullmatch(self.host.strip()):
            errors.append("Ubuntu 地址无效")
        if not 1 <= int(self.port) <= 65535:
            errors.append("SSH 端口无效")
        if not USER_RE.fullmatch(self.username.strip()):
            errors.append("SSH 用户名无效")
        if require_connection:
            try:
                if not self.password():
                    errors.append("请输入 Ubuntu SSH 密码")
            except RuntimeError as exc:
                errors.append(str(exc))
            if not self.host_fingerprint.strip():
                errors.append("尚未确认 Ubuntu 主机指纹")
            elif not FINGERPRINT_RE.fullmatch(self.host_fingerprint.strip()):
                errors.append("Ubuntu 主机指纹格式无效")
        if self.remote_root != REMOTE_ROOT:
            errors.append("远端根目录必须是 /data/smsi-archive")
        if not self.local_root.strip():
            errors.append("请选择 Windows 本地归档目录")
        if not 1 <= int(self.minimum_free_gib) <= 1024:
            errors.append("磁盘保留空间必须为 1 至 1024 GiB")
        if not 1 <= int(self.download_workers) <= 4:
            errors.append("同步并发必须为 1 至 4")
        if int(self.bandwidth_mib) not in {0, 5, 10, 20, 50}:
            errors.append("总带宽限制无效")
        errors.extend(self._alert_errors())
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AppConfig":
        bandwidth = 20 if "bandwidth_mib" not in value else int(value.get("bandwidth_mib") or 0)
        return cls(
            config_version=int(value.get("config_version") or CONFIG_VERSION),
            host=str(value.get("host") or "192.168.2.240").strip(),
            port=int(value.get("port") or 22),
            username=str(value.get("username") or "ubuntu").strip(),
            password_blob=str(value.get("password_blob") or "").strip(),
            host_fingerprint=str(value.get("host_fingerprint") or "").strip(),
            remote_root=REMOTE_ROOT,
            local_root=str(value.get("local_root") or default_local_root()),
            minimum_free_gib=int(value.get("minimum_free_gib") or 10),
            download_workers=int(value.get("download_workers") or 2),
            bandwidth_mib=bandwidth,
            auto_sync_enabled=bool(value.get("auto_sync_enabled", False)),
            floating_window_enabled=bool(value.get("floating_window_enabled", False)),
            alert_enabled=bool(value.get("alert_enabled", False)),
            alert_token_blob=str(value.get("alert_token_blob") or "").strip(),
            alert_chat_id=str(value.get("alert_chat_id") or "").strip(),
            alert_min_level=str(value.get("alert_min_level") or "warning").strip(),
            alert_stale_hours=int(value.get("alert_stale_hours") or 6),
            alert_upstream_lag_days=int(value.get("alert_upstream_lag_days") or 3),
            theme=str(value.get("theme") or DEFAULT_THEME_NAME),
        )


class ConfigStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or app_data_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "config.json"

    def load(self) -> AppConfig:
        if not self.path.exists():
            config = AppConfig()
            self.save(config)
            return config
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Windows 客户端配置文件损坏") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("Windows 客户端配置格式无效")
        config = AppConfig.from_mapping(payload)
        errors = config.validate(require_connection=False)
        if errors:
            raise RuntimeError("客户端配置无效：" + "；".join(errors))
        return config

    def save(self, config: AppConfig) -> None:
        errors = config.validate(require_connection=False)
        if errors:
            raise ValueError("；".join(errors))
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
