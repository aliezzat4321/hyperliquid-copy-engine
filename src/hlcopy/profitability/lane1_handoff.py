from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

LANE1_SELECTION_CONTRACT_V1 = "lane1-selective-v1"


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_challenger_queue(
    robust: list[dict[str, object]],
    *,
    selection_contract_version: str,
    output_path: Path,
    universe_state_path: Path | None,
    max_universe_age_hours: float,
    now: datetime | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> dict[str, object]:
    """Persist the selective robust -> frozen prospective boundary.

    A wallet must still be present in a fresh official-leaderboard observation. Each
    selection-contract/wallet/coin/notional identity is frozen with its own prospective
    cutoff. The identity ledger and its evidence snapshots survive demotion and re-entry
    so post-selection observations can never leak into selection.
    """
    if not selection_contract_version.strip():
        raise ValueError("selection_contract_version must be explicit and non-empty")
    observed_at = now or datetime.now(UTC)
    observed_at = (
        observed_at.astimezone(UTC)
        if observed_at.tzinfo
        else observed_at.replace(tzinfo=UTC)
    )
    observed_ns = (clock_ns or __import__("time").time_ns)()
    previous: dict[str, dict[str, object]] = {}
    if output_path.exists():
        old = json.loads(output_path.read_text(encoding="utf-8"))
        ledger = old.get("candidate_history")
        if not isinstance(ledger, list):
            # Migrate the original V1 format. Its active and demoted rows are both
            # evidence, even though older writers only consulted the active array.
            ledger = [*old.get("candidates", []), *old.get("demoted", [])]
        for raw in ledger:
            row = dict(raw)
            version = row.get("selection_contract_version")
            if version is None:
                # The legacy queue was produced by this exact V1 selection contract.
                version = LANE1_SELECTION_CONTRACT_V1
                row["selection_contract_version"] = version
                legacy_key = str(row["candidate_key"])
                row["candidate_key"] = f"{version}|{legacy_key}"
            if not isinstance(row.get("history"), list):
                history = [
                    {
                        "status": "challenger",
                        "observed_at": row.get(
                            "challenger_created_at", old.get("generated_at")
                        ),
                        "evidence": {
                            field: row[field]
                            for field in (
                                "wallet_address",
                                "coin",
                                "notional_usd",
                                "worst_latency_return_bps",
                                "actions_floor",
                            )
                            if field in row
                        },
                    }
                ]
                if row.get("status") == "demoted":
                    history.append(
                        {
                            "status": "demoted",
                            "observed_at": row.get("demoted_at", old.get("generated_at")),
                            "reason": row.get(
                                "demotion_reason", "NO_LONGER_ROBUST_OR_CURRENT"
                            ),
                        }
                    )
                row["history"] = history
            previous[str(row["candidate_key"])] = row
        # Consumers historically annotate the active/demoted projections. Merge only
        # append-only outcome evidence back into the authoritative identity ledger.
        for projection in [*old.get("candidates", []), *old.get("demoted", [])]:
            projection_key = str(projection["candidate_key"])
            if projection_key not in previous and "selection_contract_version" not in projection:
                projection_key = f"{LANE1_SELECTION_CONTRACT_V1}|{projection_key}"
            if projection_key in previous and "prospective_outcomes" in projection:
                recorded = previous[projection_key].get("prospective_outcomes", [])
                projected = projection["prospective_outcomes"]
                if isinstance(recorded, list) and isinstance(projected, list):
                    previous[projection_key]["prospective_outcomes"] = [
                        *recorded,
                        *(outcome for outcome in projected if outcome not in recorded),
                    ]
                elif "prospective_outcomes" not in previous[projection_key]:
                    previous[projection_key]["prospective_outcomes"] = projected

    universe_wallets: set[str] | None = None
    universe_generated: datetime | None = None
    universe_reason: str | None = None
    if universe_state_path is not None:
        if not universe_state_path.exists():
            universe_reason = "UNIVERSE_STATE_MISSING"
            universe_wallets = set()
        else:
            universe_payload = json.loads(universe_state_path.read_text(encoding="utf-8"))
            universe_generated = _parse_time(universe_payload.get("generated_at"))
            raw_wallets = universe_payload.get("wallets", {})
            universe_wallets = (
                {str(address).lower() for address in raw_wallets}
                if isinstance(raw_wallets, dict)
                else set()
            )
            if universe_generated is None:
                universe_reason = "UNIVERSE_TIMESTAMP_INVALID"
            elif universe_generated > observed_at:
                universe_reason = "UNIVERSE_TIMESTAMP_FUTURE"
            elif (
                observed_at - universe_generated
            ).total_seconds() > max(0.0, max_universe_age_hours) * 3600:
                universe_reason = "UNIVERSE_STATE_STALE"

    candidates: list[dict[str, object]] = []
    rejections: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in robust:
        wallet = str(row.get("wallet_address", "")).lower()
        coin = str(row.get("coin", ""))
        notional = str(row.get("notional_usd", ""))
        key = f"{selection_contract_version}|{wallet}|{coin}|{notional}"
        reason = universe_reason
        if key in seen:
            reason = "DUPLICATE_WALLET_COIN_NOTIONAL"
        elif reason is None and universe_wallets is not None and wallet not in universe_wallets:
            reason = "WALLET_NOT_IN_CURRENT_LEADERBOARD"
        seen.add(key)
        if reason:
            rejections.append(
                {
                    "candidate_key": key,
                    "reason": reason,
                    "rejected_at": observed_at.isoformat(),
                }
            )
            continue
        old = previous.get(key, {})
        history = list(old.get("history", []))
        status = old.get("status")
        event = {
            "status": "challenger",
            "observed_at": observed_at.isoformat(),
            "evidence": dict(row),
        }
        if status != "challenger" or not history:
            history.append(event)
        candidate = (
            dict(row)
            | {
                "candidate_key": key,
                "selection_contract_version": selection_contract_version,
                "status": "challenger",
                "challenger_created_at": old.get(
                    "challenger_created_at", observed_at.isoformat()
                ),
                "prospective_start_ns": old.get("prospective_start_ns", observed_ns),
                "last_confirmed_at": observed_at.isoformat(),
                "history": history,
            }
        )
        # Outcomes may be written by an evaluator between queue refreshes. They belong
        # to the immutable identity and must never be replaced by research input.
        if "prospective_outcomes" in old:
            candidate["prospective_outcomes"] = old["prospective_outcomes"]
        candidates.append(candidate)

    active_keys = {str(row["candidate_key"]) for row in candidates}
    demoted: list[dict[str, object]] = []
    for key, row in previous.items():
        if key in active_keys:
            continue
        demoted_row = dict(row)
        if row.get("status") == "challenger":
            history = list(row.get("history", []))
            history.append(
                {
                    "status": "demoted",
                    "observed_at": observed_at.isoformat(),
                    "reason": "NO_LONGER_ROBUST_OR_CURRENT",
                }
            )
            demoted_row |= {
                "status": "demoted",
                "demoted_at": observed_at.isoformat(),
                "demotion_reason": "NO_LONGER_ROBUST_OR_CURRENT",
                "history": history,
            }
        demoted.append(demoted_row)

    candidate_history = sorted(
        [*candidates, *demoted], key=lambda row: str(row["candidate_key"])
    )
    payload: dict[str, object] = {
        "mode": "LANE1_SELECTIVE_CHALLENGER_QUEUE_V2",
        "selection_contract_version": selection_contract_version,
        "generated_at": observed_at.isoformat(),
        "real_trading": False,
        "universe_generated_at": universe_generated.isoformat() if universe_generated else None,
        "counts": {
            "robust": len(robust),
            "challenger": len(candidates),
            "rejected": len(rejections),
            "demoted": len(demoted),
        },
        "candidates": candidates,
        "rejections": rejections,
        "demoted": demoted,
        "candidate_history": candidate_history,
    }
    _atomic_json(output_path, payload)
    return payload
