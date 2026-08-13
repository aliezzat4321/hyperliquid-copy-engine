from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.provenance import EvidenceSnapshot, jsonable_config
from hlcopy.resolver.reverse_index import (
    AnchorMatch,
    CandidateFingerprint,
    IndexedCompletedTrade,
    _best_matches_for_anchor,
    rank_candidates,
)
from hlcopy.resolver.source_registry import ExternalSourceSpec
from hlcopy.resolver.sqd_fills import EpisodeEvidence, aggregate_close_fills, signal_position_size
from hlcopy.resolver.sqd_position_aware import (
    LifecycleEpisodeEvidence,
    SqdFill,
    SqdHyperliquidFillsClient,
    match_episode,
    reject_duplicate_lifecycle,
)
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.generic_csv import load_generic_closed_trades_bytes
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal
BPS = D("10000")
POSITION_EPSILON = D("0.000000000001")
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
    historical_entry_time_tolerance_ms: int = 300_000
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
    coverage_end_ms: int


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


def _source_signals(
    source: ExternalSourceSpec,
    *,
    snapshot: EvidenceSnapshot,
) -> tuple[CopySignal, ...]:
    if source.adapter == "generic_closed_trades_csv":
        result = load_generic_closed_trades_bytes(snapshot.data)
    elif source.adapter == "invo_closed_trades_csv":
        with tempfile.TemporaryDirectory(prefix="hlcopy-evidence-") as temp_dir:
            snapshot_path = Path(temp_dir) / "evidence.csv"
            snapshot_path.write_bytes(snapshot.data)
            result = load_invo_closed_trades(snapshot_path)
    else:
        raise ValueError(f"unsupported public trade resolver adapter: {source.adapter}")
    if result.rejected_rows:
        raise ValueError(
            f"external evidence contains {len(result.rejected_rows)} malformed rows; fail closed"
        )
    return result.signals


def _episode_is_covered(
    signal: CopySignal,
    *,
    coverage_start_ms: int,
    coverage_end_ms: int,
    entry_margin_ms: int = 0,
    close_margin_ms: int = 0,
) -> bool:
    return (
        signal.opened_at_ms - max(0, entry_margin_ms) >= coverage_start_ms
        and signal.closed_at_ms + max(0, close_margin_ms) <= coverage_end_ms
    )


def _close_window_is_covered(
    signal: CopySignal,
    *,
    coverage_start_ms: int,
    coverage_end_ms: int,
    window_ms: int,
) -> bool:
    return (
        signal.closed_at_ms - window_ms >= coverage_start_ms
        and signal.closed_at_ms + window_ms <= coverage_end_ms
    )


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def _fill_execution_id(fill: SqdFill) -> str:
    if fill.tid:
        return f"tid:{fill.tid}"
    return f"oid:{fill.oid}:t:{fill.time_ms}:sz:{fill.sz}:px:{fill.px}"


def _is_final_flatten(fill: SqdFill, *, direction: str) -> bool:
    close_name = "close long" if direction == "LONG" else "close short"
    if fill.direction.lower() != close_name:
        return False
    start_position = getattr(fill, "start_position", None)
    if start_position is None:
        return False
    tolerance = max(POSITION_EPSILON, abs(start_position) * D("0.000000001"))
    return abs(abs(start_position) - fill.sz) <= tolerance


def _close_execution_id(
    close: object,
    fills: list[SqdFill],
    *,
    direction: str,
) -> str:
    user = str(getattr(close, "user"))
    last_time_ms = int(getattr(close, "last_time_ms"))
    final_flattens = [
        fill
        for fill in fills
        if fill.user == user
        and _is_final_flatten(fill, direction=direction)
        and abs(fill.time_ms - last_time_ms) <= 2_500
    ]
    if final_flattens:
        fill = min(final_flattens, key=lambda row: abs(row.time_ms - last_time_ms))
        return f"final-flatten:{_fill_execution_id(fill)}"
    return (
        f"close:{user}:{getattr(close, 'coin')}:{direction}:"
        f"{getattr(close, 'first_time_ms')}:{last_time_ms}:"
        f"{getattr(close, 'size')}:{getattr(close, 'avg_price')}"
    )


