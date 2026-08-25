from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.identifier import (
    WalletIdentificationResult,
    _bind_source_identity,
    _load_source_evidence,
    _write_json_atomic,
    identify_wallet_from_csv,
)
from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.provenance import EvidenceSnapshot, jsonable_config
from hlcopy.resolver.public_trade_index import (
    DEFAULT_PUBLIC_TRADE_CONFIG,
    HistoricalCandidateVerification,
    HistoricalVerification,
    PublicTradeDiscoveryConfig,
    PublicTradeDiscoveryResult,
    _episode_is_covered,
    select_historical_winner,
)
from hlcopy.resolver.reverse_index import AnchorMatch, CandidateFingerprint, rank_candidates
from hlcopy.resolver.sqd_fills import signal_position_size
from hlcopy.resolver.sqd_position_aware import (
    LifecycleEpisodeEvidence,
    SqdFill,
    SqdHyperliquidFillsClient,
    fill_execution_id,
    reject_duplicate_lifecycle,
    reject_lifecycle,
)

D = Decimal
BPS = D("10000")
POSITION_EPSILON = D("0.000000000001")
FINAL_FLATTEN_PREFIX = "final-flatten:"

# Invo currently exposes allocation percentage rather than a trustworthy absolute
# source position size. This lane intentionally requires substantially more
# independent evidence than the absolute-size resolver and uses discovery only to
# generate a bounded candidate shortlist. Verification must replay disjoint,
# complete flat->open->flat Hyperliquid lifecycles.
SIZE_AGNOSTIC_MIN_SIGNALS = 20
SIZE_AGNOSTIC_ANCHORS = 8
SIZE_AGNOSTIC_MIN_DISCOVERY_MATCHES = 4
SIZE_AGNOSTIC_MAX_CANDIDATES = 6
SIZE_AGNOSTIC_MAX_CLOCK_MAD_MS = 8_000.0
SIZE_AGNOSTIC_MAX_MEDIAN_PRICE_BPS = D("15")
SIZE_AGNOSTIC_VERIFY_TRADES = 12
SIZE_AGNOSTIC_MIN_HISTORICAL_MATCHES = 5
SIZE_AGNOSTIC_MIN_HISTORICAL_RATIO = D("0.40")
SIZE_AGNOSTIC_MIN_WINNER_MATCH_GAP = 2
SIZE_AGNOSTIC_CLOSE_OFFSET_DRIFT_MS = 15_000


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def _direction_names(direction: str) -> tuple[str, str, str, str, str]:
    if direction == "LONG":
        return "open long", "long > long", "close long", "open short", "long > short"
    return "open short", "short > short", "close short", "open long", "short > long"


def _is_final_flatten(fill: SqdFill, *, direction: str) -> bool:
    close_name = "close long" if direction == "LONG" else "close short"
    if fill.direction.lower() != close_name:
        return False
    if fill.start_position is None:
        return False
    tolerance = max(POSITION_EPSILON, abs(fill.start_position) * D("0.000000001"))
    return abs(abs(fill.start_position) - fill.sz) <= tolerance


def _start_position_matches(fill: SqdFill, current_size: Decimal) -> bool:
    if fill.start_position is None:
        return True
    tolerance = max(POSITION_EPSILON, abs(current_size) * D("0.000000001"))
    return abs(abs(fill.start_position) - current_size) <= tolerance


def _failed(
    signal_id: str,
    *,
    close_time_error_ms: int | None = None,
    close_price_bps: Decimal | None = None,
    entry_time_error_ms: int | None = None,
    entry_price_bps: Decimal | None = None,
    reconstructed_entry: Decimal | None = None,
    reconstructed_size: Decimal | None = None,
    lifecycle_id: str | None = None,
    boundary_execution_id: str | None = None,
    final_execution_id: str | None = None,
    rejection_reason: str | None = None,
) -> LifecycleEpisodeEvidence:
    return LifecycleEpisodeEvidence(
        signal_id=signal_id,
        matched=False,
        close_time_error_ms=close_time_error_ms,
        close_price_bps=close_price_bps,
        close_size_ratio_error=None,
        entry_time_error_ms=entry_time_error_ms,
        entry_price_bps=entry_price_bps,
        entry_size_ratio_error=None,
        reconstructed_entry=reconstructed_entry,
        reconstructed_size=reconstructed_size,
        lifecycle_id=lifecycle_id,
        boundary_execution_id=boundary_execution_id,
        final_execution_id=final_execution_id,
        rejection_reason=rejection_reason,
    )


