from __future__ import annotations

from types import SimpleNamespace

from hlcopy.storage import metrics


def test_disk_usage_matches_df_semantics_with_reserved_blocks(monkeypatch):
    # 1000 total, 400 privileged-free, but only 350 available to this process.
    monkeypatch.setattr(metrics.os, "statvfs", lambda _: SimpleNamespace(
        f_blocks=1000, f_bfree=400, f_bavail=350, f_frsize=4096))
    usage = metrics.disk_usage("/synthetic")
    assert usage.used == 600 * 4096
    assert usage.available == 350 * 4096
    assert usage.capacity_df == 950 * 4096
    assert usage.used_pct == 100 * 600 / 950
