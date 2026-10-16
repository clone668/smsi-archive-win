from __future__ import annotations

from pathlib import Path

from .dependency_contract import PREFIX, safe_key
from .remote import safe_remote_path


class DependencySource:
    def __init__(self, client, remote_root: str, node: str, *, transfer_client=None, cancel=None, limiter=None):
        self.client = client
        self.root = safe_remote_path(remote_root, "collector=" + node)
        self.transfer_client = transfer_client
        self.cancel, self.limiter = cancel, limiter

    def list_deliveries(self):
        names = self.client.list_delivery_files(safe_remote_path(self.root, PREFIX, "deliveries"))
        return [PREFIX + "/deliveries/" + name for name in names]

    def read_small(self, key: str, maximum: int):
        return self.client.read_small(safe_remote_path(self.root, safe_key(key)), maximum, missing_ok=True)

    def download(self, key: str, target: Path, size: int, progress):
        return self.transfer_client().download(safe_remote_path(self.root, safe_key(key)), target, size,
            cancel=self.cancel, limiter=self.limiter, progress=progress)
