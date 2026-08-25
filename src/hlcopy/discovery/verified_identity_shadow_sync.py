from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hlcopy.shadow.registry import (
    MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP,
    WalletRegistry,
    WalletSpec,
)

DEFAULT_IDENTITIES_PATH = Path("/var/lib/hyperliquid-copy-engine/invo/identified_wallets.json")
DEFAULT_REGISTRY_PATH = Path("/mnt/HC_Volume_106576526/hyperliquid/shadow/wallets.json")
MANAGED_ID_PREFIX = "invo-verified-"
MANAGED_NOTE_PREFIX = "managed_by=invo_verified_identity_sync"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync currently verified Invo identities into shadow validation.",
    )
    parser.add_argument("--identities", type=Path, default=DEFAULT_IDENTITIES_PATH)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    return parser.parse_args()


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "source": "invo", "identities": []}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid verified identity publication: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("verified identity publication must be a JSON object")
    if payload.get("version") != 1:
        raise ValueError("unsupported verified identity publication version")
    if str(payload.get("source") or "").casefold() != "invo":
        raise ValueError("verified identity publication source is not Invo")
    rows = payload.get("identities")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("verified identity publication lacks an identities list")
    return payload


def _stable_id(portfolio_id: str) -> str:
    digest = hashlib.sha256(portfolio_id.encode("utf-8")).hexdigest()[:16]
    return f"{MANAGED_ID_PREFIX}{digest}"


def _verified_rows(payload: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    rows = payload.get("identities", [])
    output: list[dict[str, str]] = []
    seen_portfolios: set[str] = set()
    seen_wallets: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"verified identity row {index} is not an object")
        portfolio_id = str(raw.get("portfolio_id") or "").strip()
        username = str(raw.get("username") or "").strip()
        wallet = str(raw.get("wallet") or "").strip().lower()
        evidence_sha = str(raw.get("evidence_sha256") or "").strip()
        resolver_rule = str(raw.get("resolver_rule_version") or "").strip()
        if not portfolio_id or not wallet or not evidence_sha or not resolver_rule:
            raise ValueError(f"verified identity row {index} lacks required provenance")
        WalletSpec(
            id=_stable_id(portfolio_id),
            label=username or portfolio_id,
            source_type="hyperliquid_wallet",
            source_ref=wallet,
            stage="research",
        )
        if portfolio_id in seen_portfolios:
            raise ValueError(f"duplicate verified Invo portfolio: {portfolio_id}")
        if wallet in seen_wallets:
            raise ValueError(f"duplicate verified Hyperliquid wallet: {wallet}")
        seen_portfolios.add(portfolio_id)
        seen_wallets.add(wallet)
        output.append(
            {
                "portfolio_id": portfolio_id,
                "username": username,
                "wallet": wallet,
                "evidence_sha256": evidence_sha,
                "resolver_rule_version": resolver_rule,
            }
        )
    return tuple(output)


def _active_count(registry: WalletRegistry) -> int:
    return len(registry.active_hyperliquid_wallets())


def _note(row: Mapping[str, str]) -> str:
    return (
        f"{MANAGED_NOTE_PREFIX}; source=invo; portfolio_id={row['portfolio_id']}; "
        f"evidence_sha256={row['evidence_sha256']}; "
        f"resolver_rule_version={row['resolver_rule_version']}"
    )


def _by_id(registry: WalletRegistry) -> dict[str, WalletSpec]:
    return {wallet.id: wallet for wallet in registry.load()}


def _by_address(registry: WalletRegistry) -> dict[str, WalletSpec]:
    return {
        wallet.source_ref.lower(): wallet
        for wallet in registry.load()
        if wallet.source_type == "hyperliquid_wallet"
    }


def _revoke_stale_managed(
    registry: WalletRegistry,
    *,
    active_managed_ids: set[str],
) -> int:
    revoked = 0
    for wallet in registry.load():
        if not wallet.id.startswith(MANAGED_ID_PREFIX):
            continue
        if MANAGED_NOTE_PREFIX not in wallet.notes:
            continue
        if wallet.id in active_managed_ids:
            continue
        if wallet.stage == "research" and not wallet.enabled:
            continue
        registry.update(
            wallet.id,
            stage="research",
            enabled=False,
            notes=f"{wallet.notes}; current_verified_identity=false",
        )
        revoked += 1
    return revoked


