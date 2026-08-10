from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from hlcopy.research.publisher import publish_ranked_candidates
from hlcopy.shadow.registry import WalletRegistry

ADDRESS = "0x1111111111111111111111111111111111111111"
ADDRESS_2 = "0x2222222222222222222222222222222222222222"


def _write_ranked(path: Path) -> None:
    pl.DataFrame(
        [
            {
                "rank": 1,
                "address": ADDRESS,
                "display_name": "Alpha",
                "composite_score": 91.5,
                "style": "INTRADAY",
                "warning_flags": "",
                "source_snapshot_ms": 1234,
                "screened_count": 100,
                "shortlisted_count": 25,
                "ranked_count": 20,
            },
            {
                "rank": 2,
                "address": ADDRESS_2,
                "display_name": None,
                "composite_score": 70.0,
                "style": "SWING",
                "warning_flags": "FAST_ALPHA",
                "source_snapshot_ms": 1234,
                "screened_count": 100,
                "shortlisted_count": 25,
                "ranked_count": 20,
            },
        ]
    ).write_parquet(path)


def test_publish_registers_only_research_and_keeps_point_in_time_ledger(tmp_path: Path):
    parquet = tmp_path / "ranked.parquet"
    _write_ranked(parquet)
    registry = WalletRegistry(tmp_path / "wallets.json")
    ledger = tmp_path / "ledger.jsonl"

    result = publish_ranked_candidates(
        parquet_path=parquet,
        registry=registry,
        ledger_path=ledger,
        max_candidates=10,
        min_composite_score=80.0,
    )
    assert result.observed == 1
    assert result.newly_registered == 1
    wallets = registry.load()
    assert len(wallets) == 1
    assert wallets[0].stage == "research"
    assert wallets[0].source_ref == ADDRESS
    assert wallets[0].coins == ()

    observations = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(observations) == 1
    assert observations[0]["screened_count"] == 100
    assert observations[0]["source_snapshot_ms"] == 1234
    assert observations[0]["artifact_fingerprint"] == result.artifact_fingerprint


def test_repeated_research_appends_evidence_without_duplicate_registry_entry(tmp_path: Path):
    parquet = tmp_path / "ranked.parquet"
    _write_ranked(parquet)
    registry = WalletRegistry(tmp_path / "wallets.json")
    ledger = tmp_path / "ledger.jsonl"

    first = publish_ranked_candidates(
        parquet_path=parquet,
        registry=registry,
        ledger_path=ledger,
        max_candidates=1,
    )
    second = publish_ranked_candidates(
        parquet_path=parquet,
        registry=registry,
        ledger_path=ledger,
        max_candidates=1,
    )
    assert first.newly_registered == 1
    assert second.newly_registered == 0
    assert second.skipped_existing == 1
    assert len(registry.load()) == 1
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
