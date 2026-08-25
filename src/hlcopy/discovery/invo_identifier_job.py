from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hlcopy.resolver.identifier import identify_wallet_from_csv
from hlcopy.resolver.provenance import EvidenceSnapshot
from hlcopy.resolver.sqd_position_aware import SqdHyperliquidFillsClient

DEFAULT_STATE_DIR = Path("/var/lib/hyperliquid-copy-engine/invo")
DEFAULT_PRIORITY_TRADERS = ("carmine", "bones")
DEFAULT_UNRESOLVED_RETRY_MINUTES = 60
RESOLVER_RULE_VERSION = "sqd-public-trade-v2-absolute-size"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve ready Invo trade evidence to Hyperliquid wallets.",
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--max-portfolios", type=int, default=4)
    parser.add_argument("--priority-trader", action="append", default=[])
    parser.add_argument(
        "--unresolved-retry-minutes",
        type=int,
        default=DEFAULT_UNRESOLVED_RETRY_MINUTES,
    )
    return parser.parse_args()


def _load_object(path: Path, *, missing: Mapping[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(missing)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt wallet identifier state: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"non-object wallet identifier state: {path}")
    return payload


def _save_object(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _priority_names(values: Sequence[str]) -> tuple[str, ...]:
    selected = values or DEFAULT_PRIORITY_TRADERS
    return tuple(dict.fromkeys(value.removeprefix("@").strip().casefold() for value in selected))


def _normalized_name(value: object) -> str:
    return str(value or "").removeprefix("@").strip().casefold()


def _retry_is_due(row: Mapping[str, Any], *, now: datetime) -> bool:
    raw = row.get("next_retry_at")
    if not raw:
        return True
    try:
        retry_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return retry_at <= now


def _queue_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("queue")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("Invo resolution queue lacks a valid queue list")
    output: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"Invo resolution queue row {index} is not an object")
        item = dict(row)
        for required in ("portfolio_id", "username", "resolver_csv"):
            if not str(item.get(required) or "").strip():
                raise ValueError(f"Invo resolution queue row {index} lacks {required}")
        output.append(item)
    return output


def _safe_evidence_path(state_dir: Path, raw_path: object) -> Path:
    path = Path(str(raw_path)).resolve()
    root = state_dir.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"resolver evidence escapes Invo state directory: {path}")
    if not path.is_file():
        raise ValueError(f"resolver evidence does not exist: {path}")
    return path


def _sort_queue(
    rows: Sequence[dict[str, Any]],
    *,
    priority_names: tuple[str, ...],
) -> list[dict[str, Any]]:
    priority = {name: index for index, name in enumerate(priority_names)}
    fallback = len(priority) + 1
    return sorted(
        rows,
        key=lambda row: (
            priority.get(_normalized_name(row.get("username")), fallback),
            -int(row.get("distinct_coin_count") or 0),
            -int(row.get("evidence_count") or 0),
            str(row.get("portfolio_id") or ""),
        ),
    )


def _summary_payload(
    items: Mapping[str, Any],
    *,
    active_portfolio_ids: set[str],
    current_evidence_sha256: Mapping[str, str],
) -> dict[str, Any]:
    verified = [
        {
            "portfolio_id": portfolio_id,
            "username": row.get("username"),
            "wallet": row.get("wallet"),
            "confidence": row.get("confidence"),
            "evidence_sha256": row.get("evidence_sha256"),
            "resolver_rule_version": row.get("resolver_rule_version"),
            "identified_at": row.get("attempted_at"),
        }
        for portfolio_id, row in sorted(items.items())
        if portfolio_id in active_portfolio_ids
        and isinstance(row, Mapping)
        and row.get("status") == "VERIFIED"
        and row.get("evidence_sha256") == current_evidence_sha256.get(portfolio_id)
        and row.get("resolver_rule_version") == RESOLVER_RULE_VERSION
    ]
    return {
        "version": 1,
        "source": "invo",
        "verified_count": len(verified),
        "identities": verified,
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "unverified_candidate_used_as_identity": False,
        },
    }


