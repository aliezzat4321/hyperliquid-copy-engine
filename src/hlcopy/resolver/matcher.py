from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from hlcopy.models import Fill
from hlcopy.signals.invo import CopySignal

D = Decimal
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    trade_id: str
    action: str
    coin: str
    direction: str
    timestamp_ms: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    action: str
    coin: str
    direction: str
    timestamp_ms: int
    price: Decimal
    tid: int


@dataclass(frozen=True, slots=True)
class EventMatch:
    trade_id: str
    action: str
    coin: str
    direction: str
    evidence_timestamp_ms: int
    candidate_timestamp_ms: int
    time_delta_ms: int
    evidence_price: Decimal
    candidate_price: Decimal
    price_delta_bps: Decimal
    candidate_tid: int
    quality: Decimal

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


@dataclass(frozen=True, slots=True)
class CandidateResolution:
    address: str
    score: Decimal
    match_ratio: Decimal
    matched_events: int
    total_events: int
    matched_trades: int
    total_trades: int
    matched_coins: int
    evidence_coins: int
    median_time_delta_ms: float | None
    median_price_delta_bps: Decimal | None
    conflicts: int
    matches: tuple[EventMatch, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "score": str(self.score),
            "match_ratio": str(self.match_ratio),
            "matched_events": self.matched_events,
            "total_events": self.total_events,
            "matched_trades": self.matched_trades,
            "total_trades": self.total_trades,
            "matched_coins": self.matched_coins,
            "evidence_coins": self.evidence_coins,
            "median_time_delta_ms": self.median_time_delta_ms,
            "median_price_delta_bps": (
                str(self.median_price_delta_bps)
                if self.median_price_delta_bps is not None
                else None
            ),
            "conflicts": self.conflicts,
            "matches": [match.to_dict() for match in self.matches],
        }


def evidence_events(signals: tuple[CopySignal, ...]) -> tuple[EvidenceEvent, ...]:
    events: list[EvidenceEvent] = []
    for signal in signals:
        events.extend(
            [
                EvidenceEvent(
                    trade_id=signal.signal_id,
                    action="OPEN",
                    coin=signal.coin,
                    direction=signal.direction,
                    timestamp_ms=signal.opened_at_ms,
                    price=signal.entry_price,
                ),
                EvidenceEvent(
                    trade_id=signal.signal_id,
                    action="CLOSE",
                    coin=signal.coin,
                    direction=signal.direction,
                    timestamp_ms=signal.closed_at_ms,
                    price=signal.exit_price,
                ),
            ]
        )
    return tuple(sorted(events, key=lambda event: (event.timestamp_ms, event.trade_id, event.action)))


def select_anchor_trades(
    signals: tuple[CopySignal, ...],
    *,
    max_trades: int = 16,
) -> tuple[CopySignal, ...]:
    """Pick recent, diverse evidence without using candidate results to select anchors."""
    if max_trades <= 0:
        return ()
    by_coin: dict[str, list[CopySignal]] = {}
    for signal in signals:
        by_coin.setdefault(signal.coin, []).append(signal)
    selected: list[CopySignal] = []
    seen: set[str] = set()
    # First take the most recent trade for every coin so rare assets remain informative.
    for coin in sorted(by_coin, key=lambda key: (len(by_coin[key]), key)):
        signal = max(by_coin[coin], key=lambda item: (item.closed_at_ms, item.signal_id))
        selected.append(signal)
        seen.add(signal.signal_id)
        if len(selected) >= max_trades:
            break
    # Fill remaining capacity strictly by recency.
    for signal in sorted(signals, key=lambda item: (item.closed_at_ms, item.signal_id), reverse=True):
        if len(selected) >= max_trades:
            break
        if signal.signal_id in seen:
            continue
        selected.append(signal)
        seen.add(signal.signal_id)
    return tuple(sorted(selected, key=lambda item: (item.opened_at_ms, item.signal_id)))


