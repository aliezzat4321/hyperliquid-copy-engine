from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
    output_path: Path,
    universe_state_path: Path | None,
    max_universe_age_hours: float,
    now: datetime | None = None,
    clock_ns: Callable[[], int] | None = None,
) -> dict[str, object]:
    """Persist the selective robust -> frozen prospective boundary.

    A wallet must still be present in a fresh official-leaderboard observation.  Each
    wallet/coin/notional is frozen with its own prospective cutoff, which is preserved
    on later runs so post-selection observations can never leak into selection.
    """
    observed_at = now or datetime.now(UTC)
    observed_ns = (clock_ns or __import__("time").time_ns)()
    previous: dict[str, dict[str, object]] = {}
    if output_path.exists():
        old = json.loads(output_path.read_text(encoding="utf-8"))
        previous = {str(row["candidate_key"]): row for row in old.get("candidates", [])}

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
            elif (observed_at - universe_generated).total_seconds() > max_universe_age_hours * 3600:
                universe_reason = "UNIVERSE_STATE_STALE"

    candidates: list[dict[str, object]] = []
    rejections: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in robust:
        wallet = str(row.get("wallet_address", "")).lower()
        coin = str(row.get("coin", ""))
        notional = str(row.get("notional_usd", ""))
        key = f"{wallet}|{coin}|{notional}"
        reason = universe_reason
        if key in seen:
            reason = "DUPLICATE_WALLET_COIN_NOTIONAL"
        elif universe_wallets is not None and wallet not in universe_wallets:
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
        candidates.append(
            dict(row)
            | {
                "candidate_key": key,
                "status": "challenger",
                "challenger_created_at": old.get("challenger_created_at", observed_at.isoformat()),
                "prospective_start_ns": old.get("prospective_start_ns", observed_ns),
                "last_confirmed_at": observed_at.isoformat(),
            }
        )

    active_keys = {str(row["candidate_key"]) for row in candidates}
    demoted = [
        dict(row)
        | {
            "status": "demoted",
            "demoted_at": observed_at.isoformat(),
            "demotion_reason": "NO_LONGER_ROBUST_OR_CURRENT",
        }
        for key, row in previous.items()
        if key not in active_keys and row.get("status") == "challenger"
    ]
    payload: dict[str, object] = {
        "mode": "LANE1_SELECTIVE_CHALLENGER_QUEUE_V1",
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
    }
    _atomic_json(output_path, payload)
    return payload
