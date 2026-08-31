from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "storage_value_audit.py"
SPEC = importlib.util.spec_from_file_location("storage_value_audit_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_market_partition_scan_aggregates_channel_coin_and_date(tmp_path: Path) -> None:
    market = tmp_path / "market-shadow"
    leaf = market / "date=2026-08-30" / "coin=BTC" / "channel=l2Book"
    leaf.mkdir(parents=True)
    payload = leaf / "part.parquet"
    payload.write_bytes(b"x" * 4096)

    total, by_date, by_coin, by_channel = MODULE._scan_tree(
        market,
        market_root=market,
    )

    assert total.file_count == 1
    assert total.apparent_bytes == 4096
    assert by_date["2026-08-30"].file_count == 1
    assert by_coin["BTC"].file_count == 1
    assert by_channel["l2Book"].file_count == 1


def test_scan_never_follows_symlinked_directory(tmp_path: Path) -> None:
    market = tmp_path / "market-shadow"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.parquet").write_bytes(b"x" * 8192)
    date = market / "date=2026-08-30"
    date.mkdir(parents=True)
    (date / "coin=BTC").symlink_to(outside, target_is_directory=True)

    total, _, _, _ = MODULE._scan_tree(market, market_root=market)

    assert total.file_count == 0
    assert total.apparent_bytes == 0


def test_unclassified_dataset_policy_fails_closed() -> None:
    fallback = {
        "tier": "UNCLASSIFIED_FAIL_CLOSED",
        "filesystem_delete_allowed": False,
    }
    value = MODULE.DATASET_POLICY.get("future-unknown-dataset", fallback)
    assert value["tier"] == "UNCLASSIFIED_FAIL_CLOSED"
    assert value["filesystem_delete_allowed"] is False


def test_postgresql_is_never_filesystem_deletable() -> None:
    policy = MODULE.DATASET_POLICY["postgresql"]
    assert policy["tier"] == "DATABASE_MANAGED"
    assert policy["filesystem_delete_allowed"] is False


def test_forecast_uses_positive_growth_and_df_semantics() -> None:
    mount = {
        "df_used_bytes": 80,
        "df_available_bytes": 20,
    }
    previous = {
        "mount": {"df_used_bytes": 60},
    }
    forecast = MODULE._forecast_thresholds(
        mount=mount,
        previous=previous,
        elapsed_hours=2.0,
    )
    assert forecast["observed_growth_bytes_per_hour"] == 10.0
    assert forecast["hours_to_df_threshold"]["80"] == 0.0
    assert forecast["hours_to_df_threshold"]["90"] == 1.0
    assert forecast["hours_to_df_threshold"]["100"] == 2.0


def test_forecast_does_not_invent_growth_when_usage_falls() -> None:
    mount = {
        "df_used_bytes": 70,
        "df_available_bytes": 30,
    }
    previous = {
        "mount": {"df_used_bytes": 80},
    }
    forecast = MODULE._forecast_thresholds(
        mount=mount,
        previous=previous,
        elapsed_hours=1.0,
    )
    assert forecast["observed_growth_bytes_per_hour"] == -10.0
    assert forecast["hours_to_df_threshold"]["80"] is None


def test_previous_snapshot_requires_valid_json_object(tmp_path: Path) -> None:
    path = tmp_path / "audit.json"
    path.write_text("not-json", encoding="utf-8")
    assert MODULE._load_previous(path) is None
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert MODULE._load_previous(path) is None


def test_elapsed_hours_requires_aware_timestamp() -> None:
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    assert MODULE._hours_between(now, {"generated_at": "2026-08-31T11:00:00"}) is None
    assert MODULE._hours_between(
        now,
        {"generated_at": "2026-08-31T10:00:00+00:00"},
    ) == 2.0


def test_allocated_bytes_are_nonnegative_for_real_file(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.write_bytes(os.urandom(2048))
    usage = MODULE.Usage()
    usage.add_stat(payload.stat())
    assert usage.apparent_bytes == 2048
    assert usage.allocated_bytes >= 0
    assert usage.file_count == 1