def _normalize_fill_direction(fill: Fill) -> tuple[str, str] | None:
    text = fill.direction.strip().lower()
    if "open" in text and "long" in text:
        return "OPEN", "LONG"
    if "open" in text and "short" in text:
        return "OPEN", "SHORT"
    if "close" in text and "long" in text:
        return "CLOSE", "LONG"
    if "close" in text and "short" in text:
        return "CLOSE", "SHORT"
    return None


def candidate_events(address: str, raw_rows: list[dict[str, Any]]) -> tuple[CandidateEvent, ...]:
    events: list[CandidateEvent] = []
    for raw in raw_rows:
        try:
            fill = Fill.from_raw(address, raw)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
        normalized = _normalize_fill_direction(fill)
        if normalized is None:
            continue
        action, direction = normalized
        events.append(
            CandidateEvent(
                action=action,
                coin=fill.coin.upper(),
                direction=direction,
                timestamp_ms=fill.timestamp_ms,
                price=fill.price,
                tid=fill.tid,
            )
        )
    return tuple(sorted(events, key=lambda event: (event.timestamp_ms, event.tid)))


def _price_delta_bps(reference: Decimal, candidate: Decimal) -> Decimal:
    if reference <= 0 or candidate <= 0:
        return D("Infinity")
    return abs(candidate / reference - D("1")) * BPS


def score_candidate(
    *,
    address: str,
    evidence: tuple[EvidenceEvent, ...],
    candidate: tuple[CandidateEvent, ...],
    time_tolerance_ms: int,
    price_tolerance_bps: Decimal,
) -> CandidateResolution:
    if time_tolerance_ms <= 0:
        raise ValueError("time_tolerance_ms must be positive")
    if price_tolerance_bps <= 0:
        raise ValueError("price_tolerance_bps must be positive")
    used: set[int] = set()
    matches: list[EventMatch] = []
    conflicts = 0

    for ev in evidence:
        best_index: int | None = None
        best_key: tuple[Decimal, int, Decimal] | None = None
        for index, candidate_ev in enumerate(candidate):
            if index in used or candidate_ev.coin != ev.coin:
                continue
            time_delta = abs(candidate_ev.timestamp_ms - ev.timestamp_ms)
            if time_delta > time_tolerance_ms:
                continue
            if candidate_ev.action != ev.action or candidate_ev.direction != ev.direction:
                conflicts += 1
                continue
            price_delta = _price_delta_bps(ev.price, candidate_ev.price)
            if price_delta > price_tolerance_bps:
                continue
            normalized = D(time_delta) / D(time_tolerance_ms) + price_delta / price_tolerance_bps
            key = (normalized, time_delta, price_delta)
            if best_key is None or key < best_key:
                best_key = key
                best_index = index
        if best_index is None:
            continue
        candidate_ev = candidate[best_index]
        used.add(best_index)
        time_delta = abs(candidate_ev.timestamp_ms - ev.timestamp_ms)
        price_delta = _price_delta_bps(ev.price, candidate_ev.price)
        quality = max(
            D("0"),
            D("1")
            - (D(time_delta) / D(time_tolerance_ms)) * D("0.5")
            - (price_delta / price_tolerance_bps) * D("0.5"),
        )
        matches.append(
            EventMatch(
                trade_id=ev.trade_id,
                action=ev.action,
                coin=ev.coin,
                direction=ev.direction,
                evidence_timestamp_ms=ev.timestamp_ms,
                candidate_timestamp_ms=candidate_ev.timestamp_ms,
                time_delta_ms=time_delta,
                evidence_price=ev.price,
                candidate_price=candidate_ev.price,
                price_delta_bps=price_delta,
                candidate_tid=candidate_ev.tid,
                quality=quality,
            )
        )

    total_events = len(evidence)
    matched_events = len(matches)
    match_ratio = D(matched_events) / D(total_events) if total_events else D("0")
    total_trades = len({event.trade_id for event in evidence})
    matched_trades = len({match.trade_id for match in matches})
    evidence_coins = len({event.coin for event in evidence})
    matched_coins = len({match.coin for match in matches})
    mean_quality = (
        sum((match.quality for match in matches), D("0")) / D(matched_events)
        if matches
        else D("0")
    )
    coin_coverage = D(matched_coins) / D(evidence_coins) if evidence_coins else D("0")
    actions_present = {event.action for event in evidence}
    matched_actions = {match.action for match in matches}
    action_coverage = (
        D(len(matched_actions)) / D(len(actions_present)) if actions_present else D("0")
    )
    conflict_penalty = min(D("0.20"), D(conflicts) * D("0.01"))
    score = max(
        D("0"),
        D("100")
        * (
            match_ratio * D("0.65")
            + mean_quality * D("0.20")
            + coin_coverage * D("0.10")
            + action_coverage * D("0.05")
            - conflict_penalty
        ),
    )
    return CandidateResolution(
        address=address.lower(),
        score=score,
        match_ratio=match_ratio,
        matched_events=matched_events,
        total_events=total_events,
        matched_trades=matched_trades,
        total_trades=total_trades,
        matched_coins=matched_coins,
        evidence_coins=evidence_coins,
        median_time_delta_ms=(median(match.time_delta_ms for match in matches) if matches else None),
        median_price_delta_bps=(
            D(str(median(match.price_delta_bps for match in matches))) if matches else None
        ),
        conflicts=conflicts,
        matches=tuple(matches),
    )


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    status: str
    verified_address: str | None
    reason_codes: tuple[str, ...]
    best_score: Decimal | None
    runner_up_score: Decimal | None
    score_gap: Decimal | None


