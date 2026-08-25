from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from hlcopy.discovery import invo_identifier_job
from hlcopy.resolver.identifier import WalletIdentificationResult


class _FakeClient:
    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _result(wallet: str) -> WalletIdentificationResult:
    return WalletIdentificationResult(
        status="VERIFIED",
        wallet=wallet,
        candidate=wallet,
        confidence=Decimal("0.75"),
        input_trades=12,
        rejected_rows=0,
        discovery_matches=4,
        discovery_anchors=8,
        candidate_unique=True,
        historical_matches=5,
        historical_attempted=12,
        verification_source="sqd_finalized_fills",
        median_clock_offset_ms=1000.0,
        median_price_bps=Decimal("1.5"),
        report_path=None,
    )


def _unresolved() -> WalletIdentificationResult:
    return replace(
        _result("0x" + "1" * 40),
        status="UNRESOLVED",
        wallet=None,
        confidence=Decimal("0"),
    )


def test_identifier_prioritizes_carmine_and_skips_unchanged_terminal_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "invo"
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    carmine_csv = queue_dir / "carmine.csv"
    other_csv = queue_dir / "other.csv"
    carmine_csv.write_text("carmine-evidence", encoding="utf-8")
    other_csv.write_text("other-evidence", encoding="utf-8")
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": "other-id",
                        "username": "other",
                        "resolver_csv": str(other_csv),
                        "evidence_count": 30,
                        "distinct_coin_count": 10,
                    },
                    {
                        "portfolio_id": "carmine-id",
                        "username": "Carmine",
                        "resolver_csv": str(carmine_csv),
                        "evidence_count": 12,
                        "distinct_coin_count": 2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    calls: list[str] = []

    async def fake_identify(path: Path, **_: object) -> WalletIdentificationResult:
        calls.append(path.name)
        return _result("0x" + "1" * 40)

    monkeypatch.setattr(invo_identifier_job, "SqdHyperliquidFillsClient", _FakeClient)
    monkeypatch.setattr(invo_identifier_job, "identify_wallet_from_csv", fake_identify)
    args = Namespace(state_dir=state_dir, max_portfolios=1, priority_trader=[])

    first = asyncio.run(invo_identifier_job.run_once(args))
    assert first["attempted"] == 1
    assert calls == ["carmine.csv"]

    second = asyncio.run(invo_identifier_job.run_once(args))
    assert second["attempted"] == 1
    assert calls == ["carmine.csv", "other.csv"]

    third = asyncio.run(invo_identifier_job.run_once(args))
    assert third["attempted"] == 0
    identities = json.loads(
        (state_dir / "identified_wallets.json").read_text(encoding="utf-8")
    )
    assert identities["verified_count"] == 2
    assert identities["safety"]["auto_trading_promotion"] is False


def test_unchanged_unresolved_evidence_uses_retry_backoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "invo"
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    evidence = queue_dir / "carmine.csv"
    evidence.write_text("evidence", encoding="utf-8")
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": "carmine-id",
                        "username": "carmine",
                        "resolver_csv": str(evidence),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    calls = 0

    async def fake_identify(path: Path, **_: object) -> WalletIdentificationResult:
        nonlocal calls
        assert path == evidence
        calls += 1
        return _unresolved()

    monkeypatch.setattr(invo_identifier_job, "SqdHyperliquidFillsClient", _FakeClient)
    monkeypatch.setattr(invo_identifier_job, "identify_wallet_from_csv", fake_identify)
    args = Namespace(
        state_dir=state_dir,
        max_portfolios=1,
        priority_trader=[],
        unresolved_retry_minutes=60,
    )

    first = asyncio.run(invo_identifier_job.run_once(args))
    second = asyncio.run(invo_identifier_job.run_once(args))

    assert first["attempted"] == 1
    assert second["attempted"] == 0
    assert calls == 1


def test_published_identities_only_include_current_ready_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / "invo"
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps({"queue": []}),
        encoding="utf-8",
    )
    (state_dir / "identifier_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    "no-longer-ready": {
                        "status": "VERIFIED",
                        "wallet": "0x" + "1" * 40,
                        "username": "old",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    args = Namespace(state_dir=state_dir, max_portfolios=1, priority_trader=[])

    result = asyncio.run(invo_identifier_job.run_once(args))

    assert result["attempted"] == 0
    identities = json.loads(
        (state_dir / "identified_wallets.json").read_text(encoding="utf-8")
    )
    assert identities["verified_count"] == 0
    assert identities["identities"] == []


def test_identifier_uses_the_same_immutable_snapshot_it_hashed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "invo"
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    evidence = queue_dir / "carmine.csv"
    evidence.write_bytes(b"original-evidence")
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": "carmine-id",
                        "username": "carmine",
                        "resolver_csv": str(evidence),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    async def fake_identify(path: Path, **options: object) -> WalletIdentificationResult:
        path.write_bytes(b"replacement-evidence")
        snapshot = options["snapshot"]
        assert snapshot.data == b"original-evidence"
        assert options["expected_source_identity"] == "carmine-id"
        return _result("0x" + "2" * 40)

    monkeypatch.setattr(invo_identifier_job, "SqdHyperliquidFillsClient", _FakeClient)
    monkeypatch.setattr(invo_identifier_job, "identify_wallet_from_csv", fake_identify)
    args = Namespace(state_dir=state_dir, max_portfolios=1, priority_trader=[])

    result = asyncio.run(invo_identifier_job.run_once(args))

    assert result["verified"] == 1


def test_mixed_success_and_error_fails_service_after_persisting_both(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "invo"
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    carmine = queue_dir / "carmine.csv"
    bones = queue_dir / "bones.csv"
    carmine.write_text("carmine", encoding="utf-8")
    bones.write_text("bones", encoding="utf-8")
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": "carmine-id",
                        "username": "carmine",
                        "resolver_csv": str(carmine),
                    },
                    {
                        "portfolio_id": "bones-id",
                        "username": "bones",
                        "resolver_csv": str(bones),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    async def fake_identify(path: Path, **_: object) -> WalletIdentificationResult:
        if path == carmine:
            raise RuntimeError("SQD unavailable for Carmine")
        return _result("0x" + "3" * 40)

    monkeypatch.setattr(invo_identifier_job, "SqdHyperliquidFillsClient", _FakeClient)
    monkeypatch.setattr(invo_identifier_job, "identify_wallet_from_csv", fake_identify)
    args = Namespace(state_dir=state_dir, max_portfolios=2, priority_trader=[])

    with pytest.raises(RuntimeError, match="1 of 2 .* attempts failed"):
        asyncio.run(invo_identifier_job.run_once(args))

    state = json.loads(
        (state_dir / "identifier_state.json").read_text(encoding="utf-8")
    )
    assert state["items"]["carmine-id"]["status"] == "ERROR"
    assert state["items"]["bones-id"]["status"] == "VERIFIED"
    identities = json.loads(
        (state_dir / "identified_wallets.json").read_text(encoding="utf-8")
    )
    assert identities["verified_count"] == 1
    assert identities["identities"][0]["portfolio_id"] == "bones-id"


def test_changed_verified_digest_is_unpublished_while_waiting_behind_batch_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "invo"
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    carmine = queue_dir / "carmine.csv"
    waiting = queue_dir / "waiting.csv"
    carmine.write_text("new-carmine", encoding="utf-8")
    waiting.write_text("changed-waiting-evidence", encoding="utf-8")
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": "carmine-id",
                        "username": "carmine",
                        "resolver_csv": str(carmine),
                    },
                    {
                        "portfolio_id": "waiting-id",
                        "username": "other",
                        "resolver_csv": str(waiting),
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "identifier_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    "waiting-id": {
                        "status": "VERIFIED",
                        "wallet": "0x" + "4" * 40,
                        "username": "other",
                        "evidence_sha256": "old-digest",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    async def fake_identify(path: Path, **_: object) -> WalletIdentificationResult:
        assert path == carmine
        return _result("0x" + "5" * 40)

    monkeypatch.setattr(invo_identifier_job, "SqdHyperliquidFillsClient", _FakeClient)
    monkeypatch.setattr(invo_identifier_job, "identify_wallet_from_csv", fake_identify)
    args = Namespace(state_dir=state_dir, max_portfolios=1, priority_trader=[])

    result = asyncio.run(invo_identifier_job.run_once(args))

    assert result["pending"] == 2
    identities = json.loads(
        (state_dir / "identified_wallets.json").read_text(encoding="utf-8")
    )
    assert [row["portfolio_id"] for row in identities["identities"]] == [
        "carmine-id"
    ]
