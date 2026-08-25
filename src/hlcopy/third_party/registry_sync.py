from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hlcopy.shadow.registry import WalletRegistry, WalletSpec

MANAGED_NOTE = "managed_by=third_party_identity_sync"


def _source_slug(source: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", source.strip().casefold()).strip("-")
    if not value:
        raise ValueError("third-party source name is required")
    return value


def _stable_id(source: str, identity: str) -> str:
    digest = hashlib.sha256(f"{source.casefold()}:{identity}".encode()).hexdigest()[:16]
    return f"third-party-{_source_slug(source)}-{digest}"


def _parse_publication(value: str) -> tuple[str, Path]:
    source, separator, raw_path = value.partition("=")
    if not separator or not source.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError(
            "publication must use SOURCE=/path/to/identified_wallets.json"
        )
    return source.strip().casefold(), Path(raw_path.strip())


def _load_publication(source: str, path: Path) -> tuple[dict[str, str], ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {source} identity publication: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{source} identity publication must be an object")
    published_source = str(payload.get("source") or "").strip().casefold()
    if published_source != source.casefold():
        raise ValueError(
            f"identity publication source mismatch: expected {source!r}, got {published_source!r}"
        )
    rows = payload.get("identities")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError(f"{source} identity publication lacks an identities list")

    output: list[dict[str, str]] = []
    seen_identities: set[str] = set()
    seen_wallets: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{source} identity row {index} is not an object")
        identity = str(raw.get("portfolio_id") or raw.get("identity") or "").strip()
        username = str(raw.get("username") or raw.get("label") or "").strip()
        wallet = str(raw.get("wallet") or "").strip().lower()
        evidence_sha = str(raw.get("evidence_sha256") or "").strip()
        resolver_rule = str(raw.get("resolver_rule_version") or "").strip()
        if not identity or not wallet or not evidence_sha or not resolver_rule:
            raise ValueError(f"{source} identity row {index} lacks required provenance")
        WalletSpec(
            id=_stable_id(source, identity),
            label=username or identity,
            source_type="hyperliquid_wallet",
            source_ref=wallet,
            stage="research",
        )
        if identity in seen_identities:
            raise ValueError(f"duplicate {source} identity: {identity}")
        if wallet in seen_wallets:
            raise ValueError(f"duplicate {source} wallet: {wallet}")
        seen_identities.add(identity)
        seen_wallets.add(wallet)
        output.append(
            {
                "source": source.casefold(),
                "identity": identity,
                "username": username,
                "wallet": wallet,
                "evidence_sha256": evidence_sha,
                "resolver_rule_version": resolver_rule,
            }
        )
    return tuple(output)


def _note(row: Mapping[str, str]) -> str:
    return (
        f"{MANAGED_NOTE}; third_party_source={row['source']}; "
        f"third_party_identity={row['identity']}; evidence_sha256={row['evidence_sha256']}; "
        f"resolver_rule_version={row['resolver_rule_version']}"
    )


def _managed_for_source(wallet: WalletSpec, source: str) -> bool:
    return (
        wallet.id.startswith(f"third-party-{_source_slug(source)}-")
        and MANAGED_NOTE in wallet.notes
        and f"third_party_source={source.casefold()}" in wallet.notes
    )


def _revoke_stale(
    registry: WalletRegistry,
    *,
    source: str,
    active_ids: set[str],
) -> int:
    revoked = 0
    for wallet in registry.load():
        if not _managed_for_source(wallet, source) or wallet.id in active_ids:
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


def _sync_source(
    registry: WalletRegistry,
    *,
    source: str,
    rows: tuple[dict[str, str], ...],
) -> dict[str, int]:
    active_ids = {_stable_id(source, row["identity"]) for row in rows}
    outcomes: dict[str, int] = {
        "revoked_stale": _revoke_stale(
            registry,
            source=source,
            active_ids=active_ids,
        )
    }

    for row in rows:
        wallet_id = _stable_id(source, row["identity"])
        by_id = {item.id: item for item in registry.load()}
        by_address = {
            item.source_ref.lower(): item
            for item in registry.load()
            if item.source_type == "hyperliquid_wallet"
        }
        existing = by_id.get(wallet_id)
        address_owner = by_address.get(row["wallet"])

        if existing is not None and existing.source_ref.lower() != row["wallet"]:
            registry.update(
                existing.id,
                stage="research",
                enabled=False,
                notes=(
                    f"{existing.notes}; current_verified_identity=false; "
                    f"wallet_changed_to={row['wallet']}"
                ),
            )
            raise ValueError(
                f"third-party identity {source}:{row['identity']} changed wallet from "
                f"{existing.source_ref} to {row['wallet']}"
            )
        if address_owner is not None and address_owner.id != wallet_id:
            raise ValueError(
                f"verified third-party wallet {row['wallet']} is already owned by "
                f"registry id {address_owner.id}"
            )

        if existing is None:
            label = (
                f"{source.title()} @{row['username']}"
                if row.get("username")
                else f"{source.title()} {row['identity']}"
            )
            registry.add(
                WalletSpec(
                    id=wallet_id,
                    label=label,
                    source_type="hyperliquid_wallet",
                    source_ref=row["wallet"],
                    stage="research",
                    enabled=True,
                    coins=(),
                    notes=_note(row),
                )
            )
            outcomes["added_research"] = outcomes.get("added_research", 0) + 1
            continue

        registry.update(
            existing.id,
            stage="research",
            enabled=True,
            notes=_note(row),
        )
        outcomes["refreshed_research"] = outcomes.get("refreshed_research", 0) + 1

    return outcomes


def sync_publications(
    *,
    publications: Mapping[str, Path],
    registry_path: Path,
) -> dict[str, Any]:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise RuntimeError("third-party research registry refuses REAL_TRADING_ENABLED=YES")
    registry = WalletRegistry(registry_path)
    registry.init()

    source_results: dict[str, Any] = {}
    for source, path in sorted(publications.items()):
        rows = _load_publication(source, path)
        outcomes = _sync_source(registry, source=source, rows=rows)
        source_results[source] = {
            "publication": str(path),
            "verified": len(rows),
            "outcomes": outcomes,
        }

    enabled = [
        wallet
        for wallet in registry.load()
        if wallet.enabled and wallet.source_type == "hyperliquid_wallet"
    ]
    return {
        "mode": "THIRD_PARTY_RESEARCH_REGISTRY_V1",
        "registry": str(registry_path),
        "sources": source_results,
        "enabled_wallets": len(enabled),
        "stages": sorted({wallet.stage for wallet in enabled}),
        "safety": {
            "research_only": True,
            "consumes_user_specific_websocket_slot": False,
            "auto_validation": False,
            "auto_live_approval": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.third_party.registry_sync")
    parser.add_argument(
        "--publication",
        action="append",
        required=True,
        type=_parse_publication,
        metavar="SOURCE=PATH",
    )
    parser.add_argument("--registry", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    publications: dict[str, Path] = {}
    for source, path in args.publication:
        if source in publications:
            raise SystemExit(f"duplicate third-party publication source: {source}")
        publications[source] = path
    result = sync_publications(publications=publications, registry_path=args.registry)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