async def run_once(args: argparse.Namespace) -> dict[str, object]:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise RuntimeError("Invo wallet identifier refuses REAL_TRADING_ENABLED=YES")

    state_dir: Path = args.state_dir
    queue_path = state_dir / "resolution_queue" / "resolution_queue.json"
    identifier_state_path = state_dir / "identifier_state.json"
    identities_path = state_dir / "identified_wallets.json"
    reports_dir = state_dir / "wallet_identifications"

    queue_payload = _load_object(queue_path, missing={"queue": []})
    queue = _sort_queue(
        _queue_items(queue_payload),
        priority_names=_priority_names(args.priority_trader),
    )
    active_portfolio_ids = {str(row["portfolio_id"]) for row in queue}
    state = _load_object(identifier_state_path, missing={"version": 1, "items": {}})
    items = state.get("items")
    if not isinstance(items, dict):
        raise ValueError("wallet identifier state lacks an items object")

    pending: list[tuple[dict[str, Any], Path, EvidenceSnapshot]] = []
    current_evidence_sha256: dict[str, str] = {}
    now = datetime.now(tz=UTC)
    for row in queue:
        portfolio_id = str(row["portfolio_id"])
        evidence_path = _safe_evidence_path(state_dir, row["resolver_csv"])
        snapshot = EvidenceSnapshot.from_path(evidence_path)
        evidence_sha = snapshot.sha256
        current_evidence_sha256[portfolio_id] = evidence_sha
        previous = items.get(portfolio_id)
        if (
            isinstance(previous, Mapping)
            and previous.get("evidence_sha256") == evidence_sha
            and previous.get("resolver_rule_version") == RESOLVER_RULE_VERSION
        ):
            if previous.get("status") == "VERIFIED":
                continue
            if previous.get("status") == "UNRESOLVED" and not _retry_is_due(
                previous, now=now
            ):
                continue
        pending.append((row, evidence_path, snapshot))

    attempted = 0
    verified = 0
    unresolved = 0
    errors = 0
    limit = max(1, int(args.max_portfolios))
    if pending:
        async with SqdHyperliquidFillsClient() as client:
            for row, evidence_path, snapshot in pending[:limit]:
                attempted += 1
                portfolio_id = str(row["portfolio_id"])
                evidence_sha = snapshot.sha256
                attempted_at = datetime.now(tz=UTC).isoformat()
                try:
                    report_key = hashlib.sha256(portfolio_id.encode("utf-8")).hexdigest()[:16]
                    result = await identify_wallet_from_csv(
                        evidence_path,
                        output_dir=reports_dir / report_key,
                        client=client,
                        snapshot=snapshot,
                        expected_source_identity=portfolio_id,
                    )
                    result_row = result.to_dict()
                    status = result.status
                    if status == "VERIFIED":
                        verified += 1
                    else:
                        unresolved += 1
                    previous = items.get(portfolio_id)
                    repeated_unresolved = (
                        status == "UNRESOLVED"
                        and isinstance(previous, Mapping)
                        and previous.get("status") == "UNRESOLVED"
                        and previous.get("evidence_sha256") == evidence_sha
                        and previous.get("resolver_rule_version")
                        == RESOLVER_RULE_VERSION
                    )
                    unresolved_attempts = (
                        int(previous.get("unchanged_unresolved_attempts") or 0) + 1
                        if repeated_unresolved
                        else (1 if status == "UNRESOLVED" else 0)
                    )
                    retry_minutes = min(
                        24 * 60,
                        max(
                            1,
                            int(
                                getattr(
                                    args,
                                    "unresolved_retry_minutes",
                                    DEFAULT_UNRESOLVED_RETRY_MINUTES,
                                )
                            ),
                        )
                        * (2 ** max(0, unresolved_attempts - 1)),
                    )
                    next_retry_at = (
                        (datetime.now(tz=UTC) + timedelta(minutes=retry_minutes)).isoformat()
                        if status == "UNRESOLVED"
                        else None
                    )
                    items[portfolio_id] = {
                        "portfolio_id": portfolio_id,
                        "username": row.get("username"),
                        "portfolio_name": row.get("portfolio_name"),
                        "status": status,
                        "wallet": result.wallet,
                        "candidate": result.candidate,
                        "confidence": str(result.confidence),
                        "evidence_count": row.get("evidence_count"),
                        "evidence_sha256": evidence_sha,
                        "resolver_rule_version": RESOLVER_RULE_VERSION,
                        "attempted_at": attempted_at,
                        "unchanged_unresolved_attempts": unresolved_attempts,
                        "next_retry_at": next_retry_at,
                        "result": result_row,
                    }
                except Exception as exc:
                    errors += 1
                    items[portfolio_id] = {
                        "portfolio_id": portfolio_id,
                        "username": row.get("username"),
                        "portfolio_name": row.get("portfolio_name"),
                        "status": "ERROR",
                        "wallet": None,
                        "candidate": None,
                        "confidence": "0",
                        "evidence_count": row.get("evidence_count"),
                        "evidence_sha256": evidence_sha,
                        "resolver_rule_version": RESOLVER_RULE_VERSION,
                        "attempted_at": attempted_at,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                _save_object(identifier_state_path, {"version": 1, "items": items})
                _save_object(
                    identities_path,
                    _summary_payload(
                        items,
                        active_portfolio_ids=active_portfolio_ids,
                        current_evidence_sha256=current_evidence_sha256,
                    ),
                )

    if not identifier_state_path.exists():
        _save_object(identifier_state_path, {"version": 1, "items": items})
    _save_object(
        identities_path,
        _summary_payload(
            items,
            active_portfolio_ids=active_portfolio_ids,
            current_evidence_sha256=current_evidence_sha256,
        ),
    )
    if errors > 0:
        raise RuntimeError(
            f"{errors} of {attempted} Invo wallet identification attempts failed"
        )
    return {
        "queue_ready": len(queue),
        "pending": len(pending),
        "attempted": attempted,
        "verified": verified,
        "unresolved": unresolved,
        "errors": errors,
        "priority_traders": list(_priority_names(args.priority_trader)),
        "state_dir": str(state_dir),
    }


async def _main() -> int:
    result = await run_once(_parse_args())
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
