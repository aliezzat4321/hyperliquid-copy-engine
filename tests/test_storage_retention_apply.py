from __future__ import annotations

import importlib.util
import sys
from types import SimpleNamespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "storage_retention_apply.py"
SPEC = importlib.util.spec_from_file_location("storage_retention_apply_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
validate_manifest = MODULE.validate_manifest

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


def _manifest(
    path: Path,
    *,
    action: str = "DELETE_CANDIDATE",
    canonical: str = "DOGE",
) -> dict:
    return {
        "mode": "DRY_RUN_ONLY_NO_DELETION",
        "generated_at": "2026-08-31T13:55:00+00:00",
        "real_trading": False,
        "recent_days_kept_full_fidelity": 3,
        "deletion_budget_bytes": 6 * 1024**3,
        "source_evidence": {"complete": True},
        "normalization": {
            "robust_alias_safety_passed": True,
            "profitability_protection_safety_passed": True,
        },
        "funnel": {
            "robust_coins": ["BTC", "XYZ:KORU"],
            "profitability_protected_coins": ["BTC", "ETH", "XYZ:KORU"],
        },
        "market_shadow": {
            "delete_candidate_count": 1,
            "recoverable_delete_candidate_bytes": 1234,
            "partitions": [
                {
                    "path": str(path),
                    "date": "2026-08-27",
                    "coin_dir": "DOGE",
                    "canonical_coin": canonical,
                    "bytes": 1234,
                    "action": action,
                }
            ],
        },
        "safety": {
            "deletion_performed": False,
            "postgres_filesystem_deletion_allowed": False,
            "apply_requires_separate_explicit_reviewed_manifest": True,
            "source_evidence_complete": True,
            "robust_set_nonempty": True,
            "profitability_protection_set_nonempty": True,
            "robust_alias_safety_passed": True,
            "profitability_protection_safety_passed": True,
        },
    }


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    market = tmp_path / "hyperliquid" / "market-shadow"
    candidate = market / "date=2026-08-27" / "coin=DOGE"
    candidate.mkdir(parents=True)
    (candidate / "sample.jsonl").write_text("{}\n", encoding="utf-8")
    return market, candidate


def _validate(manifest: dict, market: Path, tmp_path: Path):
    return validate_manifest(
        manifest,
        manifest_path=tmp_path / "manifest.json",
        market_root=market,
        max_age_minutes=15,
        now=NOW,
    )


def test_accepts_only_direct_old_delete_candidate(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    rows = _validate(_manifest(candidate), market, tmp_path)
    assert len(rows) == 1
    assert rows[0].path == candidate.resolve()


def test_rejects_stale_manifest(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["generated_at"] = "2026-08-31T12:00:00+00:00"
    with pytest.raises(ValueError, match="not fresh"):
        _validate(manifest, market, tmp_path)


def test_rejects_robust_coin_even_if_manifest_marks_delete(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    with pytest.raises(ValueError, match="is robust"):
        _validate(_manifest(candidate, canonical="BTC"), market, tmp_path)


def test_rejects_profitability_protected_coin_even_if_not_robust(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    with pytest.raises(ValueError, match="profitability evidence"):
        _validate(_manifest(candidate, canonical="ETH"), market, tmp_path)


def test_rejects_empty_robust_coin_evidence(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["funnel"]["robust_coins"] = []
    with pytest.raises(ValueError, match="non-empty reviewed list"):
        _validate(manifest, market, tmp_path)


def test_rejects_empty_profitability_protection_set(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["funnel"]["profitability_protected_coins"] = []
    with pytest.raises(ValueError, match="profitability_protected_coins"):
        _validate(manifest, market, tmp_path)


def test_rejects_profitability_set_that_omits_robust_coin(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["funnel"]["profitability_protected_coins"] = ["ETH"]
    with pytest.raises(ValueError, match="include robust coins"):
        _validate(manifest, market, tmp_path)


def test_rejects_missing_source_evidence(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["source_evidence"]["complete"] = False
    with pytest.raises(ValueError, match="source_evidence.complete"):
        _validate(manifest, market, tmp_path)


def test_rejects_recent_partition(tmp_path: Path) -> None:
    market, _ = _layout(tmp_path)
    recent = market / "date=2026-08-31" / "coin=DOGE"
    recent.mkdir(parents=True)
    manifest = _manifest(recent)
    manifest["market_shadow"]["partitions"][0]["date"] = "2026-08-31"
    with pytest.raises(ValueError, match="recent protection"):
        _validate(manifest, market, tmp_path)


def test_rejects_manifest_date_not_matching_path(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["market_shadow"]["partitions"][0]["date"] = "2026-08-26"
    with pytest.raises(ValueError, match="does not match partition path"):
        _validate(manifest, market, tmp_path)


def test_rejects_path_escape(tmp_path: Path) -> None:
    market, _ = _layout(tmp_path)
    outside = tmp_path / "postgresql" / "date=2026-08-27" / "coin=DOGE"
    outside.mkdir(parents=True)
    with pytest.raises(ValueError, match="not lexically below market root"):
        _validate(_manifest(outside), market, tmp_path)


def test_rejects_non_delete_action(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate, action="COMPRESS_CANDIDATE")
    manifest["market_shadow"]["delete_candidate_count"] = 0
    manifest["market_shadow"]["recoverable_delete_candidate_bytes"] = 0
    with pytest.raises(ValueError, match="no DELETE_CANDIDATE"):
        _validate(manifest, market, tmp_path)


def test_rejects_failed_alias_safety(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["safety"]["robust_alias_safety_passed"] = False
    with pytest.raises(ValueError, match="robust alias safety"):
        _validate(manifest, market, tmp_path)


def test_rejects_failed_profitability_protection_safety(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["safety"]["profitability_protection_safety_passed"] = False
    with pytest.raises(ValueError, match="profitability protection safety"):
        _validate(manifest, market, tmp_path)


def test_rejects_missing_separate_review_requirement(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["safety"]["apply_requires_separate_explicit_reviewed_manifest"] = False
    with pytest.raises(ValueError, match="separately reviewed apply step"):
        _validate(manifest, market, tmp_path)


def test_rejects_symlink_component(tmp_path: Path) -> None:
    market, _ = _layout(tmp_path)
    real_date = market / "real-date"
    real_date.mkdir()
    real_candidate = real_date / "coin=DOGE"
    real_candidate.mkdir()
    linked_date = market / "date=2026-08-27-linked"
    linked_date.symlink_to(real_date, target_is_directory=True)
    linked_candidate = linked_date / "coin=DOGE"
    with pytest.raises(ValueError, match="symlink component"):
        _validate(_manifest(linked_candidate), market, tmp_path)


def test_requires_three_recent_days_protected(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["recent_days_kept_full_fidelity"] = 2
    with pytest.raises(ValueError, match="at least 3"):
        _validate(manifest, market, tmp_path)


def test_rejects_delete_candidate_count_mismatch(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["market_shadow"]["delete_candidate_count"] = 2
    with pytest.raises(ValueError, match="count mismatch"):
        _validate(manifest, market, tmp_path)


def test_rejects_delete_candidate_byte_total_mismatch(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["market_shadow"]["recoverable_delete_candidate_bytes"] = 9999
    with pytest.raises(ValueError, match="byte total mismatch"):
        _validate(manifest, market, tmp_path)


def test_rejects_delete_pool_over_reviewed_budget(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["deletion_budget_bytes"] = 1000
    with pytest.raises(ValueError, match="exceeds reviewed budget"):
        _validate(manifest, market, tmp_path)


def test_accepts_reviewed_budget_larger_than_six_gib(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["deletion_budget_bytes"] = 12 * 1024**3
    rows = _validate(manifest, market, tmp_path)
    assert len(rows) == 1


def test_rejects_reviewed_budget_larger_than_twenty_four_gib(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["deletion_budget_bytes"] = 25 * 1024**3
    with pytest.raises(ValueError, match="<=24 GiB"):
        _validate(manifest, market, tmp_path)


def test_apply_refuses_unreachable_target_before_deletion(tmp_path, monkeypatch):
    market, candidate_path = _layout(tmp_path)
    candidate = MODULE.Candidate(
        path=candidate_path,
        day="2026-08-27",
        coin="DOGE",
        canonical_coin="DOGE",
        bytes_planned=100,
        device=candidate_path.lstat().st_dev,
        inode=candidate_path.lstat().st_ino,
    )
    monkeypatch.setattr(
        MODULE,
        "disk_usage",
        lambda _: SimpleNamespace(capacity_df=1000, used=900, available=100, used_pct=90),
    )
    deleted = []
    monkeypatch.setattr(MODULE.shutil, "rmtree", lambda path: deleted.append(path))
    MODULE.shutil.rmtree.avoids_symlink_attacks = True

    with pytest.raises(ValueError, match="refusing before deletion"):
        MODULE.apply_candidates(
            [candidate], mount=tmp_path, market_root=market,
            target_used_pct=75, apply=True,
        )
    assert deleted == []
    assert candidate_path.exists()
