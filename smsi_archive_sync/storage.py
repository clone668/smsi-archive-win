"""Free space on the Windows archive volume, and what it means for a sync.

This client writes the last copy in the chain onto one volume and runs
unattended for weeks, so a volume that quietly fills up ends the archive with a
write error somewhere in the middle of an object - the least useful moment to
find out.  Space is therefore decided before the first byte of a day is
written, and reported while there is still room to act.

Nothing here deletes anything.  Reclaiming space stays the user's decision:
this module only says how much is left and what the client itself is holding.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


GIB = 1024**3


def bytes_text(value: Any) -> str:
    number = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    precision = 0 if number >= 10 or index == 0 else 1
    return f"{number:.{precision}f} {units[index]}"


def usable_root(path: Path | str) -> Path:
    """The nearest existing ancestor of ``path``.

    The archive directory does not exist yet on a first run, but the volume
    holding it does, and that is what has the free space.
    """
    current = Path(path).expanduser()
    for candidate in (current, *current.parents):
        if candidate.exists():
            return candidate
    return current


def free_bytes(path: Path | str) -> int:
    """Free bytes on the volume holding ``path``; raises OSError if unreadable."""
    return int(shutil.disk_usage(usable_root(path)).free)


def reserve_bytes(minimum_free_gib: Any) -> int:
    """The margin the volume must keep after a sync finishes."""
    return max(1, int(minimum_free_gib or 0)) * GIB


def directory_bytes(path: Path | str) -> int:
    """Bytes held under ``path``, best effort.

    Only used when reporting a shortage: walking a directory to draw an
    ordinary screen would cost more than the number is worth.
    """
    total = 0
    try:
        for entry in Path(path).rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def disk_status(local_root: Path | str, minimum_free_gib: Any) -> dict[str, Any]:
    """One reading of the archive volume, shared by the UI and by alerting.

    ``ok`` false means the volume could not be read at all.  A disconnected
    archive disk is a different and worse fault than a full one, so the two are
    reported separately rather than collapsed into "no space".
    """
    reserve = reserve_bytes(minimum_free_gib)
    try:
        usage = shutil.disk_usage(usable_root(local_root))
    except OSError as exc:
        return {
            "ok": False,
            "error": str(exc)[:200],
            "free_bytes": 0,
            "total_bytes": 0,
            "reserve_bytes": reserve,
            "low": False,
        }
    return {
        "ok": True,
        "error": "",
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
        "reserve_bytes": reserve,
        "low": int(usage.free) < reserve,
    }
