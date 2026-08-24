from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.provenance import EvidenceSnapshot, jsonable_config
from hlcopy.resolver.public_trade_index import (
    DEFAULT_PUBLIC_TRADE_CONFIG,
    PublicTradeDiscoveryConfig,
    _episode_is_covered,
    discover_candidates,
    select_historical_winner,
    verify_candidate_shortlist,
)
from hlcopy.resolver.sqd_position_aware import SqdHyperliquidFillsClient
from hlcopy.signals.generic_csv import (
    GenericTradeImportResult,
    load_generic_closed_trades_bytes,
)

D = Decimal


@dataclass(frozen=True, slots=True)
class WalletIdentificationResult:
    status: str
    wallet: str | None
    candidate: str | None
    confidence: Decimal
    input_trades: int
    rejected_rows: int
    discovery_matches: int
    discovery_anchors: int
    candidate_unique: bool
    historical_matches: int
    historical_attempted: int
    verification_source: str
    median_clock_offset_ms: float | None
    median_price_bps: Decimal | None
    report_path: str | None

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


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


def _load_source_evidence(
    snapshot: EvidenceSnapshot,
    config: PublicTradeDiscoveryConfig,
) -> GenericTradeImportResult:
    """Normalize source evidence using the widest tolerances that can award a vote."""
    return load_generic_closed_trades_bytes(
        snapshot.data,
        near_duplicate_entry_time_ms=config.historical_entry_time_tolerance_ms,
        near_duplicate_close_time_ms=max(
            config.window_seconds * 1000,
            config.historical_time_tolerance_ms,
        ),
        near_duplicate_entry_price_bps=config.historical_entry_price_tolerance_bps,
        near_duplicate_exit_price_bps=max(
            config.max_price_bps,
            config.historical_price_tolerance_bps,
        ),
    )


async def identify_wallet_from_csv(
    evidence_path: Path,
    *,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
    config: PublicTradeDiscoveryConfig | None = None,
) -> WalletIdentificationResult:
    del cache_dir
    if config is None:
        config = DEFAULT_PUBLIC_TRADE_CONFIG

    snapshot = EvidenceSnapshot.from_path(evidence_path)
    imported = _load_source_evidence(snapshot, config)
    if imported.rejected_rows:
        raise ValueError(
            f"external evidence contains {len(imported.rejected_rows)} malformed rows; "
            "fail closed before wallet discovery"
        )
    signals = imported.signals
    if len(signals) < 3:
        raise ValueError(
            f"insufficient independent trade evidence: {len(signals)} accepted units, "
            f"{len(imported.rejected_rows)} rejected, "
            f"{len(imported.duplicate_rows)} exact duplicates removed, "
            f"{len(imported.overlapping_rows)} overlapping rows collapsed"
        )

    async with SqdHyperliquidFillsClient() as sqd:
        discovery = await discover_candidates(signals, client=sqd, config=config)
        historical_results = await verify_candidate_shortlist(
            ranked=discovery.ranked,
            signals=signals,
            excluded_signal_ids={signal.signal_id for signal in discovery.anchors},
            coverage_start_ms=discovery.coverage_start_ms,
            client=sqd,
            config=config,
        )
        winner = select_historical_winner(
            historical_results,
            min_matches=config.min_historical_matches,
            min_ratio=config.min_historical_ratio,
            min_match_gap=config.min_historical_winner_match_gap,
        )

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

    report_path: Path | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"wallet_identification_{evidence_path.stem}.json"
        payload = {
            "version": 9,
            "resolver_rule_version": "generic-sqd-fill-wallet-identity-v9",
            "input_file": str(evidence_path),
            "input_sha256": snapshot.sha256,
            "input_bytes": snapshot.size,
            "effective_config": jsonable_config(config),
            "detected_columns": imported.column_map,
            "accepted_trades": len(signals),
            "rejected_rows": list(imported.rejected_rows),
            "duplicate_rows": list(imported.duplicate_rows),
            "overlapping_rows": list(imported.overlapping_rows),
            "coverage_start_ms": discovery.coverage_start_ms,
            "coverage_end_ms": discovery.coverage_end_ms,
            "uncovered_signal_ids": uncovered_signal_ids,
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
                "held_out_verification_required": True,
                "discovery_held_out_execution_disjointness_required": True,
                "flat_to_open_boundary_required": True,
                "exact_boundary_sequence_replay_required": True,
                "entry_time_verification_required": True,
                "overlapping_source_positions_collapsed": True,
                "source_collapse_uses_effective_resolver_tolerances": True,
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
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
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
        verification_source="sqd_finalized_fills",
        median_clock_offset_ms=(
            evidence_candidate.median_clock_offset_ms if evidence_candidate else None
        ),
        median_price_bps=(
            evidence_candidate.median_price_bps if evidence_candidate else None
        ),
        report_path=str(report_path) if report_path else None,
    )
