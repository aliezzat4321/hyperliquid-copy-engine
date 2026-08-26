from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlcopy.discovery.invo_durable_identity import publish_durable_verified_identities

BONES = "0x7a5973ca24c3d36cea16632711ac7a6cff684789"
OTHER = "0x1111111111111111111111111111111111111111"
THIRD = "0x2222222222222222222222222222222222222222"
RULE = "generic-sqd-fill-wallet-identity-v12-size-agnostic-sequence"


def _queue_rows(state_dir: Path, rows: list[tuple[str, str]]) -> None:
    path = state_dir / "resolution_queue" / "resolution_queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": identity,
                        "username": username,
                        "resolver_csv": str(state_dir / f"{identity}.csv"),
                    }
                    for identity, username in rows
                ]
            }
        ),
        encoding="utf-8",
    )


def _queue(state_dir: Path, *, username: str = "bones") -> None:
    _queue_rows(state_dir, [("bones-portfolio", username)])


def _verified_report_for_identity(
    state_dir: Path,
    *,
    identity: str,
    wallet: str,
    suffix: str,
    evidence_sha: str = "a" * 64,
) -> Path:
    directory = state_dir / "wallet_identifications" / identity
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"wallet_identification_portfolio_deadbeef_{suffix}.json"
    path.write_text(
        json.dumps(
            {
                "version": 12,
                "resolver_rule_version": RULE,
                "mode": "SIZE_AGNOSTIC_SEQUENCE",
                "source_identity": identity,
                "input_sha256": evidence_sha,
                "status": "VERIFIED",
                "wallet": wallet,
                "confidence": "0.6000",
                "safety": {
                    "discovery_cannot_verify": True,
                    "held_out_full_lifecycle_required": True,
                    "flat_to_open_boundary_required": True,
                    "exact_boundary_sequence_replay_required": True,
                    "entry_and_exit_price_required": True,
                    "discovery_held_out_execution_disjointness_required": True,
                    "one_vote_per_sqd_execution_in_discovery": True,
                    "one_vote_per_sqd_lifecycle_in_verification": True,
                    "unique_held_out_winner_required": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _verified_report(state_dir: Path, *, wallet: str, suffix: str) -> Path:
    return _verified_report_for_identity(
        state_dir,
        identity="bones-portfolio",
        wallet=wallet,
        suffix=suffix,
    )


def test_verified_proof_survives_later_unresolved_refresh(tmp_path: Path) -> None:
    _queue(tmp_path)
    proof = _verified_report(tmp_path, wallet=BONES, suffix="20260825T213437Z")

    # The mutable latest-attempt state can become unresolved after evidence grows.
    (tmp_path / "identifier_state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": {
                    "bones-portfolio": {
                        "status": "UNRESOLVED",
                        "wallet": None,
                        "evidence_sha256": "b" * 64,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    publication = publish_durable_verified_identities(state_dir=tmp_path)
    assert publication["verified_count"] == 1
    identity = publication["identities"][0]
    assert identity["username"] == "bones"
    assert identity["wallet"] == BONES
    assert identity["evidence_sha256"] == "a" * 64
    assert identity["attestation_status"] == "DURABLE_VERIFIED"
    assert identity["proof_report"] == str(proof)
    assert publication["safety"]["unresolved_refresh_revokes_verified_identity"] is False


def test_disappearing_source_identity_is_not_published(tmp_path: Path) -> None:
    _queue(tmp_path)
    _verified_report(tmp_path, wallet=BONES, suffix="20260825T213437Z")
    first = publish_durable_verified_identities(state_dir=tmp_path)
    assert first["verified_count"] == 1

    queue_path = tmp_path / "resolution_queue" / "resolution_queue.json"
    queue_path.write_text(json.dumps({"queue": []}), encoding="utf-8")
    second = publish_durable_verified_identities(state_dir=tmp_path)
    assert second["verified_count"] == 0


def test_conflicting_verified_wallets_for_same_identity_fail_closed(tmp_path: Path) -> None:
    _queue(tmp_path)
    _verified_report(tmp_path, wallet=BONES, suffix="20260825T213437Z")
    _verified_report(tmp_path, wallet=OTHER, suffix="20260826T010000Z")

    with pytest.raises(ValueError, match="conflicting verified Hyperliquid wallets"):
        publish_durable_verified_identities(state_dir=tmp_path)

    publication = json.loads((tmp_path / "identified_wallets.json").read_text())
    assert publication["verified_count"] == 0
    assert publication["identities"] == []


def test_same_wallet_for_two_current_traders_is_quarantined_without_blocking_others(
    tmp_path: Path,
) -> None:
    _queue_rows(
        tmp_path,
        [
            ("archiduc-portfolio", "archiduc"),
            ("ironside-portfolio", "ironside"),
            ("clean-portfolio", "cleantrader"),
        ],
    )
    _verified_report_for_identity(
        tmp_path,
        identity="archiduc-portfolio",
        wallet=OTHER,
        suffix="20260826T010000Z",
        evidence_sha="a" * 64,
    )
    _verified_report_for_identity(
        tmp_path,
        identity="ironside-portfolio",
        wallet=OTHER,
        suffix="20260826T010001Z",
        evidence_sha="b" * 64,
    )
    _verified_report_for_identity(
        tmp_path,
        identity="clean-portfolio",
        wallet=THIRD,
        suffix="20260826T010002Z",
        evidence_sha="c" * 64,
    )

    publication = publish_durable_verified_identities(state_dir=tmp_path)

    assert publication["verified_count"] == 1
    assert publication["identities"][0]["username"] == "cleantrader"
    assert publication["identities"][0]["wallet"] == THIRD
    assert publication["quarantined_identity_count"] == 2
    assert publication["identity_conflicts"] == [
        {
            "wallet": OTHER,
            "portfolio_ids": ["archiduc-portfolio", "ironside-portfolio"],
            "usernames": ["archiduc", "ironside"],
            "status": "QUARANTINED_AMBIGUOUS_IDENTITY",
        }
    ]


def test_disappeared_conflicting_identity_does_not_block_current_trader(tmp_path: Path) -> None:
    _queue_rows(tmp_path, [("current-portfolio", "current")])
    _verified_report_for_identity(
        tmp_path,
        identity="current-portfolio",
        wallet=OTHER,
        suffix="20260826T010000Z",
    )
    _verified_report_for_identity(
        tmp_path,
        identity="old-portfolio",
        wallet=OTHER,
        suffix="20260825T010000Z",
        evidence_sha="b" * 64,
    )

    publication = publish_durable_verified_identities(state_dir=tmp_path)

    assert publication["verified_count"] == 1
    assert publication["identities"][0]["username"] == "current"
    assert publication["identity_conflicts"] == []


def test_identifier_service_runs_durable_wrapper() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/hyperliquid-invo-wallet-identifier.service").read_text()
    assert "hlcopy.discovery.invo_identifier_durable_job" in unit
    assert "OnSuccess=hyperliquid-invo-verified-shadow-sync.service" in unit
    assert "hyperliquid-third-party-registry-sync.service" in unit