def _dedupe_reused_execution_matches(
    matches_by_anchor: dict[str, dict[str, AnchorMatch]],
) -> dict[str, dict[str, AnchorMatch]]:
    """One concrete SQD close/final-flatten may contribute only one anchor vote per wallet."""
    rows: list[tuple[str, str, AnchorMatch]] = []
    for signal_id, matches in matches_by_anchor.items():
        rows.extend((signal_id, address, match) for address, match in matches.items())
    rows.sort(key=lambda row: (row[2].quality, row[0]), reverse=True)
    used: set[tuple[str, str]] = set()
    output: dict[str, dict[str, AnchorMatch]] = {
        signal_id: {} for signal_id in matches_by_anchor
    }
    for signal_id, address, match in rows:
        identity = (address, match.trade_id)
        if identity in used:
            continue
        used.add(identity)
        output[signal_id][address] = match
    return output


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
                trade_id=_close_execution_id(close, fills, direction=signal.direction),
                raw={
                    "aggregated_close_size": str(close.size),
                    "fill_count": close.fill_count,
                    "closed_pnl": str(close.closed_pnl),
                },
            )
        )
    matches = _best_matches_for_anchor(
        signal,
        candidates,
        window_ms=window_ms,
        max_price_bps=max_price_bps,
    )

    for fill in fills:
        if not _is_final_flatten(fill, direction=signal.direction):
            continue
        close_offset = fill.time_ms - signal.closed_at_ms
        if abs(close_offset) > window_ms or fill.user in matches:
            continue
        exit_bps = _price_bps(signal.exit_price, fill.px)
        time_penalty = D(abs(close_offset)) / D("1000")
        price_penalty = min(exit_bps, D("50"))
        quality = max(D("0"), D("75") - time_penalty - price_penalty)
        matches[fill.user] = AnchorMatch(
            signal_id=signal.signal_id,
            user=fill.user,
            trade_id=f"final-flatten:{_fill_execution_id(fill)}",
            open_offset_ms=0,
            close_offset_ms=close_offset,
            offset_gap_ms=abs(close_offset),
            entry_price_bps=D("0"),
            exit_price_bps=exit_bps,
            quality=quality,
        )
    return matches


