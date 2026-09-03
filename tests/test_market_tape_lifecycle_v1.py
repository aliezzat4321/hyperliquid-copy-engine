from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

pl = pytest.importorskip("polars")
PATH = Path(__file__).parents[1] / "scripts" / "market_tape_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("market_tape_lifecycle_test", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def policy() -> dict:
    return {"policy_version": "LOSSLESS_NORMALIZED_V1", "recent_days": 3,
            "reader_required_columns": {"l2Book": ["coin", "exchange_ts_ms", "received_at_ns",
                                                            "bid_levels_json", "ask_levels_json"]}}


def write_book(path: Path) -> None:
    bids, asks = '[{"n":1,"px":"10","sz":"2"}]', '[{"n":1,"px":"11","sz":"2"}]'
    raw = json.dumps({"coin": "BTC", "levels": [json.loads(bids), json.loads(asks)], "time": 1},
                     separators=(",", ":"), sort_keys=True)
    pl.DataFrame({"channel": ["l2Book"], "coin": ["BTC"], "exchange_ts_ms": [1],
                  "received_at_ns": [2], "bid_levels_json": [bids], "ask_levels_json": [asks],
                  "raw_json": [raw]}).write_parquet(path)


def test_plan_apply_is_exact_sha_and_lossless(tmp_path, monkeypatch):
    directory = tmp_path / "date=2026-08-01" / "coin=BTC" / "channel=l2Book"
    directory.mkdir(parents=True)
    source = directory / "part-1.parquet"
    write_book(source)
    manifest_path = tmp_path / "manifest.json"
    manifest = MODULE.build_plan(tmp_path, policy(), manifest_path, today=date(2026, 9, 3))
    assert manifest["totals"]["source_rows"] == 1
    monkeypatch.setattr(MODULE, "disk_usage", lambda _: type("U", (), {"available": 10**9})())
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    result = MODULE.apply(manifest_path, digest, policy(), max_age_minutes=30, min_free=0)
    assert result["rows_lost"] == 0
    assert not source.exists()
    output = next(directory.glob("part-lifecycle-*.parquet"))
    assert pl.read_parquet(output).height == 1
    assert "raw_json" not in pl.read_parquet_schema(output)


def test_recent_partition_and_sha_mismatch_are_refused(tmp_path):
    directory = tmp_path / "date=2026-09-02" / "coin=BTC" / "channel=l2Book"
    directory.mkdir(parents=True)
    write_book(directory / "part-1.parquet")
    manifest_path = tmp_path / "manifest.json"
    manifest = MODULE.build_plan(tmp_path, policy(), manifest_path, today=date(2026, 9, 3))
    assert manifest["groups"] == []
    with pytest.raises(ValueError, match="SHA-256"):
        MODULE.apply(manifest_path, "0" * 64, policy(), max_age_minutes=30, min_free=0)


def test_symlinked_partition_is_refused(tmp_path):
    target = tmp_path / "actual"
    target.mkdir()
    date_link = tmp_path / "date=2026-08-01"
    date_link.symlink_to(target, target_is_directory=True)
    channel = target / "coin=BTC" / "channel=l2Book"
    channel.mkdir(parents=True)
    write_book(channel / "part.parquet")
    with pytest.raises(ValueError, match="symlinked"):
        MODULE.build_plan(tmp_path, policy(), tmp_path / "manifest.json", today=date(2026, 9, 3))


def test_reader_registry_covers_known_columnar_consumers():
    root = Path(__file__).parents[1]
    configured = json.loads(
        (root / "config" / "market_tape_lifecycle.json").read_text()
    )["reader_required_columns"]
    book_source = (root / "src" / "hlcopy" / "profitability" / "causal_book.py").read_text()
    mark_source = (
        root / "src" / "hlcopy" / "profitability" / "parquet_mark_stream.py"
    ).read_text()
    assert {"exchange_ts_ms", "received_at_ns", "bid_levels_json", "ask_levels_json"} <= set(
        configured["l2Book"]
    )
    assert {"coin", "received_at_ns", "mark_px", "oracle_px"} <= set(
        configured["activeAssetCtx"]
    )
    for column in configured["l2Book"]:
        if column != "coin":
            assert column in book_source
    for column in configured["activeAssetCtx"]:
        assert column in mark_source
