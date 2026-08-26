from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

EXPECTED_REPORT_VERSION = 12
EXPECTED_MODE = "SIZE_AGNOSTIC_SEQUENCE"
EXPECTED_RULE = "generic-sqd-fill-wallet-identity-v12-size-agnostic-sequence"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_REQUIRED_SAFETY = (
    "discovery_cannot_verify",
    "held_out_full_lifecycle_required",
    "flat_to_open_boundary_required",
    "exact_boundary_sequence_replay_required",
    "entry_and_exit_price_required",
    "discovery_held_out_execution_disjointness_required",
    "one_vote_per_sqd_execution_in_discovery",
    "one_vote_per_sqd_lifecycle_in_verification",
    "unique_held_out_winner_required",
)


def _empty_publication() -> dict[str, Any]:
    return {
        "version": 1,
        "source": "invo",
        "verified_count": 0,
        "identities": [],
        "identity_conflicts": [],
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "unverified_candidate_used_as_identity": False,
            "durable_verified_attestation": True,
            "unresolved_refresh_revokes_verified_identity": False,
            "conflicting_verified_wallet_fails_closed": True,
            "cross_identity_wallet_conflicts_quarantined": True,
        },
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_queue(path: Path) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Invo resolution queue: {path}") from exc
    rows = payload.get("queue") if isinstance(payload, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("Invo resolution queue lacks a valid queue list")
    output: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Invo resolution queue row {index} is not an object")
        identity = str(raw.get("portfolio_id") or "").strip()
        username = str(raw.get("username") or "").strip()
        if not identity or not username:
            raise ValueError(f"Invo resolution queue row {index} lacks identity provenance")
        if identity in output:
            raise ValueError(f"duplicate Invo portfolio in resolution queue: {identity}")
        output[identity] = {"portfolio_id": identity, "username": username}
    return output


def _verified_report(path: Path) -> dict[str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt wallet identification report: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"wallet identification report is not an object: {path}")
    if payload.get("status") != "VERIFIED":
        return None
    if payload.get("version") != EXPECTED_REPORT_VERSION:
        return None
    if payload.get("mode") != EXPECTED_MODE:
        return None
    resolver_rule = str(payload.get("resolver_rule_version") or "").strip()
    if resolver_rule != EXPECTED_RULE:
        return None
    safety = payload.get("safety")
    missing_safety = (
        not isinstance(safety, Mapping)
        or any(safety.get(key) is not True for key in _REQUIRED_SAFETY)
    )
    if missing_safety:
        raise ValueError(f"verified report lacks required identity safety proof: {path}")
    identity = str(payload.get("source_identity") or "").strip()
    wallet = str(payload.get("wallet") or "").strip().lower()
    evidence_sha = str(payload.get("input_sha256") or "").strip().lower()
    confidence = str(payload.get("confidence") or "0").strip()
    if not identity or not _ADDRESS_RE.fullmatch(wallet):
        raise ValueError(f"verified report lacks valid identity or wallet: {path}")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha):
        raise ValueError(f"verified report lacks valid evidence digest: {path}")
    return {
        "portfolio_id": identity,
        "wallet": wallet,
        "confidence": confidence,
        "evidence_sha256": evidence_sha,
        "resolver_rule_version": resolver_rule,
        "proof_report": str(path),
        "identified_at": path.stem.rsplit("_", 1)[-1],
    }


def _collect_attestations(reports_dir: Path) -> dict[str, dict[str, str]]:
    by_identity: dict[str, list[dict[str, str]]] = {}
    if not reports_dir.exists():
        return {}
    for path in sorted(reports_dir.glob("*/wallet_identification_*.json")):
        attestation = _verified_report(path)
        if attestation is None:
            continue
        by_identity.setdefault(attestation["portfolio_id"], []).append(attestation)

    output: dict[str, dict[str, str]] = {}
    for identity, rows in sorted(by_identity.items()):
        wallets = {row["wallet"] for row in rows}
        if len(wallets) != 1:
            # Contradictory proofs for one Invo identity invalidate the full
            # publication because we cannot know which address owns that identity.
            raise ValueError(
                f"conflicting verified Hyperliquid wallets for Invo portfolio {identity}: "
                + ", ".join(sorted(wallets))
            )
        output[identity] = rows[-1]
    return output


def publish_durable_verified_identities(*, state_dir: Path) -> dict[str, Any]:
    queue_path = state_dir / "resolution_queue" / "resolution_queue.json"
    reports_dir = state_dir / "wallet_identifications"
    identities_path = state_dir / "identified_wallets.json"

    # Any malformed queue/report set must revoke the public view before failing.
    _write_atomic(identities_path, _empty_publication())
    queue = _load_queue(queue_path)
    attestations = _collect_attestations(reports_dir)

    # Only current queue identities participate in cross-identity uniqueness.
    # Historical/disappeared identities must not freeze unrelated current wallets.
    selected: dict[str, dict[str, str]] = {
        portfolio_id: proof
        for portfolio_id, proof in attestations.items()
        if portfolio_id in queue
    }
    wallet_owners: dict[str, list[str]] = {}
    for portfolio_id, proof in selected.items():
        wallet_owners.setdefault(proof["wallet"], []).append(portfolio_id)

    conflicting_ids: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    for wallet, owners in sorted(wallet_owners.items()):
        if len(owners) <= 1:
            continue
        owner_ids = sorted(owners)
        conflicting_ids.update(owner_ids)
        conflicts.append(
            {
                "wallet": wallet,
                "portfolio_ids": owner_ids,
                "usernames": [queue[identity]["username"] for identity in owner_ids],
                "status": "QUARANTINED_AMBIGUOUS_IDENTITY",
            }
        )

    identities: list[dict[str, Any]] = []
    for portfolio_id, current in sorted(queue.items()):
        proof = selected.get(portfolio_id)
        if proof is None or portfolio_id in conflicting_ids:
            continue
        identities.append(
            {
                "portfolio_id": portfolio_id,
                "username": current["username"],
                "wallet": proof["wallet"],
                "confidence": proof["confidence"],
                "evidence_sha256": proof["evidence_sha256"],
                "resolver_rule_version": proof["resolver_rule_version"],
                "identified_at": proof["identified_at"],
                "proof_report": proof["proof_report"],
                "attestation_status": "DURABLE_VERIFIED",
            }
        )

    publication = _empty_publication()
    publication["verified_count"] = len(identities)
    publication["identities"] = identities
    publication["identity_conflicts"] = conflicts
    publication["quarantined_identity_count"] = len(conflicting_ids)
    publication["attestation_reports_seen"] = len(attestations)
    _write_atomic(identities_path, publication)
    return publication
