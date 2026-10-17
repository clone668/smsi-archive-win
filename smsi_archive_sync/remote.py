from __future__ import annotations

import base64
import hashlib
import posixpath
import shlex
import socket
import stat
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Callable

import paramiko

from .config import AppConfig
from .protocol import OperationCancelled


TRANSFER_CHUNK_BYTES = 1024 * 1024
TRANSFER_IDLE_TIMEOUT_SECONDS = 30


class HostKeyRequired(RuntimeError):
    def __init__(self, fingerprint: str, key_type: str) -> None:
        super().__init__(f"需要确认 Ubuntu 主机指纹: {fingerprint}")
        self.fingerprint = fingerprint
        self.key_type = key_type


class HostKeyMismatch(RuntimeError):
    pass


def key_fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str) -> None:
        self.expected = expected.strip()

    def missing_host_key(self, _client, _hostname, key: paramiko.PKey) -> None:
        observed = key_fingerprint(key)
        if not self.expected:
            raise HostKeyRequired(observed, key.get_name())
        if observed != self.expected:
            raise HostKeyMismatch(
                f"Ubuntu 主机指纹不匹配，期望 {self.expected}，实际 {observed}"
            )


def safe_remote_path(root: str, *parts: str) -> str:
    root_path = PurePosixPath(root)
    if not root_path.is_absolute():
        raise RuntimeError("Ubuntu 根目录必须是绝对路径")
    clean: list[str] = []
    for value in parts:
        path = PurePosixPath(str(value).strip("/"))
        if path.is_absolute() or ".." in path.parts or "\\" in str(value):
            raise RuntimeError("Ubuntu 文件路径越界")
        clean.extend(path.parts)
    return posixpath.join(root_path.as_posix(), *clean)


class RemoteArchiveClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.ssh: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None

    def connect(self) -> "RemoteArchiveClient":
        errors = self.config.validate(require_connection=False)
        try:
            if not self.config.password():
                errors.append("请输入 Ubuntu SSH 密码")
        except RuntimeError as exc:
            errors.append(str(exc))
        if errors:
            raise RuntimeError("；".join(errors))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(PinnedHostKeyPolicy(self.config.host_fingerprint))
        try:
            client.connect(
                hostname=self.config.host,
                port=self.config.port,
                username=self.config.username,
                password=self.config.password(),
                timeout=12,
                banner_timeout=12,
                auth_timeout=15,
                allow_agent=False,
                look_for_keys=False,
            )
            sftp = client.open_sftp()
            attributes = sftp.stat(self.config.remote_root)
            if not stat.S_ISDIR(attributes.st_mode):
                raise RuntimeError("Ubuntu 归档根路径不是目录")
        except Exception:
            client.close()
            raise
        self.ssh = client
        self.sftp = sftp
        return self

    def close(self) -> None:
        if self.sftp is not None:
            self.sftp.close()
            self.sftp = None
        if self.ssh is not None:
            self.ssh.close()
            self.ssh = None

    def __enter__(self) -> "RemoteArchiveClient":
        return self.connect()

    def __exit__(self, *_args) -> None:
        self.close()

    def _require_sftp(self) -> paramiko.SFTPClient:
        if self.sftp is None:
            raise RuntimeError("Ubuntu SFTP 尚未连接")
        return self.sftp

    def _require_ssh(self) -> paramiko.SSHClient:
        if self.ssh is None:
            raise RuntimeError("Ubuntu SSH 尚未连接")
        return self.ssh

    def list_directories(self, path: str) -> list[str]:
        result: list[str] = []
        for item in self._require_sftp().listdir_attr(path):
            if stat.S_ISDIR(item.st_mode) and item.filename not in {".", ".."}:
                result.append(item.filename)
        return sorted(result, key=str.casefold)

    def read_small(self, path: str, maximum_bytes: int, *, missing_ok: bool = False) -> bytes | None:
        sftp = self._require_sftp()
        try:
            attributes = sftp.stat(path)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        if attributes.st_size > maximum_bytes:
            raise RuntimeError(f"Ubuntu 文件超过安全大小: {path}")
        with sftp.file(path, "rb") as handle:
            payload = handle.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise RuntimeError(f"Ubuntu 文件超过安全大小: {path}")
        return bytes(payload)

    def download(
        self,
        remote_path: str,
        destination: Path,
        expected_size: int,
        *,
        cancel: threading.Event,
        limiter: "BandwidthLimiter",
        progress: Callable[[int], None],
    ) -> int:
        sftp = self._require_sftp()
        attributes = sftp.stat(remote_path)
        if int(attributes.st_size) != int(expected_size):
            raise RuntimeError(f"Ubuntu 文件大小与 manifest 不一致: {remote_path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        offset = destination.stat().st_size if destination.exists() else 0
        if offset < 0 or offset > expected_size:
            destination.unlink(missing_ok=True)
            offset = 0
        mode = "ab" if offset else "wb"
        progress(offset)
        command = f"tail -c +{offset + 1} -- {shlex.quote(remote_path)}"
        stdin, stdout, stderr = self._require_ssh().exec_command(command, timeout=12)
        channel = stdout.channel
        channel.settimeout(1.0)
        last_received = time.monotonic()
        try:
            stdin.close()
            with destination.open(mode) as target:
                current = offset
                while current < expected_size:
                    if cancel.is_set():
                        channel.close()
                        raise OperationCancelled("同步已取消，未完成文件保留以便继续")
                    try:
                        chunk = channel.recv(
                            min(TRANSFER_CHUNK_BYTES, expected_size - current)
                        )
                    except socket.timeout:
                        if time.monotonic() - last_received >= TRANSFER_IDLE_TIMEOUT_SECONDS:
                            channel.close()
                            raise RuntimeError(f"Ubuntu 文件读取超时: {remote_path}")
                        continue
                    if not chunk:
                        break
                    last_received = time.monotonic()
                    limiter.consume(len(chunk), cancel)
                    target.write(chunk)
                    current += len(chunk)
                    progress(current)
            exit_status = channel.recv_exit_status()
            error = stderr.read(4097)
            if exit_status != 0:
                detail = error[:4096].decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"Ubuntu 文件读取失败: {remote_path}"
                    + (f" ({detail})" if detail else "")
                )
        finally:
            stdout.close()
            stderr.close()
            channel.close()
        return destination.stat().st_size


class BandwidthLimiter:
    """Thread-safe global token bucket shared by all active transfers."""

    def __init__(self, mib_per_second: int) -> None:
        self.rate = max(0, int(mib_per_second)) * 1024 * 1024
        self.capacity = max(self.rate, 1024 * 1024)
        self.tokens = float(self.capacity)
        self.updated = __import__("time").monotonic()
        self.lock = threading.Lock()

    def consume(self, size: int, cancel: threading.Event) -> None:
        if self.rate <= 0:
            return
        import time

        remaining = float(size)
        while remaining > 0:
            if cancel.is_set():
                raise OperationCancelled("同步已取消，未完成文件保留以便继续")
            wait_for = 0.0
            with self.lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self.updated)
                self.tokens = min(float(self.capacity), self.tokens + elapsed * self.rate)
                self.updated = now
                consumed = min(self.tokens, remaining)
                self.tokens -= consumed
                remaining -= consumed
                if remaining > 0:
                    wait_for = min(0.2, remaining / self.rate)
            if wait_for:
                time.sleep(wait_for)