def _capacity_waiting(
    registry: WalletRegistry,
    wallet_id: str,
    *,
    notes: str,
) -> str:
    registry.update(
        wallet_id,
        stage="research",
        enabled=True,
        notes=notes,
    )
    return "capacity_waiting"


def _activate_managed(
    registry: WalletRegistry,
    row: Mapping[str, str],
) -> str:
    managed_id = _stable_id(row["portfolio_id"])
    existing_id = _by_id(registry).get(managed_id)
    existing_address = _by_address(registry).get(row["wallet"])

    if existing_id is not None and existing_id.source_ref.lower() != row["wallet"]:
        registry.update(
            existing_id.id,
            stage="research",
            enabled=False,
            notes=(
                f"{existing_id.notes}; current_verified_identity=false; "
                f"wallet_changed_to={row['wallet']}"
            ),
        )
        raise ValueError(
            f"managed Invo identity {managed_id} changed wallet from "
            f"{existing_id.source_ref} to {row['wallet']}"
        )

    if existing_address is not None and existing_address.id != managed_id:
        if existing_address.enabled and existing_address.stage in {"validation", "approved"}:
            return "already_active_external"
        return "external_registry_conflict"

    if existing_id is not None:
        if existing_id.stage == "approved":
            return "already_approved"
        if existing_id.stage == "validation" and existing_id.enabled:
            registry.update(existing_id.id, notes=_note(row))
            return "already_validation"
        if _active_count(registry) >= MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP:
            return _capacity_waiting(registry, existing_id.id, notes=_note(row))
        try:
            registry.update(
                existing_id.id,
                stage="validation",
                enabled=True,
                notes=_note(row),
            )
        except ValueError as exc:
            if "per-IP limit" not in str(exc):
                raise
            return _capacity_waiting(registry, existing_id.id, notes=_note(row))
        return "promoted_validation"

    stage = (
        "validation"
        if _active_count(registry) < MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP
        else "research"
    )
    label = (
        f"Invo @{row['username']}"
        if row.get("username")
        else f"Invo {row['portfolio_id']}"
    )
    stored = WalletSpec(
        id=managed_id,
        label=label,
        source_type="hyperliquid_wallet",
        source_ref=row["wallet"],
        stage=stage,
        enabled=True,
        coins=(),
        notes=_note(row),
    )
    try:
        registry.add(stored)
    except ValueError as exc:
        if stage != "validation" or "per-IP limit" not in str(exc):
            raise
        registry.add(
            WalletSpec(
                id=managed_id,
                label=stored.label,
                source_type=stored.source_type,
                source_ref=stored.source_ref,
                stage="research",
                enabled=True,
                coins=(),
                notes=stored.notes,
            )
        )
        return "capacity_waiting"
    return "added_validation" if stage == "validation" else "capacity_waiting"


def sync_verified_identities(*, identities_path: Path, registry_path: Path) -> dict[str, object]:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise RuntimeError("verified identity shadow sync refuses REAL_TRADING_ENABLED=YES")

    registry = WalletRegistry(registry_path)
    registry.init()
    try:
        payload = _load_payload(identities_path)
        rows = _verified_rows(payload)
    except ValueError:
        _revoke_stale_managed(registry, active_managed_ids=set())
        raise

    active_managed_ids = {_stable_id(row["portfolio_id"]) for row in rows}
    revoked = _revoke_stale_managed(registry, active_managed_ids=active_managed_ids)

    outcomes: dict[str, int] = {}
    for row in rows:
        outcome = _activate_managed(registry, row)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    return {
        "source": "invo",
        "published_verified": len(rows),
        "revoked_stale": revoked,
        "active_hyperliquid_wallets": _active_count(registry),
        "per_ip_limit": MAX_ACTIVE_HYPERLIQUID_USERS_PER_IP,
        "outcomes": outcomes,
        "identities_path": str(identities_path),
        "registry_path": str(registry_path),
        "safety": {
            "auto_live_approval": False,
            "new_wallet_stage": "validation_or_research_when_capacity_full",
            "stale_managed_identity_revoked": True,
            "changed_wallet_revokes_old_before_error": True,
            "invalid_publication_revokes_managed_before_error": True,
        },
    }


def main() -> int:
    args = _parse_args()
    result = sync_verified_identities(
        identities_path=args.identities,
        registry_path=args.registry,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
