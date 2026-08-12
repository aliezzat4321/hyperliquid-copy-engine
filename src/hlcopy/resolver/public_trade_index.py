from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import canonical_coin
from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.reverse_index import (
    AnchorMatch,
    CandidateFingerprint,
    ReverseResolverConfig,
    _best_matches_for_anchor,
    rank_candidates,
    verify_candidate_officially,
)
from hlcopy.resolver.source_registry import ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal


@dataclass(frozen=True, slots=True)
class PublicTradeDiscoveryConfig:
    anchor_trades: int = 8
    window_seconds: int = 120
    max_price_bps: Decimal = D("25")
    min_discovery_matches: int = 3
    min_runner_up_score_gap: Decimal = D("15")
    official_verify_trades: int = 6
    official_time_tolerance_ms: int = 12_000
    official_price_tolerance_bps: Decimal = D("12")
    min_official_matches: int = 3
    min_official_ratio: Decimal = D("0.60")


DEFAULT_PUBLIC_TRADE_CONFIG = PublicTradeDiscoveryConfig()


def _source_signals(source: ExternalSourceSpec) -> tuple[CopySignal, ...]:
    if source.adapter != "invo_closed_trades_csv":
        raise ValueError(f"unsupported public trade resolver adapter: {source.adapter}")
    result = load_invo_closed_trades(Path(source.evidence_path))
    if result.rejected_rows:
        raise ValueError(
            f"external evidence contains {len(result.rejected_rows)} malformed rows; fail closed"
        )
    return result.signals


def _hour_key(timestamp_ms: int) -> tuple[str, int]:
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return dt.strftime("%Y%m%d"), dt.hour


def _hours_for_window(center_ms: int, window_seconds: int) -> tuple[tuple[str, int], ...]:
    delta_ms = max(1, window_seconds) * 1000
    keys = {
        _hour_key(center_ms - delta_ms),
        _hour_key(center_ms),
        _hour_key(center_ms + delta_ms),
    }
    return tuple(sorted(keys))


