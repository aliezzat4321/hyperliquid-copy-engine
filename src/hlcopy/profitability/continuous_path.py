from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

from hlcopy.profitability.path_risk import EquityCheckpoint, OpenPositionMark
from hlcopy.profitability.portfolio_position_copy import FollowerStateEvent

D = Decimal
ZERO = D("0")


@dataclass(frozen=True, slots=True)
class AssetContextMark:
    coin: str
    received_at_ns: int
    mark_price: Decimal
    oracle_price: Decimal

    def __post_init__(self) -> None:
        if self.received_at_ns <= 0:
            raise ValueError("received_at_ns must be positive")
        if self.mark_price <= ZERO or self.oracle_price <= ZERO:
            raise ValueError("mark and oracle prices must be positive")


@dataclass(frozen=True, slots=True)
class FundingPayment:
    coin: str
    payment_ts_ms: int
    funding_rate: Decimal


@dataclass(frozen=True, slots=True)
class MarginRule:
    coin: str
    effective_from_ns: int
    maintenance_margin_rate: Decimal
    maintenance_margin_deduction_usd: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.effective_from_ns <= 0:
            raise ValueError("effective_from_ns must be positive")
        if self.maintenance_margin_rate < ZERO:
            raise ValueError("maintenance_margin_rate cannot be negative")
        if self.maintenance_margin_deduction_usd < ZERO:
            raise ValueError("maintenance_margin_deduction_usd cannot be negative")


@dataclass(frozen=True, slots=True)
class PathCoverage:
    complete: bool
    blockers: tuple[str, ...]
    first_checkpoint_ns: int | None
    last_checkpoint_ns: int | None
    checkpoint_count: int
    funding_payment_count: int


@dataclass(frozen=True, slots=True)
class ContinuousPath:
    checkpoints: tuple[EquityCheckpoint, ...]
    checkpoint_received_at_ns: tuple[int, ...]
    coverage: PathCoverage


def _rule_at(rules: tuple[MarginRule, ...], at_ns: int) -> MarginRule | None:
    chosen: MarginRule | None = None
    for rule in rules:
        if rule.effective_from_ns <= at_ns:
            chosen = rule
        else:
            break
    return chosen


