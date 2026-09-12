from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
MODULE_PATH = SCRIPTS / "p0_90_storage_apply.py"
SPEC = importlib.util.spec_from_file_location("p0_90_storage_apply_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _candidate(tmp_path: Path, name: str, size: int):
    path = tmp_path / "market-shadow" / "date=2026-08-20" / f"coin={name}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "sample").write_bytes(b"x")
    st = path.lstat()
    return MODULE.retention.Candidate(
        path=path,
        day="2026-08-20",
        coin=name,
        canonical_coin=name,
        bytes_planned=size,
        device=st.st_dev,
        inode=st.st_ino,
    )


def _plan(candidate, manifest_sha="a" * 64, code_sha="b" * 40):
    return {
        "mode": "DRY_RUN_BOOTSTRAP_HEADROOM",
        "apply": False,
        "final_exit_gate": False,
        "target_reached": True,
        "bootstrap_free_bytes": MODULE.retention.MIN_BOOTSTRAP_FREE_BYTES,
        "manifest_sha256": manifest_sha,
        "code_sha": code_sha,
        "postgresql_filesystem_deletion": False,
        "polymarket_mutation": False,
        "real_trading_changed": False,
        "partitions_processed": 1,
        "planned_bytes_processed": candidate.bytes_planned,
        "processed": [
            {
                "path": str(candidate.path),
                "date": candidate.day,
                "canonical_coin": candidate.canonical_coin,
                "planned_bytes": candidate.bytes_planned,
                "observed_file_bytes": candidate.bytes_planned,
                "device": candidate.device,
                "inode": candidate.inode,
            }
        ],
    }


def test_exact_plan_accepts_reviewed_deterministic_prefix(tmp_path):
    candidate = _candidate(tmp_path, "DOGE", 123)
    bound = MODULE._bind_exact_plan(
        [candidate],
        plan=_plan(candidate),
        manifest_sha256="a" * 64,
        code_sha="b" * 40,
        bootstrap_free_bytes=MODULE.retention.MIN_BOOTSTRAP_FREE_BYTES,
    )
    assert bound == [candidate]


def test_exact_plan_rejects_nonprefix_candidate_before_apply(tmp_path):
    first = _candidate(tmp_path, "DOGE", 123)
    second = _candidate(tmp_path, "SOL", 456)
    plan = _plan(second)
    with pytest.raises(ValueError, match="not the validated deterministic prefix"):
        MODULE._bind_exact_plan(
            [first, second],
            plan=plan,
            manifest_sha256="a" * 64,
            code_sha="b" * 40,
            bootstrap_free_bytes=MODULE.retention.MIN_BOOTSTRAP_FREE_BYTES,
        )


def test_exact_plan_rejects_inode_change(tmp_path):
    candidate = _candidate(tmp_path, "DOGE", 123)
    plan = _plan(candidate)
    plan["processed"][0]["inode"] += 1
    with pytest.raises(ValueError, match="filesystem identity changed"):
        MODULE._bind_exact_plan(
            [candidate],
            plan=plan,
            manifest_sha256="a" * 64,
            code_sha="b" * 40,
            bootstrap_free_bytes=MODULE.retention.MIN_BOOTSTRAP_FREE_BYTES,
        )


def test_exact_plan_rejects_manifest_hash_mismatch(tmp_path):
    candidate = _candidate(tmp_path, "DOGE", 123)
    with pytest.raises(ValueError, match="retention-manifest SHA mismatch"):
        MODULE._bind_exact_plan(
            [candidate],
            plan=_plan(candidate),
            manifest_sha256="c" * 64,
            code_sha="b" * 40,
            bootstrap_free_bytes=MODULE.retention.MIN_BOOTSTRAP_FREE_BYTES,
        )
