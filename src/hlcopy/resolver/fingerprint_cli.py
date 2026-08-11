from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from hlcopy.config import Settings
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.positions.reconstruction import reconstruct_positions
from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.reverse_index import ReverseResolverConfig, verify_candidate_officially
from hlcopy.resolver.source_registry import ExternalSourceRegistry, ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class Candidate:
    address: str
    offset_ms: int
    matches: int
    price_error_bps: Decimal


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("items", "fills", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _time_ms(value: object) -> int:
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number if number > 10_000_000_000 else number * 1000)
    text = str(value or "").strip()
    if text.isdigit():
        return _time_ms(int(text))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _px(row: dict[str, Any]) -> Decimal:
    return D(str(row.get("px", row.get("price", "0"))))


def _price_bps(expected: Decimal, actual: Decimal) -> Decimal:
    if expected <= 0 or actual <= 0:
        return D("Infinity")
    return abs(actual / expected - D("1")) * BPS


def _close_side(signal: CopySignal) -> str:
    return "A" if signal.direction == "LONG" else "B"


class FillIndexClient:
    def __init__(self, api_key: str, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"X-API-Key": api_key, "User-Agent": "hyperliquid-copy-engine/0.1"},
        )

    async def __aenter__(self) -> "FillIndexClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.aclose()

    async def fills(
        self,
        *,
        signal: CopySignal,
        center_ms: int,
        window_ms: int,
        limit: int,
        user: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, object] = {
            "coin": wire_coin(signal.coin),
            "side": _close_side(signal),
            "start_time": datetime.fromtimestamp((center_ms - window_ms) / 1000, UTC).isoformat(),
            "end_time": datetime.fromtimestamp((center_ms + window_ms) / 1000, UTC).isoformat(),
            "limit": limit,
            "order": "ASC",
        }
        if user:
            params["user"] = user.lower()
        response = await self.client.get(f"{self.base_url}/fills/", params=params)
        response.raise_for_status()
        return _items(response.json())


def _source_signals(source: ExternalSourceSpec) -> tuple[CopySignal, ...]:
    result = load_invo_closed_trades(Path(source.evidence_path))
    if result.rejected_rows:
        raise ValueError(f"external evidence has {len(result.rejected_rows)} malformed rows")
    return result.signals


def _matching_rows(
    signal: CopySignal,
    rows: list[dict[str, Any]],
    *,
    max_price_bps: Decimal,
) -> list[tuple[str, int, Decimal]]:
    matches: list[tuple[str, int, Decimal]] = []
    for row in rows:
        user = str(row.get("user") or "").lower()
        if not user.startswith("0x") or len(user) != 42:
            continue
        if canonical_coin(str(row.get("coin") or "")) != canonical_coin(signal.coin):
            continue
        if str(row.get("side") or "").upper() != _close_side(signal):
            continue
        try:
            when = _time_ms(row.get("time", row.get("time_dt")))
            error = _price_bps(signal.exit_price, _px(row))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if error <= max_price_bps:
            matches.append((user, when - signal.closed_at_ms, error))
    return matches