def build_continuous_path(
    state_events: Iterable[FollowerStateEvent],
    asset_contexts: Iterable[AssetContextMark],
    funding_payments: Iterable[FundingPayment],
    margin_rules: Iterable[MarginRule],
    *,
    max_mark_age_ns: int = 15_000_000_000,
) -> ContinuousPath:
    """Replay follower state over actual captured mark/oracle contexts.

    The resulting checkpoints are suitable for ``evaluate_cross_margin_path`` only when
    ``coverage.complete`` is true. No price, funding, margin rule, or state is invented.

    Funding convention: a positive market funding rate means a positive-quantity long
    pays and a negative-quantity short receives, so follower funding PnL is
    ``-signed_qty * oracle_price * funding_rate``.
    """
    if max_mark_age_ns <= 0:
        raise ValueError("max_mark_age_ns must be positive")

    states = sorted(
        tuple(state_events),
        key=lambda item: (item.execution_received_at_ns, item.source_tid, item.action),
    )
    contexts = sorted(
        tuple(asset_contexts),
        key=lambda item: (item.received_at_ns, item.coin),
    )
    funding = sorted(tuple(funding_payments), key=lambda item: (item.payment_ts_ms, item.coin))

    rules_by_coin: dict[str, tuple[MarginRule, ...]] = {}
    grouped_rules: dict[str, list[MarginRule]] = {}
    for rule in margin_rules:
        grouped_rules.setdefault(rule.coin, []).append(rule)
    for coin, rows in grouped_rules.items():
        rules_by_coin[coin] = tuple(sorted(rows, key=lambda item: item.effective_from_ns))

    if not states:
        return ContinuousPath(
            checkpoints=(),
            checkpoint_received_at_ns=(),
            coverage=PathCoverage(False, ("NO_FOLLOWER_STATE_EVENTS",), None, None, 0, 0),
        )
    if not contexts:
        return ContinuousPath(
            checkpoints=(),
            checkpoint_received_at_ns=(),
            coverage=PathCoverage(False, ("NO_ACTIVE_ASSET_CONTEXT",), None, None, 0, 0),
        )

    current_qty: dict[str, Decimal] = {}
    current_entry: dict[str, Decimal | None] = {}
    current_realized = ZERO
    cumulative_funding = ZERO
    latest_context: dict[str, AssetContextMark] = {}
    blockers: set[str] = set()
    checkpoints: list[EquityCheckpoint] = []
    checkpoint_ns: list[int] = []

    state_index = 0
    funding_index = 0
    applied_funding = 0

    # Funding records carry exchange wall-clock milliseconds. Map them to the first
    # locally received market context at/after that timestamp, preserving causal order.
    for context in contexts:
        now_ns = context.received_at_ns

        while state_index < len(states) and states[state_index].execution_received_at_ns <= now_ns:
            event = states[state_index]
            current_qty[event.coin] = event.qty_after
            current_entry[event.coin] = event.avg_entry_after
            current_realized = event.realized_net_pnl_cumulative_usd
            state_index += 1

        latest_context[context.coin] = context

        while funding_index < len(funding) and funding[funding_index].payment_ts_ms * 1_000_000 <= now_ns:
            payment = funding[funding_index]
            qty = current_qty.get(payment.coin, ZERO)
            if qty != ZERO:
                oracle_context = latest_context.get(payment.coin)
                if oracle_context is None:
                    blockers.add(f"MISSING_FUNDING_ORACLE:{payment.coin}")
                elif now_ns - oracle_context.received_at_ns > max_mark_age_ns:
                    blockers.add(f"STALE_FUNDING_ORACLE:{payment.coin}")
                else:
                    cumulative_funding += -qty * oracle_context.oracle_price * payment.funding_rate
                    applied_funding += 1
            funding_index += 1

        positions: list[OpenPositionMark] = []
        for coin, qty in sorted(current_qty.items()):
            if qty == ZERO:
                continue
            entry = current_entry.get(coin)
            if entry is None:
                blockers.add(f"MISSING_ENTRY:{coin}")
                continue
            mark = latest_context.get(coin)
            if mark is None:
                blockers.add(f"MISSING_MARK:{coin}")
                continue
            if now_ns - mark.received_at_ns > max_mark_age_ns:
                blockers.add(f"STALE_MARK:{coin}")
                continue
            rule = _rule_at(rules_by_coin.get(coin, ()), now_ns)
            if rule is None:
                blockers.add(f"MISSING_MARGIN_RULE:{coin}")
                continue
            positions.append(
                OpenPositionMark(
                    coin=coin,
                    qty=qty,
                    avg_entry=entry,
                    mark_price=mark.mark_price,
                    maintenance_margin_rate=rule.maintenance_margin_rate,
                    maintenance_margin_deduction_usd=rule.maintenance_margin_deduction_usd,
                )
            )

        # Only emit a checkpoint when all currently open positions were represented.
        open_count = sum(qty != ZERO for qty in current_qty.values())
        if len(positions) != open_count:
            continue
        checkpoints.append(
            EquityCheckpoint(
                exchange_ts_ms=now_ns // 1_000_000,
                realized_net_pnl_usd=current_realized,
                funding_pnl_usd=cumulative_funding,
                positions=tuple(positions),
            )
        )
        checkpoint_ns.append(now_ns)

    if state_index < len(states):
        blockers.add("STATE_AFTER_LAST_MARK")
    if funding_index < len(funding):
        blockers.add("FUNDING_AFTER_LAST_MARK")

    complete = not blockers and bool(checkpoints)
    return ContinuousPath(
        checkpoints=tuple(checkpoints),
        checkpoint_received_at_ns=tuple(checkpoint_ns),
        coverage=PathCoverage(
            complete=complete,
            blockers=tuple(sorted(blockers)),
            first_checkpoint_ns=checkpoint_ns[0] if checkpoint_ns else None,
            last_checkpoint_ns=checkpoint_ns[-1] if checkpoint_ns else None,
            checkpoint_count=len(checkpoints),
            funding_payment_count=applied_funding,
        ),
    )
