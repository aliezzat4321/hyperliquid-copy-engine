from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable

import httpx

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import canonical_coin, wire_coin
from hlcopy.models import Fill
from hlcopy.positions.reconstruction import reconstruct_positions
from hlcopy.positions.state_machine import PositionReconstructionError
from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.source_registry import ExternalSourceSpec
from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.signals.invo import CopySignal, load_invo_closed_trades

D = Decimal
BPS = D("10000")


@dataclass(frozen=True, slots=True)
class ReverseResolverConfig:
    anchor_trades: int = 8
    primary_window_ms: int = 120_000
    fallback_window_ms: int = 600_000
    max_index_rows_per_anchor: int = 5_000
    index_page_size: int = 1_000
    max_index_price_bps: Decimal = D("25")
    min_discovery_matches: int = 3
    official_verify_trades: int = 6
    official_time_tolerance_ms: int = 12_000
    official_price_tolerance_bps: Decimal = D("12")
    min_official_matches: int = 3
    min_official_ratio: Decimal = D("0.60")


DEFAULT_REVERSE_CONFIG = ReverseResolverConfig()


@dataclass(frozen=True, slots=True)
class IndexedCompletedTrade:
    user: str
    coin: str
    direction: str
    start_ms: int
    end_ms: int
    entry_price: Decimal
    exit_price: Decimal
    trade_id: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AnchorMatch:
    signal_id: str
    user: str
    trade_id: str
    open_offset_ms: int
    close_offset_ms: int
    offset_gap_ms: int
    entry_price_bps: Decimal
    exit_price_bps: Decimal
    quality: Decimal

    def to_dict(self) -> dict[str, object]:
        row = asdict(self)
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }


@dataclass(frozen=True, slots=True)
class CandidateFingerprint:
    address: str
    matched_anchors: int
    total_anchors: int
    match_ratio: Decimal
    median_clock_offset_ms: float
    clock_offset_mad_ms: float
    median_offset_gap_ms: float
    median_price_bps: Decimal
    score: Decimal
    matches: tuple[AnchorMatch, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "matched_anchors": self.matched_anchors,
            "total_anchors": self.total_anchors,
            "match_ratio": str(self.match_ratio),
            "median_clock_offset_ms": self.median_clock_offset_ms,
            "clock_offset_mad_ms": self.clock_offset_mad_ms,
            "median_offset_gap_ms": self.median_offset_gap_ms,
            "median_price_bps": str(self.median_price_bps),
            "score": str(self.score),
            "matches": [match.to_dict() for match in self.matches],
        }


@dataclass(frozen=True, slots=True)
class OfficialVerification:
    attempted: int
    matched: int
    ratio: Decimal
    matched_signal_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "matched": self.matched,
            "ratio": str(self.ratio),
            "matched_signal_ids": list(self.matched_signal_ids),
        }


@dataclass(frozen=True, slots=True)
class ReverseResolverRun:
    source_id: str
    status: str
    address: str | None
    discovery_matches: int
    official_matches: int
    report_path: str


