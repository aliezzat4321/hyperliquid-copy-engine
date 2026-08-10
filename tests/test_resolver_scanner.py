from __future__ import annotations

import json
from pathlib import Path

from hlcopy.research.cli import build_parser as build_research_parser
from hlcopy.resolver.cli import build_parser as build_resolver_parser
from hlcopy.resolver.scanner import ScanConfig, _load_state, _write_state


def test_research_cli_has_explicit_max_candidates() -> None:
    args = build_research_parser().parse_args(
        [
            "--registry",
            "/tmp/wallets.json",
            "--ledger",
            "/tmp/ledger.jsonl",
            "--max-candidates",
            "100",
            "run",
        ]
    )
    assert args.max_candidates == 100


def test_scan_resolve_cli_accepts_checkpointed_batch_options() -> None:
    args = build_resolver_parser().parse_args(
        [
            "--source-registry",
            "/tmp/sources.json",
            "--wallet-registry",
            "/tmp/wallets.json",
            "scan-resolve",
            "--id",
            "bones",
            "--output-dir",
            "/tmp/out",
            "--batch-size",
            "75",
            "--universe-limit",
            "5000",
        ]
    )
    assert args.batch_size == 75
    assert args.universe_limit == 5000


def test_scan_state_is_atomic_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "scan.json"
    _write_state(
        path,
        source_id="bones",
        checked_addresses={"0x" + "1" * 40, "0x" + "2" * 40},
        leaderboard_snapshot_ms=123,
        universe_addresses=("0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40),
    )
    loaded = _load_state(path)
    assert loaded["source_id"] == "bones"
    assert loaded["universe_size"] == 3
    assert len(loaded["checked_addresses"]) == 2
    assert not path.with_suffix(".json.tmp").exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["universe_fingerprint"]) == 64


def test_scan_config_rejects_invalid_batch() -> None:
    try:
        ScanConfig(batch_size=0)
    except ValueError as exc:
        assert "batch_size" in str(exc)
    else:
        raise AssertionError("expected invalid batch_size to fail")
