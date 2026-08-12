from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.resolver.public_trade_index import (
    DEFAULT_PUBLIC_TRADE_CONFIG,
    PublicTradeDiscoveryConfig,
    discover_candidates,
)
from hlcopy.resolver.reverse_index import ReverseResolverConfig, verify_candidate_officially
from hlcopy.signals.generic_csv import GenericTradeImportResult, load_generic_closed_trades

D = Decimal


@dataclass(frozen=True, slots=True)
class WalletIdentificationResult:
    status: str
    wallet: str | None
    confidence: Decimal
    input_trades: int
    rejected_rows: int
    discovery_matches: int
    discovery_anchors: int
    official_matches: int
    official_attempted: int
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
    official_matches: int,
    official_attempted: int,
) -> Decimal:
    discovery_ratio = D(discovery_matches) / D(max(1, discovery_anchors))
    official_ratio = D(official_matches) / D(max(1, official_attempted))
    return min(D("0.999"), discovery_ratio * D("0.35") + official_ratio * D("0.65"))


async def identify_wallet_from_csv(
    evidence_path: Path,
    *,
    output_dir: Path | None = None,
    cache_dir: Path | None = None,
    config: PublicTradeDiscoveryConfig | None = None,
) -> WalletIdentificationResult:
    if config is None:
        config = DEFAULT_PUBLIC_TRADE_CONFIG

    imported: GenericTradeImportResult = load_generic_closed_trades(evidence_path)
    signals = imported.signals
    if len(signals) < 3:
        raise ValueError(
            f"insufficient usable trade evidence: {len(signals)} accepted, "
            f"{len(imported.rejected_rows)} rejected"
        )
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / "hlcopy-public-trades"

    ranked = discover_candidates(signals, cache_dir=cache_dir, config=config)
    best = ranked[0] if ranked else None
    discovery_anchors = min(max(3, config.anchor_trades), len(signals))
    official_matches = 0
    official_attempted = 0
    status = "UNRESOLVED"
    wallet: str | None = None

    if best is not None and best.matched_anchors >= config.min_discovery_matches:
        reverse_config = ReverseResolverConfig(
            anchor_trades=config.anchor_trades,
            primary_window_ms=config.window_seconds * 1000,
            fallback_window_ms=config.window_seconds * 1000,
            max_index_price_bps=config.max_price_bps,
            min_discovery_matches=config.min_discovery_matches,
            official_verify_trades=config.official_verify_trades,
            official_time_tolerance_ms=config.official_time_tolerance_ms,
            official_price_tolerance_bps=config.official_price_tolerance_bps,
            min_official_matches=config.min_official_matches,
            min_official_ratio=config.min_official_ratio,
        )
        async with HyperliquidHttpClient() as official:
            verification = await verify_candidate_officially(
                address=best.address,
                signals=signals,
                clock_offset_ms=best.median_clock_offset_ms,
                client=official,
                config=reverse_config,
            )
        official_matches = verification.matched
        official_attempted = verification.attempted
        if (
            verification.matched >= config.min_official_matches
            and verification.ratio >= config.min_official_ratio
        ):
            status = "VERIFIED"
            wallet = best.address
        else:
            status = "LIKELY"
            wallet = best.address

    confidence = _confidence(
        discovery_matches=best.matched_anchors if best else 0,
        discovery_anchors=discovery_anchors,
        official_matches=official_matches,
        official_attempted=official_attempted,
    )

    report_path: Path | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"wallet_identification_{evidence_path.stem}.json"
        payload = {
            "version": 1,
            "resolver_rule_version": "generic-public-trade-wallet-identity-v1",
            "input_file": str(evidence_path),
            "detected_columns": imported.column_map,
            "accepted_trades": len(signals),
            "rejected_rows": list(imported.rejected_rows),
            "status": status,
            "wallet": wallet,
            "confidence": str(confidence),
            "best_candidate": best.to_dict() if best else None,
            "ranked_candidates": [item.to_dict() for item in ranked[:25]],
            "official_verification": {
                "matched": official_matches,
                "attempted": official_attempted,
            },
            "safety": {
                "auto_validation_promotion": False,
                "auto_trading_promotion": False,
            },
        }
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return WalletIdentificationResult(
        status=status,
        wallet=wallet,
        confidence=confidence,
        input_trades=len(signals),
        rejected_rows=len(imported.rejected_rows),
        discovery_matches=best.matched_anchors if best else 0,
        discovery_anchors=discovery_anchors,
        official_matches=official_matches,
        official_attempted=official_attempted,
        median_clock_offset_ms=best.median_clock_offset_ms if best else None,
        median_price_bps=best.median_price_bps if best else None,
        report_path=str(report_path) if report_path else None,
    )
