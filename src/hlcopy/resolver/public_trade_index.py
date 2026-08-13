from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.reverse_index import (
    AnchorMatch,
    CandidateFingerprint,
    IndexedCompletedTrade,
    _best_matches_for_anchor,
    rank_candidates,
)
from hlcopy.resolver.source_registry import ExternalSourceSpec
from hlcopy.resolver.sqd_fills import (
    EpisodeEvidence,
    SqdFill,
    SqdHyperliquidFillsClient,
    aggregate_close_fills,
    match_episode,
    signal_position_size,
)
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.generic_csv import load_generic_closed_trades
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal
DEFAULT_MAX_SIZE_RATIO_ERROR = D("0.60")


@dataclass(frozen=True, slots=True)
class PublicTradeDiscoveryConfig:
    anchor_trades: int = 8
    window_seconds: int = 30
    max_price_bps: Decimal = D("25")
    max_size_ratio_error: Decimal = DEFAULT_MAX_SIZE_RATIO_ERROR
    min_discovery_matches: int = 3
    min_runner_up_score_gap: Decimal = D("15")
    max_candidates_to_verify: int = 6
    historical_verify_trades: int = 12
    historical_lookback_hours: int = 6
    historical_time_tolerance_ms: int = 25_000
    historical_price_tolerance_bps: Decimal = D("35")
    historical_entry_price_tolerance_bps: Decimal = D("15")
    historical_max_size_ratio_error: Decimal = D("0.45")
    min_historical_matches: int = 3
    min_historical_ratio: Decimal = D("0.20")
    min_historical_winner_match_gap: int = 2


DEFAULT_PUBLIC_TRADE_CONFIG = PublicTradeDiscoveryConfig()


@dataclass(frozen=True, slots=True)
class PublicTradeDiscoveryResult:
    ranked: tuple[CandidateFingerprint, ...]
    anchors: tuple[CopySignal, ...]
    coverage_start_ms: int


@dataclass(frozen=True, slots=True)
class HistoricalVerification:
    attempted: int
    matched: int
    ratio: Decimal
    matched_signal_ids: tuple[str, ...]
    evidence: tuple[EpisodeEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "matched": self.matched,
            "ratio": str(self.ratio),
            "matched_signal_ids": list(self.matched_signal_ids),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class HistoricalCandidateVerification:
    address: str
    discovery_matches: int
    discovery_score: Decimal
    verification: HistoricalVerification

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "discovery_matches": self.discovery_matches,
            "discovery_score": str(self.discovery_score),
            "verification": self.verification.to_dict(),
        }


def _source_signals(source: ExternalSourceSpec) -> tuple[CopySignal, ...]:
    if source.adapter == "invo_closed_trades_csv":
        result = load_invo_closed_trades(Path(source.evidence_path))
        rejected = result.rejected_rows
        signals = result.signals
    elif source.adapter == "generic_closed_trades_csv":
        result = load_generic_closed_trades(Path(source.evidence_path))
        rejected = result.rejected_rows
        signals = result.signals
    else:
        raise ValueError(f"unsupported public trade resolver adapter: {source.adapter}")
    if rejected:
        raise ValueError(
            f"external evidence contains {len(rejected)} malformed rows; fail closed"
        )
    return signals


def _public_trade_matches(
    signal: CopySignal,
    fills: list[SqdFill],
    *,
    window_ms: int,
    max_price_bps: Decimal,
    max_size_ratio_error: Decimal = DEFAULT_MAX_SIZE_RATIO_ERROR,
) -> dict[str, AnchorMatch]:
    target_size = signal_position_size(signal)
    candidates: list[IndexedCompletedTrade] = []
    for close in aggregate_close_fills(fills, direction=signal.direction):
        time_error = min(
            abs(close.first_time_ms - signal.closed_at_ms),
            abs(close.last_time_ms - signal.closed_at_ms),
        )
        if time_error > window_ms:
            continue
        if target_size is not None:
            size_error = abs(close.size / target_size - D("1"))
            if size_error > max_size_ratio_error:
                continue
        candidates.append(
            IndexedCompletedTrade(
                user=close.user,
                coin=close.coin,
                direction=signal.direction,
                start_ms=signal.opened_at_ms,
                end_ms=close.last_time_ms,
                entry_price=signal.entry_price,
                exit_price=close.avg_price,
                trade_id=close.group_id,
                raw={
                    "aggregated_close_size": str(close.size),
                    "fill_count": close.fill_count,
                    "closed_pnl": str(close.closed_pnl),
                },
            )
        )
    return _best_matches_for_anchor(
        signal,
        candidates,
        window_ms=window_ms,
        max_price_bps=max_price_bps,
    )


