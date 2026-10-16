from __future__ import annotations

import re
import stat
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from smsi_archive_sync.config import AppConfig
from smsi_archive_sync.remote import BandwidthLimiter, RemoteArchiveClient


def test_supplement_inventory_rejects_links_and_access_errors():
    client = RemoteArchiveClient(AppConfig())
    name = "00000000000000000001-" + "a" * 64 + ".json"
    client.sftp = SimpleNamespace(listdir_attr=lambda _: [
        SimpleNamespace(filename=name, st_mode=stat.S_IFREG),
        SimpleNamespace(filename=name + "." + "b" * 32 + ".tmp", st_mode=stat.S_IFREG)])
    assert client.list_delivery_files("/archive/dependencies-v1/deliveries") == [name]
    client.sftp.listdir_attr = lambda _: [SimpleNamespace(filename=name, st_mode=stat.S_IFLNK)]
    with pytest.raises(RuntimeError, match="非普通文件"):
        client.list_delivery_files("/archive/dependencies-v1/deliveries")
    def fail(_):
        raise PermissionError("denied")
    client.sftp.listdir_attr = fail
    with pytest.raises(PermissionError):
        client.list_delivery_files("/archive/dependencies-v1/deliveries")


class FakeChannel:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.position = 0
        self.timeout = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, size: int) -> bytes:
        chunk = self.payload[self.position : self.position + size]
        self.position += len(chunk)
        return chunk

    def recv_exit_status(self) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


class FakeChannelFile:
    def __init__(self, channel: FakeChannel, payload: bytes = b"") -> None:
        self.channel = channel
        self.payload = payload

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]

    def close(self) -> None:
        return None


class FakeStdin:
    def close(self) -> None:
        return None


class FakeSftp:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def stat(self, _path: str):
        return SimpleNamespace(st_size=len(self.payload))


class FakeSsh:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.commands: list[str] = []
        self.channels: list[FakeChannel] = []

    def exec_command(self, command: str, timeout: int):
        self.commands.append(command)
        match = re.match(r"^tail -c \+(\d+) -- ", command)
        assert match is not None
        offset = int(match.group(1)) - 1
        channel = FakeChannel(self.payload[offset:])
        self.channels.append(channel)
        return (
            FakeStdin(),
            FakeChannelFile(channel),
            FakeChannelFile(channel),
        )


def test_download_uses_ssh_stream_and_preserves_resume(tmp_path: Path) -> None:
    payload = (b"0123456789abcdef" * 131_072) + b"complete"
    destination = tmp_path / "archive.partial"
    resume_at = 777_777
    destination.write_bytes(payload[:resume_at])
    client = RemoteArchiveClient(AppConfig())
    client.sftp = FakeSftp(payload)
    ssh = FakeSsh(payload)
    client.ssh = ssh
    progress: list[int] = []

    observed = client.download(
        "/data/example file.parquet",
        destination,
        len(payload),
        cancel=Event(),
        limiter=BandwidthLimiter(0),
        progress=progress.append,
    )

    assert observed == len(payload)
    assert destination.read_bytes() == payload
    assert progress[0] == resume_at
    assert progress[-1] == len(payload)
    assert ssh.commands == [
        f"tail -c +{resume_at + 1} -- '/data/example file.parquet'"
    ]
    assert ssh.channels[0].timeout == 1.0
    assert ssh.channels[0].closed is True