async def resolve_source(
    source: ExternalSourceSpec,
    *,
    index: FillIndexClient,
    official: HyperliquidHttpClient,
    registry: WalletRegistry,
    output_dir: Path,
    anchors_count: int,
    seed_window_ms: int,
    follow_window_ms: int,
    max_seed_candidates: int,
    max_price_bps: Decimal,
) -> dict[str, object]:
    signals = _source_signals(source)
    anchors = select_anchor_trades(signals, max_trades=max(3, anchors_count))
    anchor_ids = {signal.signal_id for signal in anchors}
    held_out = tuple(signal for signal in signals if signal.signal_id not in anchor_ids)
    if not anchors:
        raise ValueError("no usable anchors")

    seed = anchors[0]
    seed_rows = await index.fills(
        signal=seed,
        center_ms=seed.closed_at_ms,
        window_ms=seed_window_ms,
        limit=1000,
    )
    seed_matches = _matching_rows(seed, seed_rows, max_price_bps=max_price_bps)
    best_seed: dict[str, tuple[int, Decimal]] = {}
    for address, offset, error in seed_matches:
        current = best_seed.get(address)
        if current is None or error < current[1]:
            best_seed[address] = (offset, error)
    candidates = [
        Candidate(address, offset, 1, error)
        for address, (offset, error) in best_seed.items()
    ]
    candidates.sort(key=lambda item: (item.price_error_bps, abs(item.offset_ms)))
    candidates = candidates[:max_seed_candidates]

    for signal in anchors[1:]:
        survivors: list[Candidate] = []
        for candidate in candidates:
            center = signal.closed_at_ms + candidate.offset_ms
            rows = await index.fills(
                signal=signal,
                center_ms=center,
                window_ms=follow_window_ms,
                limit=100,
                user=candidate.address,
            )
            matches = _matching_rows(signal, rows, max_price_bps=max_price_bps)
            if not matches:
                continue
            best = min(matches, key=lambda item: item[2])
            new_offset = int(round((candidate.offset_ms * candidate.matches + best[1]) / (candidate.matches + 1)))
            survivors.append(
                Candidate(
                    candidate.address,
                    new_offset,
                    candidate.matches + 1,
                    candidate.price_error_bps + best[2],
                )
            )
        candidates = sorted(
            survivors,
            key=lambda item: (-item.matches, item.price_error_bps / D(item.matches), abs(item.offset_ms)),
        )
        if len(candidates) <= 1:
            break

    best = candidates[0] if candidates else None
    verification = None
    status = "UNRESOLVED"
    if best is not None and best.matches >= 3 and held_out:
        config = ReverseResolverConfig(
            official_verify_trades=min(8, len(held_out)),
            official_time_tolerance_ms=max(12_000, follow_window_ms),
            official_price_tolerance_bps=max_price_bps,
            min_official_matches=min(3, len(held_out)),
            min_official_ratio=D("0.60"),
        )
        verification = await verify_candidate_officially(
            address=best.address,
            signals=held_out,
            clock_offset_ms=float(best.offset_ms),
            client=official,
            config=config,
        )
        if verification.matched >= config.min_official_matches and verification.ratio >= config.min_official_ratio:
            status = "VERIFIED"
            registry.init()
            if not any(
                wallet.source_type == "hyperliquid_wallet" and wallet.source_ref.lower() == best.address
                for wallet in registry.load()
            ):
                registry.add(
                    WalletSpec(
                        id=f"resolved-{source.id}-{best.address[2:12]}",
                        label=f"{source.label} (fill fingerprint verified)",
                        source_type="hyperliquid_wallet",
                        source_ref=best.address,
                        stage="research",
                        coins=tuple(sorted({canonical_coin(signal.coin) for signal in signals})),
                        notes="Targeted fill fingerprint + held-out official Hyperliquid verification; research only",
                    )
                )
        else:
            status = "CANDIDATE_ONLY"

    payload = {
        "version": 1,
        "resolver": "targeted-fill-fingerprint-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_id": source.id,
        "anchors": len(anchors),
        "seed_rows_fetched": len(seed_rows),
        "seed_candidates_after_price_filter": len(best_seed),
        "candidate_cap": max_seed_candidates,
        "survivors": [
            {
                "address": item.address,
                "matches": item.matches,
                "offset_ms": item.offset_ms,
                "avg_price_error_bps": str(item.price_error_bps / D(item.matches)),
            }
            for item in candidates[:20]
        ],
        "official_verification": verification.to_dict() if verification else None,
        "status": status,
        "verified_address": best.address if status == "VERIFIED" and best else None,
        "safety": {"real_trading": False, "auto_validation_promotion": False},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"fill_fingerprint_{source.id}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    payload["report_path"] = str(path)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.resolver.fingerprint_cli")
    parser.add_argument("--source-registry", required=True, type=Path)
    parser.add_argument("--wallet-registry", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--id", required=True)
    parser.add_argument("--hypedexer-base-url", default="https://api.hypedexer.com")
    parser.add_argument("--anchors", type=int, default=6)
    parser.add_argument("--seed-window-ms", type=int, default=120_000)
    parser.add_argument("--follow-window-ms", type=int, default=15_000)
    parser.add_argument("--max-seed-candidates", type=int, default=50)
    parser.add_argument("--max-price-bps", type=Decimal, default=D("20"))
    return parser


async def _run(args: argparse.Namespace) -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("fill fingerprint resolver refuses REAL_TRADING_ENABLED=YES")
    api_key = os.getenv("HYPEDEXER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("HYPEDEXER_API_KEY is required")
    source = ExternalSourceRegistry(args.source_registry).get(args.id)
    settings = Settings.from_env()
    async with FillIndexClient(api_key, args.hypedexer_base_url) as index:
        async with HyperliquidHttpClient(
            settings.api_url,
            settings.leaderboard_url,
            concurrency=settings.http_concurrency,
        ) as official:
            result = await resolve_source(
                source,
                index=index,
                official=official,
                registry=WalletRegistry(args.wallet_registry),
                output_dir=args.output_dir,
                anchors_count=max(3, args.anchors),
                seed_window_ms=max(5_000, args.seed_window_ms),
                follow_window_ms=max(2_000, args.follow_window_ms),
                max_seed_candidates=max(5, args.max_seed_candidates),
                max_price_bps=max(D("0.5"), args.max_price_bps),
            )
    print(json.dumps(result, indent=2))


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
