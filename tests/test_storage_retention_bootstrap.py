from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "storage_retention_apply.py"
SPEC = importlib.util.spec_from_file_location("storage_retention_bootstrap_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _candidate(tmp_path: Path, name: str, size: int) -> tuple[Path, object]:
    market = tmp_path / "hyperliquid" / "market-shadow"
    path = market / "date=2026-08-20" / f"coin={name}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "sample.jsonl").write_bytes(b"x")
    st = path.lstat()
    candidate = MODULE.Candidate(
        path=path,
        day="2026-08-20",
        coin=name,
        canonical_coin=name,
        bytes_planned=size,
        device=st.st_dev,
        inode=st.st_ino,
    )
    return market, candidate


def test_bootstrap_refuses_unreachable_objective_before_deletion(tmp_path, monkeypatch):
    market, candidate = _candidate(tmp_path, "DOGE", 100)
    monkeypatch.setattr(
        MODULE,
        "disk_usage",
        lambda _: SimpleNamespace(
            capacity_df=10 * MODULE.GIB,
            used=9 * MODULE.GIB,
            available=100,
            used_pct=90.0,
        ),
    )
    deleted = []
    monkeypatch.setattr(MODULE.shutil, "rmtree", lambda path: deleted.append(path))
    MODULE.shutil.rmtree.avoids_symlink_attacks = True

    with pytest.raises(ValueError, match="bootstrap objective; refusing before deletion"):
        MODULE.apply_bootstrap_candidates(
            [candidate],
            mount=tmp_path,
            market_root=market,
            bootstrap_free_bytes=MODULE.MIN_BOOTSTRAP_FREE_BYTES,
            apply=True,
        )
    assert deleted == []
    assert candidate.path.exists()


def test_bootstrap_dry_run_stops_after_enough_reviewed_bytes(tmp_path, monkeypatch):
    market, first = _candidate(tmp_path, "DOGE", 700 * MODULE.MIB)
    _, second = _candidate(tmp_path, "SOL", 700 * MODULE.MIB)
    free_before = 100 * MODULE.MIB
    monkeypatch.setattr(
        MODULE,
        "disk_usage",
        lambda _: SimpleNamespace(
            capacity_df=10 * MODULE.GIB,
            used=9 * MODULE.GIB,
            available=free_before,
            used_pct=90.0,
        ),
    )
    monkeypatch.setattr(
        MODULE,
        "_revalidate_identity",
        lambda candidate, _: candidate.bytes_planned,
    )

    result = MODULE.apply_bootstrap_candidates(
        [first, second],
        mount=tmp_path,
        market_root=market,
        bootstrap_free_bytes=MODULE.MIN_BOOTSTRAP_FREE_BYTES,
        apply=False,
    )

    assert result["target_reached"] is True
    assert result["final_exit_gate"] is False
    assert result["partitions_processed"] == 1
    assert result["planned_bytes_processed"] == first.bytes_planned
    assert result["after_available_bytes"] >= MODULE.MIN_BOOTSTRAP_FREE_BYTES
    assert result["compress_candidates_deleted"] == 0
    assert first.path.exists()
    assert second.path.exists()


def test_bootstrap_rejects_objective_above_hard_cap(tmp_path):
    market, candidate = _candidate(tmp_path, "DOGE", MODULE.GIB)
    with pytest.raises(ValueError, match="between 512 MiB and 4 GiB"):
        MODULE.apply_bootstrap_candidates(
            [candidate],
            mount=tmp_path,
            market_root=market,
            bootstrap_free_bytes=MODULE.MAX_BOOTSTRAP_FREE_BYTES + 1,
            apply=False,
        )


def test_bootstrap_mode_never_claims_final_storage_exit_gate(tmp_path, monkeypatch):
    market, candidate = _candidate(tmp_path, "DOGE", MODULE.GIB)
    free_before = MODULE.GIB
    monkeypatch.setattr(
        MODULE,
        "disk_usage",
        lambda _: SimpleNamespace(
            capacity_df=10 * MODULE.GIB,
            used=9 * MODULE.GIB,
            available=free_before,
            used_pct=90.0,
        ),
    )
    monkeypatch.setattr(
        MODULE,
        "_revalidate_identity",
        lambda candidate, _: candidate.bytes_planned,
    )

    result = MODULE.apply_bootstrap_candidates(
        [candidate],
        mount=tmp_path,
        market_root=market,
        bootstrap_free_bytes=MODULE.MIN_BOOTSTRAP_FREE_BYTES,
        apply=False,
    )
    assert result["target_reached"] is True
    assert result["final_exit_gate"] is False
    assert result["target_used_pct"] is None
