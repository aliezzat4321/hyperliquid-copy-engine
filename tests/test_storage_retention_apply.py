from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.storage_retention_apply import validate_manifest

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)


def _manifest(path: Path, *, action: str = "DELETE_CANDIDATE", canonical: str = "DOGE") -> dict:
    return {
        "mode": "DRY_RUN_ONLY_NO_DELETION",
        "generated_at": "2026-08-31T13:55:00+00:00",
        "real_trading": False,
        "recent_days_kept_full_fidelity": 3,
        "normalization": {"robust_alias_safety_passed": True},
        "funnel": {"robust_coins": ["BTC", "XYZ:KORU"]},
        "market_shadow": {
            "partitions": [
                {
                    "path": str(path),
                    "date": "2026-08-27",
                    "coin_dir": "DOGE",
                    "canonical_coin": canonical,
                    "bytes": 1234,
                    "action": action,
                }
            ]
        },
        "safety": {
            "deletion_performed": False,
            "postgres_filesystem_deletion_allowed": False,
            "robust_alias_safety_passed": True,
        },
    }


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    market = tmp_path / "hyperliquid" / "market-shadow"
    candidate = market / "date=2026-08-27" / "coin=DOGE"
    candidate.mkdir(parents=True)
    return market, candidate


def test_accepts_only_direct_old_delete_candidate(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    rows = validate_manifest(
        _manifest(candidate),
        manifest_path=tmp_path / "manifest.json",
        market_root=market,
        max_age_minutes=15,
        now=NOW,
    )
    assert len(rows) == 1
    assert rows[0].path == candidate.resolve()


def test_rejects_stale_manifest(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["generated_at"] = "2026-08-31T12:00:00+00:00"
    with pytest.raises(ValueError, match="not fresh"):
        validate_manifest(
            manifest,
            manifest_path=tmp_path / "manifest.json",
            market_root=market,
            max_age_minutes=15,
            now=NOW,
        )


def test_rejects_robust_coin_even_if_manifest_marks_delete(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    with pytest.raises(ValueError, match="is robust"):
        validate_manifest(
            _manifest(candidate, canonical="BTC"),
            manifest_path=tmp_path / "manifest.json",
            market_root=market,
            max_age_minutes=15,
            now=NOW,
        )


def test_rejects_recent_partition(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    recent = market / "date=2026-08-31" / "coin=DOGE"
    recent.mkdir(parents=True)
    manifest = _manifest(recent)
    manifest["market_shadow"]["partitions"][0]["date"] = "2026-08-31"
    with pytest.raises(ValueError, match="recent protection"):
        validate_manifest(
            manifest,
            manifest_path=tmp_path / "manifest.json",
            market_root=market,
            max_age_minutes=15,
            now=NOW,
        )


def test_rejects_path_escape(tmp_path: Path) -> None:
    market, _ = _layout(tmp_path)
    outside = tmp_path / "postgresql" / "date=2026-08-27" / "coin=DOGE"
    outside.mkdir(parents=True)
    with pytest.raises(ValueError, match="escapes direct market partition"):
        validate_manifest(
            _manifest(outside),
            manifest_path=tmp_path / "manifest.json",
            market_root=market,
            max_age_minutes=15,
            now=NOW,
        )


def test_rejects_non_delete_action(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    with pytest.raises(ValueError, match="no DELETE_CANDIDATE"):
        validate_manifest(
            _manifest(candidate, action="COMPRESS_CANDIDATE"),
            manifest_path=tmp_path / "manifest.json",
            market_root=market,
            max_age_minutes=15,
            now=NOW,
        )


def test_rejects_failed_alias_safety(tmp_path: Path) -> None:
    market, candidate = _layout(tmp_path)
    manifest = _manifest(candidate)
    manifest["safety"]["robust_alias_safety_passed"] = False
    with pytest.raises(ValueError, match="robust alias safety"):
        validate_manifest(
            manifest,
            manifest_path=tmp_path / "manifest.json",
            market_root=market,
            max_age_minutes=15,
            now=NOW,
        )
