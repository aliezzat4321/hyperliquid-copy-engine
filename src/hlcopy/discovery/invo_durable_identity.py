from __future__ import annotations

import hashlib
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
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "unverified_candidate_used_as_identity": False,
            "durable_verified_attestation": True,
            # A later transient/unresolved retry cannot erase a proof for the
            # exact same evidence, but any evidence change does revoke it.
            "unresolved_refresh_revokes_verified_identity": False,
            "current_evidence_digest_required": True,
            "evidence_change_revokes_verified_identity": True,
            "conflicting_verified_wallet_fails_closed": True,
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
        resolver_csv = str(raw.get("resolver_csv") or "").strip()
        if not identity or not username or not resolver_csv:
            raise ValueError(
                f"Invo resolution queue row {index} lacks identity/evidence provenance"
            )
        if identity in output:
            raise ValueError(f"duplicate Invo portfolio in resolution queue: {identity}")
        output[identity] = {
            "portfolio_id": identity,
            "username": username,
            "resolver_csv": resolver_csv,
        }
    return output


def _current_evidence_sha256(*, state_dir: Path, resolver_csv: str) -> str:
    root = state_dir.resolve()
    raw_path = Path(resolver_csv)
    candidate = raw_path if raw_path.is_absolute() else root / raw_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"current Invo resolver evidence is missing: {candidate}") from exc
    if not resolved.is_file():
        raise ValueError(f"current Invo resolver evidence is not a file: {resolved}")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Invo resolver evidence escapes state directory: {resolved}"
        ) from exc

    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"cannot read current Invo resolver evidence: {resolved}") from exc
    return digest.hexdigest()


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


def _collect_attestations(
    reports_dir: Path,
    *,
    current_evidence_sha: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    by_identity: dict[str, list[dict[str, str]]] = {}
    if not reports_dir.exists():
        return {}
    for path in sorted(reports_dir.glob("*/wallet_identification_*.json")):
        attestation = _verified_report(path)
        if attestation is None:
            continue
        identity = attestation["portfolio_id"]
        expected_sha = current_evidence_sha.get(identity)
        if expected_sha is None or attestation["evidence_sha256"] != expected_sha:
            # Historical proofs are useful audit records, but they are never a
            # current identity proof after the source evidence changes.
            continue
        by_identity.setdefault(identity, []).append(attestation)

    output: dict[str, dict[str, str]] = {}
    wallet_owners: dict[str, str] = {}
    for identity, rows in sorted(by_identity.items()):
        wallets = {row["wallet"] for row in rows}
        if len(wallets) != 1:
            raise ValueError(
                f"conflicting verified Hyperliquid wallets for Invo portfolio {identity}: "
                + ", ".join(sorted(wallets))
            )
        selected = rows[-1]
        wallet = selected["wallet"]
        previous_owner = wallet_owners.get(wallet)
        if previous_owner is not None and previous_owner != identity:
            raise ValueError(
                f"verified Hyperliquid wallet {wallet} belongs to multiple Invo portfolios: "
                f"{previous_owner}, {identity}"
            )
        wallet_owners[wallet] = identity
        output[identity] = selected
    return output


def publish_durable_verified_identities(*, state_dir: Path) -> dict[str, Any]:
    queue_path = state_dir / "resolution_queue" / "resolution_queue.json"
    reports_dir = state_dir / "wallet_identifications"
    identities_path = state_dir / "identified_wallets.json"

    # Any malformed queue/report/evidence set must revoke the public view first.
    _write_atomic(identities_path, _empty_publication())
    queue = _load_queue(queue_path)
    current_evidence_sha = {
        portfolio_id: _current_evidence_sha256(
            state_dir=state_dir,
            resolver_csv=current["resolver_csv"],
        )
        for portfolio_id, current in queue.items()
    }
    attestations = _collect_attestations(
        reports_dir,
        current_evidence_sha=current_evidence_sha,
    )

    identities: list[dict[str, Any]] = []
    for portfolio_id, current in sorted(queue.items()):
        proof = attestations.get(portfolio_id)
        if proof is None:
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
    publication["attestation_reports_seen"] = len(attestations)
    _write_atomic(identities_path, publication)
    return publication