def _discovery_matches_without_size(
    signal,
    fills: list[SqdFill],
    *,
    window_ms: int,
    max_price_bps: Decimal,
) -> dict[str, AnchorMatch]:
    """Generate candidates only from proven final-flatten executions.

    No partial OID/cluster aggregate is allowed into this lane. A close is only a
    discovery vote when SQD startPosition proves that this exact fill flattened
    the wallet's position, and both the source close time and close price agree.
    This remains candidate generation only; it can never verify a wallet itself.
    """

    matches: dict[str, AnchorMatch] = {}
    for fill in fills:
        if not _is_final_flatten(fill, direction=signal.direction):
            continue
        close_offset = fill.time_ms - signal.closed_at_ms
        if abs(close_offset) > window_ms:
            continue
        exit_bps = _price_bps(signal.exit_price, fill.px)
        if exit_bps > max_price_bps:
            continue
        time_penalty = D(abs(close_offset)) / D("1000")
        price_penalty = min(exit_bps, D("50")) * D("2")
        quality = max(D("0"), D("100") - time_penalty - price_penalty)
        match = AnchorMatch(
            signal_id=signal.signal_id,
            user=fill.user,
            trade_id=f"{FINAL_FLATTEN_PREFIX}{fill_execution_id(fill)}",
            # Entry is deliberately not claimed during discovery. Mirroring the
            # close offset here makes rank_candidates evaluate only close-clock
            # consistency instead of fabricating an entry observation.
            open_offset_ms=close_offset,
            close_offset_ms=close_offset,
            offset_gap_ms=0,
            entry_price_bps=exit_bps,
            exit_price_bps=exit_bps,
            quality=quality,
        )
        current = matches.get(fill.user)
        if current is None or match.quality > current.quality:
            matches[fill.user] = match
    return matches


def _dedupe_discovery_executions(
    matches_by_anchor: dict[str, dict[str, AnchorMatch]],
) -> dict[str, dict[str, AnchorMatch]]:
    rows: list[tuple[str, str, AnchorMatch]] = []
    for signal_id, matches in matches_by_anchor.items():
        rows.extend((signal_id, address, match) for address, match in matches.items())
    rows.sort(key=lambda item: (item[2].quality, item[0]), reverse=True)
    used: set[tuple[str, str]] = set()
    output = {signal_id: {} for signal_id in matches_by_anchor}
    for signal_id, address, match in rows:
        execution_id = match.trade_id.removeprefix(FINAL_FLATTEN_PREFIX)
        if not execution_id or execution_id == match.trade_id:
            continue
        identity = (address, execution_id)
        if identity in used:
            continue
        used.add(identity)
        output[signal_id][address] = match
    return output


async def _discover_without_size(
    signals,
    *,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig,
) -> PublicTradeDiscoveryResult:
    coverage_start_ms = await client.coverage_start_ms()
    coverage_end_ms = await client.coverage_end_ms()
    window_ms = max(1, config.window_seconds) * 1000
    eligible = tuple(
        signal
        for signal in signals
        if signal.closed_at_ms - window_ms >= coverage_start_ms
        and signal.closed_at_ms + window_ms <= coverage_end_ms
    )
    if len(eligible) < SIZE_AGNOSTIC_ANCHORS:
        return PublicTradeDiscoveryResult((), (), coverage_start_ms, coverage_end_ms)

    anchors = select_anchor_trades(eligible, max_trades=SIZE_AGNOSTIC_ANCHORS)
    semaphore = asyncio.Semaphore(max(1, config.max_parallel_anchor_queries))

    async def match_anchor(signal):
        async with semaphore:
            fills = await client.fills_around(
                timestamp_ms=signal.closed_at_ms,
                coin=signal.coin,
                window_ms=window_ms,
            )
        return (
            signal.signal_id,
            _discovery_matches_without_size(
                signal,
                fills,
                window_ms=window_ms,
                max_price_bps=config.max_price_bps,
            ),
        )

    matches_by_anchor = dict(await asyncio.gather(*(match_anchor(row) for row in anchors)))
    unique_matches = _dedupe_discovery_executions(matches_by_anchor)
    return PublicTradeDiscoveryResult(
        rank_candidates(unique_matches, total_anchors=len(anchors)),
        anchors,
        coverage_start_ms,
        coverage_end_ms,
    )


