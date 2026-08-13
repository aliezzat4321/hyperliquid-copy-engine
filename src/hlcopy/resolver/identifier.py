from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from hlcopy.resolver.public_trade_index import (
    DEFAULT_PUBLIC_TRADE_CONFIG,
    HistoricalVerification,
    PublicTradeDiscoveryConfig,
    candidate_is_unique,
    discover_candidates,
    verify_candidate_historically,
)
from hlcopy.resolver.sqd_fills import SqdHyperliquidFillsClient
from hlcopy.signals.generic_csv import GenericTradeImportResult, load_generic_closed_trades

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

    imported: GenericTradeImportResult = load_generic_closed_trades(evidence_path)
    signals = imported.signals
    if len(signals) < 3:
        raise ValueError(
            f"insufficient usable trade evidence: {len(signals)} accepted, "
            f"{len(imported.rejected_rows)} rejected"
        )

    historical: HistoricalVerification | None = None
    async with SqdHyperliquidFillsClient() as sqd:
        discovery = await discover_candidates(signals, client=sqd, config=config)
        ranked = discovery.ranked
        best = ranked[0] if ranked else None
        unique = candidate_is_unique(ranked, min_score_gap=config.min_runner_up_score_gap)
        if best is not None and best.matched_anchors >= config.min_discovery_matches and unique:
            historical = await verify_candidate_historically(
                address=best.address,
                signals=signals,
                excluded_signal_ids={signal.signal_id for signal in discovery.anchors},
                coverage_start_ms=discovery.coverage_start_ms,
                client=sqd,
                config=config,
            )

    best = discovery.ranked[0] if discovery.ranked else None
    status = "UNRESOLVED"
    wallet: str | None = None
    candidate = best.address if best else None
    if historical is not None and (
        historical.matched >= config.min_historical_matches
        and historical.ratio >= config.min_historical_ratio
    ):
        status = "VERIFIED"
        wallet = candidate

    historical_matches = historical.matched if historical else 0
    historical_attempted = historical.attempted if historical else 0
    confidence = _confidence(
        discovery_matches=best.matched_anchors if best else 0,
        discovery_anchors=len(discovery.anchors),
        historical_matches=historical_matches,
        historical_attempted=historical_attempted,
        candidate_unique=candidate_is_unique(
            discovery.ranked,
            min_score_gap=config.min_runner_up_score_gap,
        ),
    )

    report_path: Path | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"wallet_identification_{evidence_path.stem}.json"
        payload = {
            "version": 2,
            "resolver_rule_version": "generic-sqd-fill-wallet-identity-v2",
            "input_file": str(evidence_path),
            "detected_columns": imported.column_map,
            "accepted_trades": len(signals),
            "rejected_rows": list(imported.rejected_rows),
            "coverage_start_ms": discovery.coverage_start_ms,
            "discovery_anchor_ids": [signal.signal_id for signal in discovery.anchors],
            "status": status,
            "wallet": wallet,
            "candidate": candidate,
            "candidate_unique": candidate_is_unique(
                discovery.ranked,
                min_score_gap=config.min_runner_up_score_gap,
            ),
            "confidence": str(confidence),
            "best_candidate": best.to_dict() if best else None,
            "ranked_candidates": [item.to_dict() for item in discovery.ranked[:25]],
            "historical_verification": historical.to_dict() if historical else None,
            "verification_source": "SQD finalized Hyperliquid fills tape",
            "safety": {
                "auto_validation_promotion": False,
                "auto_trading_promotion": False,
                "unverified_candidate_exposed_as_wallet": False,
                "held_out_verification_required": True,
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
        discovery_matches=best.matched_anchors if best else 0,
        discovery_anchors=len(discovery.anchors),
        candidate_unique=candidate_is_unique(
            discovery.ranked,
            min_score_gap=config.min_runner_up_score_gap,
        ),
        historical_matches=historical_matches,
        historical_attempted=historical_attempted,
        verification_source="sqd_finalized_fills",
        median_clock_offset_ms=best.median_clock_offset_ms if best else None,
        median_price_bps=best.median_price_bps if best else None,
        report_path=str(report_path) if report_path else None,
    )