def _aws_cp(uri: str, destination: Path) -> None:
    if os.getenv("HLCOPY_ALLOW_REQUESTER_PAYS", "NO").strip().upper() != "YES":
        raise RuntimeError(
            "historical Hyperliquid node_trades are requester-pays; cold-cache download is "
            "disabled by default. Pre-populate the cache from a free/local capture or set "
            "HLCOPY_ALLOW_REQUESTER_PAYS=YES to explicitly opt into authenticated AWS charges."
        )
    completed = subprocess.run(
        ["aws", "s3", "cp", uri, str(destination), "--request-payer", "requester"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"failed to fetch requester-pays Hyperliquid trade file {uri}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def _load_hour(date: str, hour: int, cache_dir: Path) -> list[dict[str, object]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"node_trades_{date}_{hour:02d}.jsonl"
    if not path.exists():
        uri = f"s3://hl-mainnet-node-data/node_trades/hourly/{date}/{hour}"
        _aws_cp(uri, path)
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _timestamp_ms(row: dict[str, object]) -> int | None:
    raw = row.get("time")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            numeric = float(raw)
            return int(numeric if numeric > 10_000_000_000 else numeric * 1000)
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _public_trade_matches(
    signal: CopySignal,
    rows: Iterable[dict[str, object]],
    *,
    window_ms: int,
    max_price_bps: Decimal,
) -> dict[str, AnchorMatch]:
    from hlcopy.resolver.reverse_index import IndexedCompletedTrade

    candidates: list[IndexedCompletedTrade] = []
    for row in rows:
        when = _timestamp_ms(row)
        if when is None or abs(when - signal.closed_at_ms) > window_ms:
            continue
        try:
            coin = canonical_coin(str(row.get("coin") or ""))
            px = D(str(row.get("px") or "0"))
        except (ArithmeticError, ValueError):
            continue
        if coin != canonical_coin(signal.coin) or px <= 0:
            continue
        side_info = row.get("side_info")
        if not isinstance(side_info, list) or len(side_info) != 2:
            continue
        chosen = side_info[1] if signal.direction == "LONG" else side_info[0]
        if not isinstance(chosen, dict):
            continue
        user = str(chosen.get("user") or "").lower()
        if not user.startswith("0x") or len(user) != 42:
            continue
        candidates.append(
            IndexedCompletedTrade(
                user=user,
                coin=coin,
                direction=signal.direction,
                start_ms=signal.opened_at_ms,
                end_ms=when,
                entry_price=signal.entry_price,
                exit_price=px,
                trade_id=str(row.get("hash") or ""),
                raw=dict(row),
            )
        )
    return _best_matches_for_anchor(
        signal,
        candidates,
        window_ms=window_ms,
        max_price_bps=max_price_bps,
    )


def discover_candidates(
    signals: tuple[CopySignal, ...],
    *,
    cache_dir: Path,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> tuple[CandidateFingerprint, ...]:
    anchors = select_anchor_trades(signals, max_trades=max(3, config.anchor_trades))
    matches_by_anchor: dict[str, dict[str, AnchorMatch]] = {}
    loaded: dict[tuple[str, int], list[dict[str, object]]] = {}
    window_ms = max(1, config.window_seconds) * 1000
    for signal in anchors:
        rows: list[dict[str, object]] = []
        for key in _hours_for_window(signal.closed_at_ms, config.window_seconds):
            if key not in loaded:
                loaded[key] = _load_hour(key[0], key[1], cache_dir)
            rows.extend(loaded[key])
        matches_by_anchor[signal.signal_id] = _public_trade_matches(
            signal,
            rows,
            window_ms=window_ms,
            max_price_bps=config.max_price_bps,
        )
    return rank_candidates(matches_by_anchor, total_anchors=len(anchors))


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


async def resolve_source_public_trades(
    *,
    source: ExternalSourceSpec,
    wallet_registry: WalletRegistry,
    output_dir: Path,
    cache_dir: Path | None = None,
    config: PublicTradeDiscoveryConfig = DEFAULT_PUBLIC_TRADE_CONFIG,
) -> dict[str, object]:
    signals = _source_signals(source)
    if len(signals) < 3:
        raise ValueError("insufficient external evidence")
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / "hlcopy-public-trades"
    ranked = discover_candidates(signals, cache_dir=cache_dir, config=config)
    best = ranked[0] if ranked else None
    unique = candidate_is_unique(ranked, min_score_gap=config.min_runner_up_score_gap)
    status = "UNRESOLVED"
    verified_address: str | None = None
    official_payload: dict[str, object] | None = None

    if best is not None and best.matched_anchors >= config.min_discovery_matches and unique:
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
        official_payload = verification.to_dict()
        if (
            verification.matched >= config.min_official_matches
            and verification.ratio >= config.min_official_ratio
        ):
            status = "VERIFIED"
            verified_address = best.address

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"public_trade_resolution_{source.id}.json"
    report = {
        "version": 1,
        "resolver_rule_version": "public-trade-wallet-identity-v1",
        "source": source.to_dict(),
        "status": status,
        "verified_address": verified_address,
        "candidate_unique": unique,
        "best_candidate": best.to_dict() if best else None,
        "official_verification": official_payload,
        "ranked_candidates": [item.to_dict() for item in ranked[:25]],
        "discovery_source": "cached Hyperliquid node_trades hourly files",
        "cost_model": (
            "zero incremental cost when cache is pre-populated; requester-pays AWS fallback "
            "requires explicit opt-in"
        ),
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
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
                    label=f"{source.label} (public-trade resolved)",
                    source_type="hyperliquid_wallet",
                    source_ref=verified_address,
                    stage="research",
                    coins=tuple(sorted({signal.coin for signal in signals})),
                    notes=(
                        "Public trade discovery + official Hyperliquid verification; "
                        "research only"
                    ),
                )
            )

    return {
        "source_id": source.id,
        "status": status,
        "verified_address": verified_address,
        "best_discovery_matches": best.matched_anchors if best else 0,
        "candidate_unique": unique,
        "report_path": str(report_path),
    }
