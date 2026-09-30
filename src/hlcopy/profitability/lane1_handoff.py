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


def _merge_identity_evidence(
    earlier: dict[str, object], later: dict[str, object]
) -> dict[str, object]:
    """Combine duplicate persisted projections without moving their frozen cutoff."""
    merged = dict(earlier) | later
    cutoffs = [
        value
        for value in (
            earlier.get("prospective_start_ns"),
            later.get("prospective_start_ns"),
        )
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if cutoffs:
        merged["prospective_start_ns"] = min(cutoffs)

    for field in ("history", "prospective_outcomes"):
        combined: list[object] = []
        for row in (earlier, later):
            values = row.get(field, [])
            if isinstance(values, list):
                combined.extend(value for value in values if value not in combined)
        if combined:
            merged[field] = combined
    return merged


def _latest_evaluated_outcome(row: dict[str, object]) -> dict[str, object] | None:
    outcomes = row.get("prospective_outcomes", [])
    if not isinstance(outcomes, list):
        return None
    for raw in reversed(outcomes):
        if isinstance(raw, dict) and raw.get("evaluation_state") == "EVALUATED":
            return raw
    return None


def record_prospective_outcomes(
    output_path: Path,
    outcomes: list[dict[str, object]],
) -> dict[str, object]:
    """Append prospective evidence to the queue's durable identity ledger.

    The evaluator is allowed to add evidence only. It cannot move the frozen cutoff,
    rewrite selection evidence, or silently convert insufficient observations into a
    failure. Repeated identical observations are deduplicated.
    """
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    by_key = {
        str(row.get("candidate_key")): row
        for row in outcomes
        if isinstance(row, dict) and row.get("candidate_key")
    }
    if not by_key:
        return payload

    def apply(rows: object) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("candidate_key", ""))
            outcome = by_key.get(key)
            if outcome is None:
                continue
            recorded = row.setdefault("prospective_outcomes", [])
            if not isinstance(recorded, list):
                recorded = []
                row["prospective_outcomes"] = recorded
            fingerprint = outcome.get("evidence_fingerprint")
            duplicate = any(
                isinstance(existing, dict)
                and fingerprint is not None
                and existing.get("evidence_fingerprint") == fingerprint
                for existing in recorded
            )
            if not duplicate:
                recorded.append(dict(outcome))
            row["last_prospective_outcome_at"] = outcome.get("observed_at")

    apply(payload.get("candidates"))
    apply(payload.get("demoted"))
    apply(payload.get("candidate_history"))
    _atomic_json(output_path, payload)
    return payload


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
            ledger = [*old.get("candidates", []), *old.get("demoted", [])]
        for raw in ledger:
            row = dict(raw)
            version = row.get("selection_contract_version")
            if version is None:
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
            key = str(row["candidate_key"])
            previous[key] = (
                _merge_identity_evidence(previous[key], row)
                if key in previous
                else row
            )
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
    rejection_reason_by_key: dict[str, str] = {}
    seen: set[str] = set()
    for row in robust:
        wallet = str(row.get("wallet_address", "")).lower()
        coin = str(row.get("coin", ""))
        notional = str(row.get("notional_usd", ""))
        key = f"{selection_contract_version}|{wallet}|{coin}|{notional}"
        old = previous.get(key, {})
        reason = universe_reason
        if key in seen:
            reason = "DUPLICATE_WALLET_COIN_NOTIONAL"
        elif reason is None and universe_wallets is not None and wallet not in universe_wallets:
            reason = "WALLET_NOT_IN_CURRENT_LEADERBOARD"
        elif reason is None:
            latest = _latest_evaluated_outcome(old)
            if latest is not None and latest.get("approved") is False:
                reason = "PROSPECTIVE_UNDERPERFORM"
        seen.add(key)
        if reason:
            rejection_reason_by_key[key] = reason
            rejections.append(
                {
                    "candidate_key": key,
                    "reason": reason,
                    "rejected_at": observed_at.isoformat(),
                }
            )
            continue
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
            rejection_reason = rejection_reason_by_key.get(key)
            reason = (
                "PROSPECTIVE_UNDERPERFORM"
                if rejection_reason == "PROSPECTIVE_UNDERPERFORM"
                else "NO_LONGER_ROBUST_OR_CURRENT"
            )
            history = list(row.get("history", []))
            history.append(
                {
                    "status": "demoted",
                    "observed_at": observed_at.isoformat(),
                    "reason": reason,
                }
            )
            demoted_row |= {
                "status": "demoted",
                "demoted_at": observed_at.isoformat(),
                "demotion_reason": reason,
                "history": history,
            }
        demoted.append(demoted_row)

    candidate_history = sorted(
        [*candidates, *demoted], key=lambda row: str(row["candidate_key"])
    )
    payload: dict[str, object] = {
        "mode": "LANE1_SELECTIVE_CHALLENGER_QUEUE_V3",
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
