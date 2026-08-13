from __future__ import annotations

import os
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
    # The v1 research publisher emits wallet+coin lifecycle rules. Pass notional=0
    # intentionally so the selective scorer evaluates capacity across every configured
    # notional; max_notional_usd remains a research hint until prospective capacity is
    # measured by this exact execution path.
    decision = store.decide(
        decision_time_ns=event.received_at_ns,
        wallet_address=event.wallet_address,
        coin=event.coin,
        direction=_event_direction(event),
        action=_event_action(event),
        notional_usd=D("0"),
    )
    return decision.state in {"SHADOW_ONLY", "COPY"}


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

    def selective_direct(shadow_dir: Path, wallet_id: str):
        return tuple(event for event in original_direct(shadow_dir, wallet_id) if _allowed(store, event))

    def selective_wide(enriched_dir: Path, *, cutoff_ns: int):
        return tuple(event for event in original_wide(enriched_dir, cutoff_ns=cutoff_ns) if _allowed(store, event))

    position_live_cli.load_direct_events = selective_direct
    position_live_cli.load_wide_events = selective_wide
    position_live_cli.ParquetL2BookProvider = _shared_causal_provider
    print(
        f"selective_shadow policy_count={len(store.policies)} "
        f"latest_policy={store.policies[-1].policy_id} real_trading=False",
        flush=True,
    )
    position_live_cli.main()


if __name__ == "__main__":
    main()