def _timestamp_ms(value: object) -> int:
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            return int(numeric)
        return int(numeric * 1000)
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is blank")
    if text.isdigit():
        return _timestamp_ms(int(text))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _decimal(value: object) -> Decimal:
    return D(str(value))


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("items", "trades", "rows", "results", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def parse_completed_trade(row: dict[str, Any]) -> IndexedCompletedTrade | None:
    try:
        user = str(row["user"]).lower()
        if not user.startswith("0x") or len(user) != 42:
            return None
        direction = str(row["direction"]).upper()
        if direction not in {"LONG", "SHORT"}:
            return None
        return IndexedCompletedTrade(
            user=user,
            coin=canonical_coin(row["coin"]),
            direction=direction,
            start_ms=_timestamp_ms(row["start_time"]),
            end_ms=_timestamp_ms(row["end_time"]),
            entry_price=_decimal(row["entry_price"]),
            exit_price=_decimal(row["exit_price"]),
            trade_id=str(row.get("trade_id") or ""),
            raw=row,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


class HypeDexerCompletedTradesClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.hypedexer.com",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("HypeDexer API key is required")
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "X-API-Key": api_key.strip(),
                "User-Agent": "hyperliquid-copy-engine/0.1",
            },
        )

    async def __aenter__(self) -> HypeDexerCompletedTradesClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.aclose()

    async def completed_trades(
        self,
        *,
        coin: str,
        direction: str,
        start_ms: int,
        end_ms: int,
        max_rows: int,
        page_size: int,
        user: str | None = None,
    ) -> list[IndexedCompletedTrade]:
        rows: list[IndexedCompletedTrade] = []
        offset = 0
        page_size = max(1, min(1000, page_size))
        max_rows = max(1, max_rows)
        while len(rows) < max_rows:
            params: dict[str, object] = {
                "coin": wire_coin(coin),
                "direction": direction.lower(),
                "start_time": datetime.fromtimestamp(start_ms / 1000, UTC).isoformat(),
                "end_time": datetime.fromtimestamp(end_ms / 1000, UTC).isoformat(),
                "limit": min(page_size, max_rows - len(rows)),
                "offset": offset,
                "sort_by": "end_time",
                "sort_dir": "ASC",
            }
            if user is not None:
                params["user"] = user.lower()
            response = await self.client.get(f"{self.base_url}/completed-trades/", params=params)
            response.raise_for_status()
            raw_items = _extract_items(response.json())
            parsed = [trade for row in raw_items if (trade := parse_completed_trade(row))]
            rows.extend(parsed)
            if len(raw_items) < int(params["limit"]):
                break
            offset += len(raw_items)
        return rows


def _match_anchor(
    signal: CopySignal,
    trade: IndexedCompletedTrade,
    *,
    window_ms: int,
    max_price_bps: Decimal,
) -> AnchorMatch | None:
    if canonical_coin(signal.coin) != canonical_coin(trade.coin):
        return None
    if signal.direction != trade.direction:
        return None
    open_offset = trade.start_ms - signal.opened_at_ms
    close_offset = trade.end_ms - signal.closed_at_ms
    if abs(open_offset) > window_ms or abs(close_offset) > window_ms:
        return None
    entry_bps = _price_bps(signal.entry_price, trade.entry_price)
    exit_bps = _price_bps(signal.exit_price, trade.exit_price)
    if entry_bps > max_price_bps or exit_bps > max_price_bps:
        return None
    offset_gap = abs(open_offset - close_offset)
    time_penalty = D(str(offset_gap)) / D("1000")
    price_penalty = (entry_bps + exit_bps) / D("2")
    window_penalty = D(str(abs(open_offset) + abs(close_offset))) / D(str(max(1, window_ms)))
    quality = max(D("0"), D("100") - price_penalty * D("2") - time_penalty - window_penalty)
    return AnchorMatch(
        signal_id=signal.signal_id,
        user=trade.user,
        trade_id=trade.trade_id,
        open_offset_ms=open_offset,
        close_offset_ms=close_offset,
        offset_gap_ms=offset_gap,
        entry_price_bps=entry_bps,
        exit_price_bps=exit_bps,
        quality=quality,
    )


def _best_matches_for_anchor(
    signal: CopySignal,
    trades: Iterable[IndexedCompletedTrade],
    *,
    window_ms: int,
    max_price_bps: Decimal,
) -> dict[str, AnchorMatch]:
    best: dict[str, AnchorMatch] = {}
    for trade in trades:
        match = _match_anchor(
            signal,
            trade,
            window_ms=window_ms,
            max_price_bps=max_price_bps,
        )
        if match is None:
            continue
        current = best.get(match.user)
        if current is None or match.quality > current.quality:
            best[match.user] = match
    return best