async def discover_candidates(
    signals: tuple[CopySignal, ...],
    *,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> PublicTradeDiscoveryResult:
    coverage_start_ms = await client.coverage_start_ms()
    eligible = tuple(signal for signal in signals if signal.closed_at_ms >= coverage_start_ms)
    if len(eligible) < 3:
        return PublicTradeDiscoveryResult((), (), coverage_start_ms)
    anchors = select_anchor_trades(eligible, max_trades=max(3, config.anchor_trades))
    matches_by_anchor: dict[str, dict[str, AnchorMatch]] = {}
    window_ms = max(1, config.window_seconds) * 1000
    for signal in anchors:
        fills = await client.fills_around(
            timestamp_ms=signal.closed_at_ms,
            coin=signal.coin,
            window_ms=window_ms,
        )
        matches_by_anchor[signal.signal_id] = _public_trade_matches(
            signal,
            fills,
            window_ms=window_ms,
            max_price_bps=config.max_price_bps,
            max_size_ratio_error=config.max_size_ratio_error,
        )
    ranked = rank_candidates(matches_by_anchor, total_anchors=len(anchors))
    return PublicTradeDiscoveryResult(ranked, anchors, coverage_start_ms)


def candidate_is_unique(
    ranked: tuple[CandidateFingerprint, ...],
    *,
    min_score_gap: Decimal,
) -> bool:
    if not ranked:
        return False
    if len(ranked) == 1:
        return True
    best, runner_up = ranked[0], ranked[1]
    if best.matched_anchors <= runner_up.matched_anchors:
        return False
    return best.score - runner_up.score >= min_score_gap


async def verify_candidate_historically(
    *,
    address: str,
    signals: tuple[CopySignal, ...],
    excluded_signal_ids: set[str],
    coverage_start_ms: int,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> HistoricalVerification:
    eligible = [
        signal
        for signal in signals
        if signal.signal_id not in excluded_signal_ids
        and signal.closed_at_ms >= coverage_start_ms
    ]
    eligible.sort(key=lambda item: (item.closed_at_ms, item.signal_id), reverse=True)
    selected = eligible[: config.historical_verify_trades]
    evidence: list[EpisodeEvidence] = []
    matched_ids: list[str] = []
    lookback_ms = max(1, config.historical_lookback_hours) * 60 * 60 * 1000
    for signal in selected:
        start_ms = max(coverage_start_ms, signal.opened_at_ms - lookback_ms)
        end_ms = signal.closed_at_ms + config.historical_time_tolerance_ms
        fills = await client.fills_between_times(
            start_ms=start_ms,
            end_ms=end_ms,
            coin=signal.coin,
            user=address,
        )
        item = match_episode(
            signal,
            fills,
            close_time_tolerance_ms=config.historical_time_tolerance_ms,
            close_price_tolerance_bps=config.historical_price_tolerance_bps,
            max_size_ratio_error=config.historical_max_size_ratio_error,
            entry_price_tolerance_bps=config.historical_entry_price_tolerance_bps,
        )
        evidence.append(item)
        if item.matched:
            matched_ids.append(signal.signal_id)
    attempted = len(selected)
    matched = len(matched_ids)
    ratio = D(matched) / D(attempted) if attempted else D("0")
    return HistoricalVerification(
        attempted=attempted,
        matched=matched,
        ratio=ratio,
        matched_signal_ids=tuple(matched_ids),
        evidence=tuple(evidence),
    )


async def verify_candidate_shortlist(
    *,
    ranked: tuple[CandidateFingerprint, ...],
    signals: tuple[CopySignal, ...],
    excluded_signal_ids: set[str],
    coverage_start_ms: int,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> tuple[HistoricalCandidateVerification, ...]:
    shortlist = [
        candidate
        for candidate in ranked
        if candidate.matched_anchors >= config.min_discovery_matches
    ][: max(1, config.max_candidates_to_verify)]
    results: list[HistoricalCandidateVerification] = []
    for candidate in shortlist:
        verification = await verify_candidate_historically(
            address=candidate.address,
            signals=signals,
            excluded_signal_ids=excluded_signal_ids,
            coverage_start_ms=coverage_start_ms,
            client=client,
            config=config,
        )
        results.append(
            HistoricalCandidateVerification(
                address=candidate.address,
                discovery_matches=candidate.matched_anchors,
                discovery_score=candidate.score,
                verification=verification,
            )
        )
    return tuple(
        sorted(
            results,
            key=lambda item: (
                item.verification.matched,
                item.verification.ratio,
                item.discovery_matches,
                item.discovery_score,
            ),
            reverse=True,
        )
    )


def select_historical_winner(
    results: tuple[HistoricalCandidateVerification, ...],
    *,
    min_matches: int,
    min_ratio: Decimal,
    min_match_gap: int,
) -> HistoricalCandidateVerification | None:
    qualified = [
        item
        for item in results
        if item.verification.matched >= min_matches
        and item.verification.ratio >= min_ratio
    ]
    if not qualified:
        return None
    best = qualified[0]
    if len(qualified) == 1:
        return best
    runner_up = qualified[1]
    if best.verification.matched - runner_up.verification.matched < min_match_gap:
        return None
    return best


async def resolve_source_public_trades(
    *,
    source: ExternalSourceSpec,
    wallet_registry: WalletRegistry,
    output_dir: Path,
    cache_dir: Path | None = None,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> dict[str, object]:
    del cache_dir
    signals = _source_signals(source)
    if len(signals) < 3:
        raise ValueError("insufficient external evidence")

    async with SqdHyperliquidFillsClient() as client:
        discovery = await discover_candidates(signals, client=client, config=config)
        historical_results = await verify_candidate_shortlist(
            ranked=discovery.ranked,
            signals=signals,
            excluded_signal_ids={signal.signal_id for signal in discovery.anchors},
            coverage_start_ms=discovery.coverage_start_ms,
            client=client,
            config=config,
        )
        winner = select_historical_winner(
            historical_results,
            min_matches=config.min_historical_matches,
            min_ratio=config.min_historical_ratio,
            min_match_gap=config.min_historical_winner_match_gap,
        )

    verified_address = winner.address if winner else None
    status = "VERIFIED" if verified_address else "UNRESOLVED"
    discovery_best = discovery.ranked[0] if discovery.ranked else None
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"public_trade_resolution_{source.id}.json"
    report = {
        "version": 3,
        "resolver_rule_version": "sqd-fill-wallet-identity-v3",
        "source": source.to_dict(),
        "status": status,
        "verified_address": verified_address,
        "candidate_unique": winner is not None,
        "coverage_start_ms": discovery.coverage_start_ms,
        "discovery_anchor_ids": [signal.signal_id for signal in discovery.anchors],
        "best_discovery_candidate": discovery_best.to_dict() if discovery_best else None,
        "historical_candidate_verifications": [
            item.to_dict() for item in historical_results
        ],
        "ranked_candidates": [item.to_dict() for item in discovery.ranked[:25]],
        "discovery_source": "SQD finalized Hyperliquid fills tape",
        "cost_model": "public SQD Portal endpoint; no requester-pays archive dependency",
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "held_out_verification_required": True,
            "discovery_only_candidate_can_verify": False,
        },
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if verified_address:
        wallet_registry.init()
        existing = wallet_registry.load()
        if not any(
            wallet.source_type == "hyperliquid_wallet"
            and wallet.source_ref.lower() == verified_address.lower()
            for wallet in existing
        ):
            wallet_registry.add(
                WalletSpec(
                    id=f"resolved-{source.id}-{verified_address[2:12]}",
                    label=f"{source.label} (SQD fill resolved)",
                    source_type="hyperliquid_wallet",
                    source_ref=verified_address,
                    stage="research",
                    coins=tuple(sorted({signal.coin for signal in signals})),
                    notes="SQD discovery + held-out episode verification; research only",
                )
            )

    return {
        "source_id": source.id,
        "status": status,
        "verified_address": verified_address,
        "best_discovery_matches": discovery_best.matched_anchors if discovery_best else 0,
        "candidate_unique": winner is not None,
        "historical_matches": winner.verification.matched if winner else 0,
        "historical_attempted": winner.verification.attempted if winner else 0,
        "report_path": str(report_path),
    }