def _match_lifecycle_without_size(
    signal,
    fills: list[SqdFill],
    *,
    expected_close_offset_ms: float,
    close_time_tolerance_ms: int,
    close_price_tolerance_bps: Decimal,
    entry_time_tolerance_ms: int,
    entry_price_tolerance_bps: Decimal,
) -> LifecycleEpisodeEvidence:
    open_name, add_name, reduce_name, opposite_open_name, flip_name = _direction_names(
        signal.direction
    )
    order_index = {id(fill): index for index, fill in enumerate(fills)}
    boundaries = [
        fill
        for fill in fills
        if fill.direction.lower() == open_name
        and fill.start_position == D("0")
        and abs(fill.time_ms - signal.opened_at_ms) <= entry_time_tolerance_ms
        and fill.time_ms <= signal.closed_at_ms + close_time_tolerance_ms
    ]
    if not boundaries:
        return _failed(signal.signal_id, rejection_reason="no_flat_to_open_boundary")
    boundaries.sort(
        key=lambda fill: (
            abs(fill.time_ms - signal.opened_at_ms),
            _price_bps(signal.entry_price, fill.px),
            fill.time_ms,
            fill.block_number,
            order_index.get(id(fill), 0),
        )
    )

    best_failure: LifecycleEpisodeEvidence | None = None
    for boundary in boundaries:
        entry_time_error = abs(boundary.time_ms - signal.opened_at_ms)
        boundary_execution_id = fill_execution_id(boundary)
        ordered = [
            (index, row)
            for index, row in enumerate(fills)
            if row.user == boundary.user
            and row.coin == boundary.coin
            and row.time_ms <= signal.closed_at_ms + close_time_tolerance_ms
        ]
        ordered.sort(key=lambda item: (item[1].time_ms, item[1].block_number, item[0]))
        boundary_position = next(
            (position for position, (_, row) in enumerate(ordered) if row is boundary),
            None,
        )
        if boundary_position is None:
            failure = _failed(
                signal.signal_id,
                entry_time_error_ms=entry_time_error,
                boundary_execution_id=boundary_execution_id,
                rejection_reason="boundary_not_in_ordered_stream",
            )
            best_failure = best_failure or failure
            continue

        current_size = D("0")
        current_entry_notional = D("0")
        gross_entry_size = D("0")
        gross_entry_notional = D("0")
        close_size = D("0")
        close_notional = D("0")
        close_last_ms: int | None = None
        failure: LifecycleEpisodeEvidence | None = None

        for _, fill in ordered[boundary_position:]:
            text = fill.direction.lower()
            if text not in {
                open_name,
                add_name,
                reduce_name,
                opposite_open_name,
                flip_name,
            }:
                continue
            if not _start_position_matches(fill, current_size):
                failure = _failed(
                    signal.signal_id,
                    entry_time_error_ms=entry_time_error,
                    reconstructed_size=gross_entry_size or None,
                    boundary_execution_id=boundary_execution_id,
                    rejection_reason="position_continuity_mismatch",
                )
                break

            if text in {open_name, add_name}:
                if current_size == 0 and fill is not boundary:
                    failure = _failed(
                        signal.signal_id,
                        entry_time_error_ms=entry_time_error,
                        reconstructed_size=gross_entry_size or None,
                        boundary_execution_id=boundary_execution_id,
                        rejection_reason="unexpected_reopen",
                    )
                    break
                current_size += fill.sz
                current_entry_notional += fill.sz * fill.px
                gross_entry_size += fill.sz
                gross_entry_notional += fill.sz * fill.px
                continue

            if text in {opposite_open_name, flip_name}:
                failure = _failed(
                    signal.signal_id,
                    entry_time_error_ms=entry_time_error,
                    reconstructed_size=gross_entry_size or None,
                    boundary_execution_id=boundary_execution_id,
                    rejection_reason="position_flip_before_expected_close",
                )
                break

            if current_size <= 0 or fill.sz > current_size + POSITION_EPSILON:
                failure = _failed(
                    signal.signal_id,
                    entry_time_error_ms=entry_time_error,
                    reconstructed_size=gross_entry_size or None,
                    boundary_execution_id=boundary_execution_id,
                    rejection_reason="invalid_reduction_size",
                )
                break

            average_entry = current_entry_notional / current_size
            close_size += fill.sz
            close_notional += fill.sz * fill.px
            close_last_ms = fill.time_ms if close_last_ms is None else max(close_last_ms, fill.time_ms)
            current_size = max(D("0"), current_size - fill.sz)
            current_entry_notional = average_entry * current_size
            if current_size > POSITION_EPSILON:
                continue

            current_size = D("0")
            final_execution_id = fill_execution_id(fill)
            lifecycle_id = (
                f"{boundary.user}:{boundary.coin}:{signal.direction}:"
                f"{boundary_execution_id}->{final_execution_id}"
            )
            if close_size <= 0 or gross_entry_size <= 0 or close_last_ms is None:
                failure = _failed(
                    signal.signal_id,
                    entry_time_error_ms=entry_time_error,
                    lifecycle_id=lifecycle_id,
                    boundary_execution_id=boundary_execution_id,
                    final_execution_id=final_execution_id,
                    rejection_reason="empty_lifecycle",
                )
                break

            reconstructed_entry = gross_entry_notional / gross_entry_size
            reconstructed_exit = close_notional / close_size
            entry_bps = _price_bps(signal.entry_price, reconstructed_entry)
            close_bps = _price_bps(signal.exit_price, reconstructed_exit)
            close_time_error = abs(close_last_ms - signal.closed_at_ms)
            signed_close_offset = close_last_ms - signal.closed_at_ms
            close_offset_drift = abs(signed_close_offset - expected_close_offset_ms)
            balance_error = abs(gross_entry_size - close_size) / gross_entry_size
            matched = (
                entry_time_error <= entry_time_tolerance_ms
                and close_time_error <= close_time_tolerance_ms
                and entry_bps <= entry_price_tolerance_bps
                and close_bps <= close_price_tolerance_bps
                and close_offset_drift <= SIZE_AGNOSTIC_CLOSE_OFFSET_DRIFT_MS
                and balance_error <= POSITION_EPSILON
            )
            evidence = LifecycleEpisodeEvidence(
                signal_id=signal.signal_id,
                matched=matched,
                close_time_error_ms=close_time_error,
                close_price_bps=close_bps,
                close_size_ratio_error=None,
                entry_time_error_ms=entry_time_error,
                entry_price_bps=entry_bps,
                entry_size_ratio_error=None,
                reconstructed_entry=reconstructed_entry,
                reconstructed_size=gross_entry_size,
                lifecycle_id=lifecycle_id,
                boundary_execution_id=boundary_execution_id,
                final_execution_id=final_execution_id,
                rejection_reason=None if matched else "size_agnostic_lifecycle_tolerance_mismatch",
            )
            if matched:
                return evidence
            failure = evidence
            break

        if failure is None:
            failure = _failed(
                signal.signal_id,
                entry_time_error_ms=entry_time_error,
                reconstructed_size=gross_entry_size or None,
                boundary_execution_id=boundary_execution_id,
                rejection_reason="no_final_flatten",
            )
        best_failure = best_failure or failure

    return best_failure or _failed(signal.signal_id)


