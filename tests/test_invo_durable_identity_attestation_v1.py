from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlcopy.discovery.invo_durable_identity import publish_durable_verified_identities

BONES = "0x7a5973ca24c3d36cea16632711ac7a6cff684789"
OTHER = "0x1111111111111111111111111111111111111111"
RULE = "generic-sqd-fill-wallet-identity-v12-size-agnostic-sequence"


def _queue(state_dir: Path, *, username: str = "bones") -> None:
    path = state_dir / "resolution_queue" / "resolution_queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "queue": [
                    {
                        "portfolio_id": "bones-portfolio",
                        "username": username,
                        "resolver_csv": str(state_dir / "resolver.csv"),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _verified_report(state_dir: Path, *, wallet: str, suffix: str) -> Path:
    directory = state_dir / "wallet_identifications" / "abcd"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"wallet_identification_portfolio_deadbeef_{suffix}.json"
    path.write_text(
        json.dumps(
            {
                "version": 12,
                "resolver_rule_version": RULE,
                "mode": "SIZE_AGNOSTIC_SEQUENCE",
                "source_identity": "bones-portfolio",
                "input_sha256": "a" * 64,
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


def test_conflicting_verified_wallets_fail_closed(tmp_path: Path) -> None:
    _queue(tmp_path)
    _verified_report(tmp_path, wallet=BONES, suffix="20260825T213437Z")
    _verified_report(tmp_path, wallet=OTHER, suffix="20260826T010000Z")

    with pytest.raises(ValueError, match="conflicting verified Hyperliquid wallets"):
        publish_durable_verified_identities(state_dir=tmp_path)

    publication = json.loads((tmp_path / "identified_wallets.json").read_text())
    assert publication["verified_count"] == 0
    assert publication["identities"] == []


def test_identifier_service_runs_durable_wrapper() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy/systemd/hyperliquid-invo-wallet-identifier.service").read_text()
    assert "hlcopy.discovery.invo_identifier_durable_job" in unit
    assert "OnSuccess=hyperliquid-invo-verified-shadow-sync.service" in unit
    assert "hyperliquid-third-party-registry-sync.service" in unit
