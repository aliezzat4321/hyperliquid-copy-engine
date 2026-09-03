"""Filesystem metrics with the same semantics as ``df -P``."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskUsage:
    capacity_df: int
    used: int
    available: int
    used_pct: float


def disk_usage(path: str | Path) -> DiskUsage:
    stat = os.statvfs(path)
    block_size = stat.f_frsize
    used = (stat.f_blocks - stat.f_bfree) * block_size
    available = stat.f_bavail * block_size
    capacity = used + available
    return DiskUsage(capacity, used, available, 100.0 * used / capacity if capacity else 100.0)