async def _verify_candidate_without_size(
    *,
    candidate: CandidateFingerprint,
    signals,
    excluded_signal_ids: set[str],
    coverage_start_ms: int,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig,
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
    selected = eligible[:SIZE_AGNOSTIC_VERIFY_TRADES]
    if len(selected) < SIZE_AGNOSTIC_VERIFY_TRADES:
        return HistoricalVerification(0, 0, D("0"), (), ())

    forbidden_final_execution_ids = {
        match.trade_id.removeprefix(FINAL_FLATTEN_PREFIX)
        for match in candidate.matches
        if match.trade_id.startswith(FINAL_FLATTEN_PREFIX)
    }
    semaphore = asyncio.Semaphore(max(1, config.max_parallel_verification_queries))

    async def match_signal(signal):
        async with semaphore:
            fills = await client.fills_between_times(
                start_ms=signal.opened_at_ms - entry_tolerance_ms,
                end_ms=signal.closed_at_ms + close_tolerance_ms,
                coin=signal.coin,
                user=candidate.address,
            )
        return _match_lifecycle_without_size(
            signal,
            fills,
            expected_close_offset_ms=candidate.median_clock_offset_ms,
            close_time_tolerance_ms=close_tolerance_ms,
            close_price_tolerance_bps=config.historical_price_tolerance_bps,
            entry_time_tolerance_ms=entry_tolerance_ms,
            entry_price_tolerance_bps=config.historical_entry_price_tolerance_bps,
        )

    resolved = await asyncio.gather(*(match_signal(signal) for signal in selected))
    evidence: list[LifecycleEpisodeEvidence] = []
    matched_ids: list[str] = []
    used_lifecycle_ids: set[str] = set()
    for signal, raw_item in zip(selected, resolved, strict=True):
        item = raw_item
        if item.matched:
            if not item.lifecycle_id or not item.final_execution_id:
                item = reject_lifecycle(item, reason="missing_lifecycle_identity")
            elif item.final_execution_id in forbidden_final_execution_ids:
                item = reject_lifecycle(item, reason="discovery_lifecycle_reuse")
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


async def _verify_shortlist_without_size(
    *,
    ranked: tuple[CandidateFingerprint, ...],
    signals,
    excluded_signal_ids: set[str],
    coverage_start_ms: int,
    client: SqdHyperliquidFillsClient,
    config: PublicTradeDiscoveryConfig,
) -> tuple[HistoricalCandidateVerification, ...]:
    shortlist = [
        candidate
        for candidate in ranked
        if candidate.matched_anchors >= SIZE_AGNOSTIC_MIN_DISCOVERY_MATCHES
        and candidate.clock_offset_mad_ms <= SIZE_AGNOSTIC_MAX_CLOCK_MAD_MS
        and candidate.median_price_bps <= SIZE_AGNOSTIC_MAX_MEDIAN_PRICE_BPS
    ]
    if len(shortlist) > SIZE_AGNOSTIC_MAX_CANDIDATES:
        return ()

    results: list[HistoricalCandidateVerification] = []
    for candidate in shortlist:
        verification = await _verify_candidate_without_size(
            candidate=candidate,
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


def _confidence(
    *,
    discovery_matches: int,
    discovery_anchors: int,
    historical_matches: int,
    historical_attempted: int,
    candidate_unique: bool,
) -> Decimal:
    if historical_attempted == 0 or historical_matches == 0 or not candidate_unique:
        return D("0")
    discovery_ratio = D(discovery_matches) / D(max(1, discovery_anchors))
    historical_ratio = D(historical_matches) / D(max(1, historical_attempted))
    return min(D("0.999"), discovery_ratio * D("0.40") + historical_ratio * D("0.60"))


async def identify_wallet_from_csv_size_aware(
    evidence_path: Path,
    *,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
    config: PublicTradeDiscoveryConfig | None = None,
    client: SqdHyperliquidFillsClient | None = None,
    snapshot: EvidenceSnapshot | None = None,
    expected_source_identity: str | None = None,
) -> WalletIdentificationResult:
    """Use strict absolute-size matching when possible, otherwise strong sequence proof.

    The existing reviewed resolver remains untouched for evidence that contains an
    absolute source size. Size-agnostic evidence is never allowed to use that
    resolver's size-dependent discovery fallback; it follows the separate,
    stronger sequence gate implemented in this module.
    """

    del cache_dir
    effective_config = config or DEFAULT_PUBLIC_TRADE_CONFIG
    snapshot = snapshot or EvidenceSnapshot.from_path(evidence_path)
    imported = _load_source_evidence(snapshot, effective_config)
    if imported.rejected_rows:
        raise ValueError(
            f"external evidence contains {len(imported.rejected_rows)} malformed rows; "
            "fail closed before wallet discovery"
        )
    source_identity = _bind_source_identity(
        imported,
        expected_source_identity=expected_source_identity,
    )
    signals = imported.signals
    if len(signals) < 3:
        raise ValueError(f"insufficient independent trade evidence: {len(signals)}")

    sizes = tuple(signal_position_size(signal) for signal in signals)
    has_size = tuple(size is not None for size in sizes)
    if all(has_size):
        return await identify_wallet_from_csv(
            evidence_path,
            output_dir=output_dir,
            config=effective_config,
            client=client,
            snapshot=snapshot,
            expected_source_identity=expected_source_identity,
        )
    if any(has_size):
        raise ValueError(
            "mixed absolute-size and size-unknown evidence is not allowed in one identity run"
        )

    if len(signals) < SIZE_AGNOSTIC_MIN_SIGNALS:
        discovery = PublicTradeDiscoveryResult((), (), 0, 0)
        historical_results: tuple[HistoricalCandidateVerification, ...] = ()
        winner = None
        unresolved_reason = (
            f"size-agnostic identity requires at least {SIZE_AGNOSTIC_MIN_SIGNALS} "
            f"independent trades; observed {len(signals)}"
        )
    else:
        unresolved_reason = None

        async def resolve_with(sqd: SqdHyperliquidFillsClient):
            discovered = await _discover_without_size(
                signals,
                client=sqd,
                config=effective_config,
            )
            verified = await _verify_shortlist_without_size(
                ranked=discovered.ranked,
                signals=signals,
                excluded_signal_ids={signal.signal_id for signal in discovered.anchors},
                coverage_start_ms=discovered.coverage_start_ms,
                client=sqd,
                config=effective_config,
            )
            selected = select_historical_winner(
                verified,
                min_matches=SIZE_AGNOSTIC_MIN_HISTORICAL_MATCHES,
                min_ratio=SIZE_AGNOSTIC_MIN_HISTORICAL_RATIO,
                min_match_gap=SIZE_AGNOSTIC_MIN_WINNER_MATCH_GAP,
            )
            return discovered, verified, selected

        if client is None:
            async with SqdHyperliquidFillsClient() as owned_client:
                discovery, historical_results, winner = await resolve_with(owned_client)
        else:
            discovery, historical_results, winner = await resolve_with(client)

    discovery_best = discovery.ranked[0] if discovery.ranked else None
    winner_discovery = next(
        (
            item
            for item in discovery.ranked
            if winner is not None and item.address == winner.address
        ),
        None,
    )
    evidence_candidate = winner_discovery or discovery_best
    wallet = winner.address if winner else None
    status = "VERIFIED" if wallet else "UNRESOLVED"
    candidate = wallet or (discovery_best.address if discovery_best else None)
    historical_matches = winner.verification.matched if winner else 0
    historical_attempted = winner.verification.attempted if winner else 0
    discovery_matches = evidence_candidate.matched_anchors if evidence_candidate else 0
    candidate_unique = winner is not None
    confidence = _confidence(
        discovery_matches=discovery_matches,
        discovery_anchors=len(discovery.anchors),
        historical_matches=historical_matches,
        historical_attempted=historical_attempted,
        candidate_unique=candidate_unique,
    )

    report_path: Path | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        from datetime import UTC, datetime

        attempt_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        report_path = output_dir / (
            f"wallet_identification_{evidence_path.stem}_"
            f"{snapshot.sha256[:16]}_{attempt_id}.json"
        )
        _write_json_atomic(
            report_path,
            {
                "version": 12,
                "resolver_rule_version": "generic-sqd-fill-wallet-identity-v12-size-agnostic-sequence",
                "mode": "SIZE_AGNOSTIC_SEQUENCE",
                "input_file": str(evidence_path),
                "input_sha256": snapshot.sha256,
                "input_bytes": snapshot.size,
                "source_identity": source_identity,
                "effective_config": jsonable_config(effective_config),
                "size_agnostic_thresholds": {
                    "min_signals": SIZE_AGNOSTIC_MIN_SIGNALS,
                    "anchors": SIZE_AGNOSTIC_ANCHORS,
                    "min_discovery_matches": SIZE_AGNOSTIC_MIN_DISCOVERY_MATCHES,
                    "max_candidates": SIZE_AGNOSTIC_MAX_CANDIDATES,
                    "max_clock_mad_ms": SIZE_AGNOSTIC_MAX_CLOCK_MAD_MS,
                    "max_median_price_bps": str(SIZE_AGNOSTIC_MAX_MEDIAN_PRICE_BPS),
                    "verify_trades": SIZE_AGNOSTIC_VERIFY_TRADES,
                    "min_historical_matches": SIZE_AGNOSTIC_MIN_HISTORICAL_MATCHES,
                    "min_historical_ratio": str(SIZE_AGNOSTIC_MIN_HISTORICAL_RATIO),
                    "min_winner_match_gap": SIZE_AGNOSTIC_MIN_WINNER_MATCH_GAP,
                    "close_offset_drift_ms": SIZE_AGNOSTIC_CLOSE_OFFSET_DRIFT_MS,
                },
                "accepted_trades": len(signals),
                "rejected_rows": list(imported.rejected_rows),
                "duplicate_rows": list(imported.duplicate_rows),
                "overlapping_rows": list(imported.overlapping_rows),
                "unresolved_reason": unresolved_reason,
                "coverage_start_ms": discovery.coverage_start_ms,
                "coverage_end_ms": discovery.coverage_end_ms,
                "discovery_anchor_ids": [signal.signal_id for signal in discovery.anchors],
                "status": status,
                "wallet": wallet,
                "candidate": candidate,
                "candidate_unique": candidate_unique,
                "confidence": str(confidence),
                "best_discovery_candidate": (
                    discovery_best.to_dict() if discovery_best else None
                ),
                "winning_discovery_candidate": (
                    winner_discovery.to_dict() if winner_discovery else None
                ),
                "ranked_candidates": [item.to_dict() for item in discovery.ranked[:25]],
                "historical_candidate_verifications": [
                    item.to_dict() for item in historical_results
                ],
                "verification_source": "SQD finalized Hyperliquid fills tape",
                "safety": {
                    "auto_validation_promotion": False,
                    "auto_trading_promotion": False,
                    "unverified_candidate_exposed_as_wallet": False,
                    "absolute_size_lane_unchanged": True,
                    "discovery_final_flatten_only": True,
                    "discovery_cannot_verify": True,
                    "held_out_full_lifecycle_required": True,
                    "flat_to_open_boundary_required": True,
                    "exact_boundary_sequence_replay_required": True,
                    "entry_and_exit_price_required": True,
                    "close_clock_offset_consistency_required": True,
                    "discovery_held_out_execution_disjointness_required": True,
                    "one_vote_per_sqd_execution_in_discovery": True,
                    "one_vote_per_sqd_lifecycle_in_verification": True,
                    "unique_held_out_winner_required": True,
                },
            },
        )

    return WalletIdentificationResult(
        status=status,
        wallet=wallet,
        candidate=candidate,
        confidence=confidence,
        input_trades=len(signals),
        rejected_rows=len(imported.rejected_rows),
        discovery_matches=discovery_matches,
        discovery_anchors=len(discovery.anchors),
        candidate_unique=candidate_unique,
        historical_matches=historical_matches,
        historical_attempted=historical_attempted,
        verification_source="sqd_finalized_fills_size_agnostic_sequence",
        median_clock_offset_ms=(
            evidence_candidate.median_clock_offset_ms if evidence_candidate else None
        ),
        median_price_bps=(
            evidence_candidate.median_price_bps if evidence_candidate else None
        ),
        report_path=str(report_path) if report_path else None,
    )