async def discover_candidates(
    signals: tuple[CopySignal, ...],
    *,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> PublicTradeDiscoveryResult:
    coverage_start_ms = await client.coverage_start_ms()
    coverage_end_ms = await client.coverage_end_ms()
    window_ms = max(1, config.window_seconds) * 1000
    eligible = tuple(
        signal
        for signal in signals
        if _close_window_is_covered(
            signal,
            coverage_start_ms=coverage_start_ms,
            coverage_end_ms=coverage_end_ms,
            window_ms=window_ms,
        )
    )
    if len(eligible) < 3:
        return PublicTradeDiscoveryResult((), (), coverage_start_ms, coverage_end_ms)
    anchors = select_anchor_trades(eligible, max_trades=max(3, config.anchor_trades))
    matches_by_anchor: dict[str, dict[str, AnchorMatch]] = {}
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
    unique_matches = _dedupe_reused_execution_matches(matches_by_anchor)
    return PublicTradeDiscoveryResult(
        rank_candidates(unique_matches, total_anchors=len(anchors)),
        anchors,
        coverage_start_ms,
        coverage_end_ms,
    )


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
    return (
        best.matched_anchors > runner_up.matched_anchors
        and best.score - runner_up.score >= min_score_gap
    )


async def verify_candidate_historically(
    *,
    address: str,
    signals: tuple[CopySignal, ...],
    excluded_signal_ids: set[str],
    coverage_start_ms: int,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> HistoricalVerification:
    coverage_end_ms = await client.coverage_end_ms()
    entry_tolerance_ms = max(1, config.historical_entry_time_tolerance_ms)
    close_tolerance_ms = max(0, config.historical_time_tolerance_ms)
    eligible = [
        signal
        for signal in signals
        if signal.signal_id not in excluded_signal_ids
        and _episode_is_covered(
            signal,
            coverage_start_ms=coverage_start_ms,
            coverage_end_ms=coverage_end_ms,
            entry_margin_ms=entry_tolerance_ms,
            close_margin_ms=close_tolerance_ms,
        )
    ]
    eligible.sort(key=lambda item: (item.closed_at_ms, item.signal_id), reverse=True)
    selected = eligible[: config.historical_verify_trades]
    evidence: list[EpisodeEvidence] = []
    matched_ids: list[str] = []
    used_lifecycle_ids: set[str] = set()
    for signal in selected:
        fills = await client.fills_between_times(
            start_ms=signal.opened_at_ms - entry_tolerance_ms,
            end_ms=signal.closed_at_ms + close_tolerance_ms,
            coin=signal.coin,
            user=address,
        )
        item = match_episode(
            signal,
            fills,
            close_time_tolerance_ms=close_tolerance_ms,
            close_price_tolerance_bps=config.historical_price_tolerance_bps,
            max_size_ratio_error=config.historical_max_size_ratio_error,
            entry_time_tolerance_ms=entry_tolerance_ms,
            entry_price_tolerance_bps=config.historical_entry_price_tolerance_bps,
        )
        if item.matched:
            if not item.lifecycle_id:
                item = LifecycleEpisodeEvidence(
                    signal_id=item.signal_id,
                    matched=False,
                    close_time_error_ms=item.close_time_error_ms,
                    close_price_bps=item.close_price_bps,
                    close_size_ratio_error=item.close_size_ratio_error,
                    entry_time_error_ms=item.entry_time_error_ms,
                    entry_price_bps=item.entry_price_bps,
                    entry_size_ratio_error=item.entry_size_ratio_error,
                    reconstructed_entry=item.reconstructed_entry,
                    reconstructed_size=item.reconstructed_size,
                    lifecycle_id=None,
                    rejection_reason="missing_lifecycle_identity",
                )
            elif item.lifecycle_id in used_lifecycle_ids:
                item = reject_duplicate_lifecycle(item)
            else:
                used_lifecycle_ids.add(item.lifecycle_id)
        evidence.append(item)
        if item.matched:
            matched_ids.append(signal.signal_id)
    attempted = len(evidence)
    matched = len(matched_ids)
    return HistoricalVerification(
        attempted=attempted,
        matched=matched,
        ratio=D(matched) / D(attempted) if attempted else D("0"),
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
    ]
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
    if not results:
        return None
    best = results[0]
    if best.verification.matched < min_matches or best.verification.ratio < min_ratio:
        return None
    if len(results) > 1:
        runner_up = results[1]
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
    evidence_path = Path(source.evidence_path)
    snapshot = EvidenceSnapshot.from_path(evidence_path)
    signals = _source_signals(source, snapshot=snapshot)
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
    uncovered_signal_ids = [
        signal.signal_id
        for signal in signals
        if not _episode_is_covered(
            signal,
            coverage_start_ms=discovery.coverage_start_ms,
            coverage_end_ms=discovery.coverage_end_ms,
            entry_margin_ms=config.historical_entry_time_tolerance_ms,
            close_margin_ms=config.historical_time_tolerance_ms,
        )
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"public_trade_resolution_{source.id}.json"
    report = {
        "version": 8,
        "resolver_rule_version": "sqd-fill-wallet-identity-v8",
        "source": source.to_dict(),
        "input_sha256": snapshot.sha256,
        "input_bytes": snapshot.size,
        "effective_config": jsonable_config(config),
        "status": status,
        "verified_address": verified_address,
        "candidate_unique": winner is not None,
        "coverage_start_ms": discovery.coverage_start_ms,
        "coverage_end_ms": discovery.coverage_end_ms,
        "uncovered_signal_ids": uncovered_signal_ids,
        "discovery_anchor_ids": [signal.signal_id for signal in discovery.anchors],
        "best_discovery_candidate": discovery_best.to_dict() if discovery_best else None,
        "historical_candidate_verifications": [item.to_dict() for item in historical_results],
        "ranked_candidates": [item.to_dict() for item in discovery.ranked[:25]],
        "discovery_source": "SQD finalized Hyperliquid fills tape",
        "cost_model": "public SQD Portal endpoint; no requester-pays archive dependency",
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "held_out_verification_required": True,
            "flat_to_open_boundary_required": True,
            "entry_time_verification_required": True,
            "exact_duplicate_rows_removed": True,
            "one_vote_per_sqd_execution_in_discovery": True,
            "one_vote_per_sqd_lifecycle_in_verification": True,
            "full_lifecycle_exit_aggregation_required": True,
            "complete_tolerance_windows_required_in_coverage": True,
            "immutable_input_snapshot_required": True,
            "discovery_only_candidate_can_verify": False,
            "all_threshold_finalists_verified": True,
            "coverage_fail_closed": True,
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