def _median_decimal(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return D("0")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / D("2")


def rank_candidates(
    matches_by_anchor: dict[str, dict[str, AnchorMatch]],
    *,
    total_anchors: int,
) -> tuple[CandidateFingerprint, ...]:
    by_user: dict[str, list[AnchorMatch]] = {}
    for anchor_matches in matches_by_anchor.values():
        for address, match in anchor_matches.items():
            by_user.setdefault(address, []).append(match)

    ranked: list[CandidateFingerprint] = []
    for address, matches in by_user.items():
        offsets = [float(match.close_offset_ms) for match in matches]
        clock_offset = median(offsets)
        mad = median([abs(value - clock_offset) for value in offsets]) if offsets else 0.0
        offset_gap = median([float(match.offset_gap_ms) for match in matches])
        prices = [
            (match.entry_price_bps + match.exit_price_bps) / D("2")
            for match in matches
        ]
        median_price = _median_decimal(prices)
        ratio = D(len(matches)) / D(max(1, total_anchors))
        quality = _median_decimal([match.quality for match in matches])
        score = (
            ratio * D("70")
            + quality * D("0.30")
            - min(D("10"), D(str(mad)) / D("1000"))
        )
        ranked.append(
            CandidateFingerprint(
                address=address,
                matched_anchors=len(matches),
                total_anchors=total_anchors,
                match_ratio=ratio,
                median_clock_offset_ms=clock_offset,
                clock_offset_mad_ms=mad,
                median_offset_gap_ms=offset_gap,
                median_price_bps=median_price,
                score=score,
                matches=tuple(sorted(matches, key=lambda item: item.signal_id)),
            )
        )
    return tuple(
        sorted(
            ranked,
            key=lambda item: (
                item.matched_anchors,
                item.score,
                -item.median_price_bps,
                item.address,
            ),
            reverse=True,
        )
    )


def _source_signals(source: ExternalSourceSpec) -> tuple[CopySignal, ...]:
    if source.adapter != "invo_closed_trades_csv":
        raise ValueError(f"unsupported reverse resolver adapter: {source.adapter}")
    result = load_invo_closed_trades(Path(source.evidence_path))
    if result.rejected_rows:
        raise ValueError(
            f"external evidence contains {len(result.rejected_rows)} malformed rows; fail closed"
        )
    return result.signals


def _flatten_pages(pages: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in pages:
        payload = getattr(page, "response_payload", None)
        if isinstance(payload, list):
            rows.extend(row for row in payload if isinstance(row, dict))
    return rows


def _episode_matches_signal(
    signal: CopySignal,
    episode: Any,
    *,
    clock_offset_ms: float,
    time_tolerance_ms: int,
    price_tolerance_bps: Decimal,
) -> bool:
    if canonical_coin(episode.coin) != canonical_coin(signal.coin):
        return False
    if str(episode.direction).upper() != signal.direction:
        return False
    if episode.opened_at_ms is None or episode.closed_at_ms is None:
        return False
    if episode.avg_entry is None or episode.avg_exit is None:
        return False
    expected_open = signal.opened_at_ms + clock_offset_ms
    expected_close = signal.closed_at_ms + clock_offset_ms
    if abs(episode.opened_at_ms - expected_open) > time_tolerance_ms:
        return False
    if abs(episode.closed_at_ms - expected_close) > time_tolerance_ms:
        return False
    if _price_bps(signal.entry_price, episode.avg_entry) > price_tolerance_bps:
        return False
    if _price_bps(signal.exit_price, episode.avg_exit) > price_tolerance_bps:
        return False
    return True


async def verify_candidate_officially(
    *,
    address: str,
    signals: tuple[CopySignal, ...],
    clock_offset_ms: float,
    client: HyperliquidHttpClient,
    config: ReverseResolverConfig,
) -> OfficialVerification:
    selected = tuple(
        sorted(signals, key=lambda item: (item.closed_at_ms, item.signal_id), reverse=True)[
            : config.official_verify_trades
        ]
    )
    matched_ids: list[str] = []
    for signal in selected:
        expected_open = int(signal.opened_at_ms + clock_offset_ms)
        expected_close = int(signal.closed_at_ms + clock_offset_ms)
        padding = max(30_000, config.official_time_tolerance_ms * 2)
        pages = await client.user_fills_by_time(
            address,
            expected_open - padding,
            expected_close + padding,
        )
        raw_rows = _flatten_pages(pages)
        fills: list[Fill] = []
        for row in raw_rows:
            if canonical_coin(row.get("coin", "")) != canonical_coin(signal.coin):
                continue
            try:
                fills.append(Fill.from_raw(address, row))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        if not fills:
            continue
        try:
            episodes, _states = reconstruct_positions(fills)
        except PositionReconstructionError:
            continue
        if any(
            _episode_matches_signal(
                signal,
                episode,
                clock_offset_ms=clock_offset_ms,
                time_tolerance_ms=config.official_time_tolerance_ms,
                price_tolerance_bps=config.official_price_tolerance_bps,
            )
            for episode in episodes
        ):
            matched_ids.append(signal.signal_id)
    attempted = len(selected)
    matched = len(matched_ids)
    ratio = D(matched) / D(attempted) if attempted else D("0")
    return OfficialVerification(
        attempted=attempted,
        matched=matched,
        ratio=ratio,
        matched_signal_ids=tuple(matched_ids),
    )


def _ensure_verified_wallet(
    *,
    registry: WalletRegistry,
    source: ExternalSourceSpec,
    address: str,
    coins: tuple[str, ...],
    report_fingerprint: str,
) -> None:
    registry.init()
    if any(
        wallet.source_type == "hyperliquid_wallet"
        and wallet.source_ref.lower() == address.lower()
        for wallet in registry.load()
    ):
        return
    suffix = address.lower().removeprefix("0x")[:10]
    registry.add(
        WalletSpec(
            id=f"resolved-{source.id}-{suffix}",
            label=f"{source.label} (reverse-index verified)",
            source_type="hyperliquid_wallet",
            source_ref=address.lower(),
            stage="research",
            coins=coins,
            notes=(
                f"Reverse-index discovery + official Hyperliquid episode verification; "
                f"report_fingerprint={report_fingerprint[:16]}; research only"
            ),
        )
    )


async def resolve_source_reverse_index(
    *,
    source: ExternalSourceSpec,
    index_client: HypeDexerCompletedTradesClient,
    official_client: HyperliquidHttpClient,
    wallet_registry: WalletRegistry,
    output_dir: Path,
    config: ReverseResolverConfig = DEFAULT_REVERSE_CONFIG,
    progress: Callable[[str], None] | None = None,
) -> ReverseResolverRun:
    all_signals = _source_signals(source)
    if len(all_signals) < config.min_discovery_matches + config.min_official_matches:
        raise ValueError("insufficient external trades for discovery plus held-out verification")
    recent_cutoff = max(signal.closed_at_ms for signal in all_signals) - int(
        timedelta(days=30).total_seconds() * 1000
    )
    recent = tuple(signal for signal in all_signals if signal.closed_at_ms >= recent_cutoff)
    if len(recent) < config.anchor_trades:
        recent = all_signals
    anchors = select_anchor_trades(recent, max_trades=config.anchor_trades)
    anchor_ids = {signal.signal_id for signal in anchors}
    held_out = tuple(signal for signal in all_signals if signal.signal_id not in anchor_ids)

    matches_by_anchor: dict[str, dict[str, AnchorMatch]] = {}
    for index, signal in enumerate(anchors, start=1):
        if progress:
            progress(
                f"reverse-index anchor {index}/{len(anchors)} {signal.coin} {signal.direction} "
                f"trade={signal.signal_id}"
            )
        window = config.primary_window_ms
        trades = await index_client.completed_trades(
            coin=signal.coin,
            direction=signal.direction,
            start_ms=signal.closed_at_ms - window,
            end_ms=signal.closed_at_ms + window,
            max_rows=config.max_index_rows_per_anchor,
            page_size=config.index_page_size,
        )
        matches = _best_matches_for_anchor(
            signal,
            trades,
            window_ms=window,
            max_price_bps=config.max_index_price_bps,
        )
        matches_by_anchor[signal.signal_id] = matches

    ranked = rank_candidates(matches_by_anchor, total_anchors=len(anchors))
    best = ranked[0] if ranked else None
    if best is None or best.matched_anchors < config.min_discovery_matches:
        if progress:
            progress("primary reverse-index pass insufficient; widening only discovery window")
        for index, signal in enumerate(anchors, start=1):
            window = config.fallback_window_ms
            trades = await index_client.completed_trades(
                coin=signal.coin,
                direction=signal.direction,
                start_ms=signal.closed_at_ms - window,
                end_ms=signal.closed_at_ms + window,
                max_rows=config.max_index_rows_per_anchor,
                page_size=config.index_page_size,
            )
            matches_by_anchor[signal.signal_id] = _best_matches_for_anchor(
                signal,
                trades,
                window_ms=window,
                max_price_bps=config.max_index_price_bps,
            )
            if progress:
                progress(f"fallback anchor {index}/{len(anchors)} candidates={len(matches_by_anchor[signal.signal_id])}")
        ranked = rank_candidates(matches_by_anchor, total_anchors=len(anchors))
        best = ranked[0] if ranked else None

    runner = ranked[1] if len(ranked) > 1 else None
    discovery_unique = bool(
        best is not None
        and best.matched_anchors >= config.min_discovery_matches
        and (
            runner is None
            or best.matched_anchors > runner.matched_anchors
            or best.score - runner.score >= D("8")
        )
    )

    official = OfficialVerification(0, 0, D("0"), ())
    status = "UNRESOLVED"
    verified_address: str | None = None
    if discovery_unique and best is not None:
        if progress:
            progress(
                f"reverse-index candidate {best.address} matched={best.matched_anchors}/{len(anchors)}; "
                "starting held-out official Hyperliquid verification"
            )
        official = await verify_candidate_officially(
            address=best.address,
            signals=held_out,
            clock_offset_ms=best.median_clock_offset_ms,
            client=official_client,
            config=config,
        )
        if (
            official.matched >= config.min_official_matches
            and official.ratio >= config.min_official_ratio
        ):
            status = "VERIFIED"
            verified_address = best.address
        else:
            status = "CANDIDATE_ONLY"
    elif best is not None and best.matched_anchors >= config.min_discovery_matches:
        status = "AMBIGUOUS"

    payload: dict[str, object] = {
        "version": 2,
        "resolver_rule_version": "reverse-index-completed-trades-v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source.to_dict(),
        "config": {
            **asdict(config),
            "max_index_price_bps": str(config.max_index_price_bps),
            "official_price_tolerance_bps": str(config.official_price_tolerance_bps),
            "min_official_ratio": str(config.min_official_ratio),
        },
        "evidence": {
            "all_signals": len(all_signals),
            "anchors": [signal.signal_id for signal in anchors],
            "held_out_signals": len(held_out),
        },
        "discovery": {
            "provider": "HypeDexer completed-trades reverse index",
            "status": "UNIQUE" if discovery_unique else "NOT_UNIQUE",
            "ranked_candidates": [candidate.to_dict() for candidate in ranked[:25]],
        },
        "official_verification": official.to_dict(),
        "decision": {
            "status": status,
            "verified_address": verified_address,
        },
        "safety": {
            "auto_validation_promotion": False,
            "auto_trading_promotion": False,
            "private_source_scraping": False,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    report_fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    payload["report_fingerprint"] = report_fingerprint
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = output_dir / f"reverse_resolution_{source.id}_{stamp}.json"
    report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if status == "VERIFIED" and verified_address is not None:
        _ensure_verified_wallet(
            registry=wallet_registry,
            source=source,
            address=verified_address,
            coins=tuple(sorted({canonical_coin(signal.coin) for signal in all_signals})),
            report_fingerprint=report_fingerprint,
        )

    return ReverseResolverRun(
        source_id=source.id,
        status=status,
        address=verified_address or (best.address if best is not None else None),
        discovery_matches=best.matched_anchors if best is not None else 0,
        official_matches=official.matched,
        report_path=str(report),
    )