def decide_resolution(
    ranked: tuple[CandidateResolution, ...],
    *,
    min_matched_events: int = 12,
    min_match_ratio: Decimal = D("0.70"),
    min_score: Decimal = D("80"),
    max_median_time_ms: float = 2_000.0,
    max_median_price_bps: Decimal = D("3"),
    min_runner_up_gap: Decimal = D("15"),
) -> ResolutionDecision:
    if not ranked:
        return ResolutionDecision("UNRESOLVED", None, ("NO_CANDIDATES",), None, None, None)
    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    failures: list[str] = []
    if best.matched_events < min_matched_events:
        failures.append("TOO_FEW_MATCHED_EVENTS")
    if best.match_ratio < min_match_ratio:
        failures.append("LOW_MATCH_RATIO")
    if best.score < min_score:
        failures.append("LOW_IDENTITY_SCORE")
    if best.median_time_delta_ms is None or best.median_time_delta_ms > max_median_time_ms:
        failures.append("TIMING_MATCH_TOO_WEAK")
    if best.median_price_delta_bps is None or best.median_price_delta_bps > max_median_price_bps:
        failures.append("PRICE_MATCH_TOO_WEAK")
    if {match.action for match in best.matches} != {"OPEN", "CLOSE"}:
        failures.append("MISSING_OPEN_CLOSE_COVERAGE")
    runner_score = runner.score if runner is not None else None
    gap = best.score - runner.score if runner is not None else best.score
    if gap < min_runner_up_gap:
        failures.append("AMBIGUOUS_RUNNER_UP")
    if failures:
        return ResolutionDecision(
            "UNRESOLVED",
            None,
            tuple(failures),
            best.score,
            runner_score,
            gap,
        )
    return ResolutionDecision(
        "VERIFIED",
        best.address,
        (),
        best.score,
        runner_score,
        gap,
    )


def evidence_fingerprint(evidence: tuple[EvidenceEvent, ...]) -> str:
    payload = [
        {
            "trade_id": event.trade_id,
            "action": event.action,
            "coin": event.coin,
            "direction": event.direction,
            "timestamp_ms": event.timestamp_ms,
            "price": str(event.price),
        }
        for event in evidence
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def candidate_universe_fingerprint(addresses: tuple[str, ...]) -> str:
    encoded = json.dumps(sorted(address.lower() for address in addresses)).encode()
    return hashlib.sha256(encoded).hexdigest()
