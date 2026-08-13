from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability import position_live_cli
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider
from hlcopy.shadow.selective_policy import EffectivePolicyStore, load_policy_store

D = Decimal
_PROVIDER_CACHE: dict[Path, CausalParquetL2BookProvider] = {}


def _shared_causal_provider(market_dir: Path) -> CausalParquetL2BookProvider:
    key = market_dir.resolve()
    provider = _PROVIDER_CACHE.get(key)
    if provider is None:
        provider = CausalParquetL2BookProvider(key)
        _PROVIDER_CACHE[key] = provider
    return provider


def _event_action(event) -> str:
    start = event.leader_start
    after = event.leader_after
    if start == 0 and after != 0:
        return "INCREASE"
    if start != 0 and after == 0:
        return "CLOSE"
    if start != 0 and after != 0 and start * after < 0:
        return "FLIP"
    if abs(after) > abs(start):
        return "INCREASE"
    if abs(after) < abs(start):
        return "REDUCE"
    return "UNCHANGED"


def _event_direction(event) -> str:
    return event.direction_after or event.direction_before or "UNKNOWN"


def _allowed(store: EffectivePolicyStore, event) -> bool:
    decision = store.decide(
        decision_time_ns=event.received_at_ns,
        wallet_address=event.wallet_address,
        coin=event.coin,
        direction=_event_direction(event),
        action=_event_action(event),
        notional_usd=D("0"),
    )
    return decision.state in {"SHADOW_ONLY", "COPY"}


def _output_dir(argv: list[str]) -> Path:
    try:
        index = argv.index("--output-dir")
        return Path(argv[index + 1])
    except (ValueError, IndexError) as exc:
        raise SystemExit("selective shadow requires --output-dir") from exc


def _jsonable(row: dict[str, object]) -> dict[str, object]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in row.items()
    }


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("selective causal shadow refuses REAL_TRADING_ENABLED=YES")
    raw_store = os.getenv("HLCOPY_SELECTIVE_POLICY_STORE", "").strip()
    if not raw_store:
        raise SystemExit("HLCOPY_SELECTIVE_POLICY_STORE is required")
    policy_path = Path(raw_store)
    if not policy_path.exists():
        raise SystemExit(f"selective policy store missing: {policy_path}")
    store = load_policy_store(policy_path)
    if not store.policies:
        raise SystemExit("selective policy store contains no policies")

    original_direct = position_live_cli.load_direct_events
    original_wide = position_live_cli.load_wide_events
    original_simulate = position_live_cli.simulate_copy_with_portfolio_capital
    state_rows: list[dict[str, object]] = []

    def selective_direct(shadow_dir: Path, wallet_id: str):
        return tuple(
            event
            for event in original_direct(shadow_dir, wallet_id)
            if _allowed(store, event)
        )

    def selective_wide(enriched_dir: Path, *, cutoff_ns: int):
        return tuple(
            event
            for event in original_wide(enriched_dir, cutoff_ns=cutoff_ns)
            if _allowed(store, event)
        )

    def capture_simulation(*args, **kwargs):
        sim = original_simulate(*args, **kwargs)
        for event in sim.state_events:
            row = asdict(event)
            row.update(
                {
                    "lane": sim.lane,
                    "wallet_id": sim.wallet_id,
                    "wallet_address": sim.wallet_address,
                    "scenario": sim.scenario,
                    "notional_usd": sim.notional_usd,
                }
            )
            state_rows.append(_jsonable(row))
        return sim

    position_live_cli.load_direct_events = selective_direct
    position_live_cli.load_wide_events = selective_wide
    position_live_cli.simulate_copy_with_portfolio_capital = capture_simulation
    position_live_cli.ParquetL2BookProvider = _shared_causal_provider
    print(
        f"selective_shadow policy_count={len(store.policies)} "
        f"latest_policy={store.policies[-1].policy_id} real_trading=False",
        flush=True,
    )
    position_live_cli.main()

    output_dir = _output_dir(sys.argv)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "selective_state_events.json"
    payload = {
        "real_trading": False,
        "policy_store": str(policy_path),
        "latest_policy_id": store.policies[-1].policy_id,
        "fee_accounting_mode": "ALLOCATED_ENTRY_PLUS_EXIT_FEES_V1",
        "state_events": state_rows,
    }
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(state_path)
    print(
        f"selective_state_events rows={len(state_rows)} output={state_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
