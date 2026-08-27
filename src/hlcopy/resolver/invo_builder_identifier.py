from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

import httpx
import lz4.frame

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.market.symbols import canonical_coin
from hlcopy.resolver.identifier import (
    WalletIdentificationResult,
    _bind_source_identity,
    _load_source_evidence,
    _write_json_atomic,
)
from hlcopy.resolver.matcher import select_anchor_trades
from hlcopy.resolver.provenance import EvidenceSnapshot, jsonable_config
from hlcopy.resolver.public_trade_index import DEFAULT_PUBLIC_TRADE_CONFIG, PublicTradeDiscoveryConfig
from hlcopy.resolver.reverse_index import AnchorMatch, CandidateFingerprint, rank_candidates
from hlcopy.signals.invo import CopySignal

D = Decimal
BPS = D("10000")
POSITION_EPSILON = D("0.000000000001")

INVO_BUILDER_ADDRESS = "0x557edb253b1d7ed5f15b248a5a3fd919fa5d3c81"
BUILDER_STATS_BASE = "https://stats-data.hyperliquid.xyz/Mainnet/builder_fills"
HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz"

BUILDER_MAX_DISCOVERY_TRADES = 24
BUILDER_MIN_DISCOVERY_TRADES = 8
BUILDER_BATCH_SIZE = 8
BUILDER_MIN_DISCOVERY_MATCHES = 4
BUILDER_MAX_SHORTLIST = 3
BUILDER_WINDOW_MS = 30_000
BUILDER_MAX_PRICE_BPS = D("15")
BUILDER_MAX_CLOCK_MAD_MS = 8_000.0
BUILDER_MAX_MEDIAN_PRICE_BPS = D("15")
BUILDER_PUBLICATION_LAG_DAYS = 2

BUILDER_VERIFY_TRADES = 12
BUILDER_MIN_VERIFY_MATCHES = 5
BUILDER_MIN_VERIFY_RATIO = D("0.40")
BUILDER_MIN_WINNER_GAP = 2
BUILDER_VERIFY_WINDOW_MS = 45_000
BUILDER_VERIFY_PRICE_BPS = D("35")
BUILDER_CLOSE_OFFSET_DRIFT_MS = 15_000

_DAY_LOCKS: dict[str, asyncio.Lock] = {}
_DAY_ROWS: OrderedDict[str, tuple["BuilderFill", ...]] = OrderedDict()
_DAY_ROWS_LIMIT = 3


@dataclass(frozen=True, slots=True)
class BuilderFill:
    time_ms: int
    user: str
    coin: str
    side: str
    px: Decimal
    sz: Decimal
    closed_pnl: Decimal
    counterparty: str
    builder_fee: Decimal
    execution_id: str


@dataclass(frozen=True, slots=True)
class BuilderCandidateVerification:
    address: str
    attempted: int
    matched: int
    ratio: Decimal
    clock_offset_mad_ms: float | None
    median_price_bps: Decimal | None
    matched_signal_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "attempted": self.attempted,
            "matched": self.matched,
            "ratio": str(self.ratio),
            "clock_offset_mad_ms": self.clock_offset_mad_ms,
            "median_price_bps": (
                str(self.median_price_bps) if self.median_price_bps is not None else None
            ),
            "matched_signal_ids": list(self.matched_signal_ids),
        }


def _timestamp_ms(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError("builder fill timestamp is blank")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def _median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / D("2")


def _parse_builder_fill(row: Mapping[str, str]) -> BuilderFill | None:
    try:
        user = str(row.get("user") or "").strip().lower()
        if not user.startswith("0x") or len(user) != 42:
            return None
        coin = canonical_coin(str(row.get("coin") or ""))
        if not coin:
            return None
        side = str(row.get("side") or "").strip().lower()
        if side not in {"ask", "bid"}:
            return None
        px = D(str(row.get("px")))
        sz = D(str(row.get("sz")))
        if px <= 0 or sz <= 0:
            return None
        time_ms = _timestamp_ms(row.get("time"))
        closed_pnl = D(str(row.get("closed_pnl") or "0"))
        builder_fee = D(str(row.get("builder_fee") or "0"))
        counterparty = str(row.get("counterparty") or "").strip().lower()
        raw_key = "|".join(
            (
                str(row.get("time") or ""),
                user,
                coin,
                side,
                str(px),
                str(sz),
                counterparty,
                str(closed_pnl),
                str(builder_fee),
            )
        )
        execution_id = "builder:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:24]
        return BuilderFill(
            time_ms=time_ms,
            user=user,
            coin=coin,
            side=side,
            px=px,
            sz=sz,
            closed_pnl=closed_pnl,
            counterparty=counterparty,
            builder_fee=builder_fee,
            execution_id=execution_id,
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def parse_builder_csv(data: bytes) -> tuple[BuilderFill, ...]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[BuilderFill] = []
    for raw in reader:
        fill = _parse_builder_fill(raw)
        if fill is not None:
            rows.append(fill)
    rows.sort(key=lambda item: (item.time_ms, item.user, item.execution_id))
    return tuple(rows)


class InvoBuilderFillsClient:
    def __init__(
        self,
        *,
        cache_dir: Path,
        builder_address: str = INVO_BUILDER_ADDRESS,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.builder_address = builder_address.lower()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"User-Agent": "hyperliquid-copy-engine/0.1"},
        )

    async def __aenter__(self) -> InvoBuilderFillsClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    def _cache_path(self, day_key: str) -> Path:
        return self.cache_dir / self.builder_address / f"{day_key}.csv.lz4"

    async def _download_day(self, day_key: str) -> bytes | None:
        path = self._cache_path(day_key)
        if path.is_file() and path.stat().st_size > 100:
            return path.read_bytes()
        url = f"{BUILDER_STATS_BASE}/{self.builder_address}/{day_key}.csv.lz4"
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = await self._client.get(url)
            except httpx.RequestError:
                if attempt == 2:
                    return None
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if response.status_code == 200 and len(response.content) > 100:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_bytes(response.content)
                temporary.replace(path)
                return response.content
            if response.status_code in {403, 404}:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
            return None
        return None

    async def day(self, day_key: str) -> tuple[BuilderFill, ...] | None:
        cached = _DAY_ROWS.get(day_key)
        if cached is not None:
            _DAY_ROWS.move_to_end(day_key)
            return cached
        lock = _DAY_LOCKS.setdefault(day_key, asyncio.Lock())
        async with lock:
            cached = _DAY_ROWS.get(day_key)
            if cached is not None:
                _DAY_ROWS.move_to_end(day_key)
                return cached
            compressed = await self._download_day(day_key)
            if compressed is None:
                return None
            try:
                raw = lz4.frame.decompress(compressed)
            except (RuntimeError, ValueError):
                return None
            rows = parse_builder_csv(raw)
            _DAY_ROWS[day_key] = rows
            _DAY_ROWS.move_to_end(day_key)
            while len(_DAY_ROWS) > _DAY_ROWS_LIMIT:
                _DAY_ROWS.popitem(last=False)
            return rows


def _day_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).strftime("%Y%m%d")


def _published_cutoff_day(now: datetime | None = None) -> date:
    current = now or datetime.now(tz=UTC)
    return (current - timedelta(days=BUILDER_PUBLICATION_LAG_DAYS)).date()


def _builder_eligible(signal: CopySignal, *, cutoff: date) -> bool:
    closed = datetime.fromtimestamp(signal.closed_at_ms / 1000, UTC).date()
    return closed <= cutoff


def _expected_close_side(direction: str) -> str:
    return "ask" if direction == "LONG" else "bid"


def _match_builder_anchor(
    signal: CopySignal,
    fill: BuilderFill,
    *,
    window_ms: int = BUILDER_WINDOW_MS,
    max_price_bps: Decimal = BUILDER_MAX_PRICE_BPS,
) -> AnchorMatch | None:
    if canonical_coin(signal.coin) != fill.coin:
        return None
    if fill.side != _expected_close_side(signal.direction):
        return None
    close_offset = fill.time_ms - signal.closed_at_ms
    if abs(close_offset) > window_ms:
        return None
    exit_bps = _price_bps(signal.exit_price, fill.px)
    if exit_bps > max_price_bps:
        return None
    time_penalty = D(abs(close_offset)) / D("1000")
    quality = max(D("0"), D("100") - time_penalty - exit_bps * D("2"))
    return AnchorMatch(
        signal_id=signal.signal_id,
        user=fill.user,
        trade_id=fill.execution_id,
        open_offset_ms=close_offset,
        close_offset_ms=close_offset,
        offset_gap_ms=0,
        entry_price_bps=exit_bps,
        exit_price_bps=exit_bps,
        quality=quality,
    )


def _best_builder_matches(
    signal: CopySignal,
    rows: Sequence[BuilderFill],
) -> dict[str, AnchorMatch]:
    best: dict[str, AnchorMatch] = {}
    for fill in rows:
        match = _match_builder_anchor(signal, fill)
        if match is None:
            continue
        current = best.get(fill.user)
        if current is None or match.quality > current.quality:
            best[fill.user] = match
    return best


def _batch_presence(candidate: CandidateFingerprint, batches: Sequence[set[str]]) -> int:
    ids = {match.signal_id for match in candidate.matches}
    return sum(1 for batch in batches if ids & batch)


def _shortlist_candidates(
    ranked: Sequence[CandidateFingerprint],
    *,
    batches: Sequence[set[str]],
) -> tuple[CandidateFingerprint, ...]:
    required_batches = 2 if len(batches) >= 2 else 1
    output = [
        candidate
        for candidate in ranked
        if candidate.matched_anchors >= BUILDER_MIN_DISCOVERY_MATCHES
        and candidate.clock_offset_mad_ms <= BUILDER_MAX_CLOCK_MAD_MS
        and candidate.median_price_bps <= BUILDER_MAX_MEDIAN_PRICE_BPS
        and _batch_presence(candidate, batches) >= required_batches
    ]
    return tuple(output[:BUILDER_MAX_SHORTLIST])


async def _discover_builder_candidates(
    signals: tuple[CopySignal, ...],
    *,
    client: InvoBuilderFillsClient,
) -> tuple[
    tuple[CandidateFingerprint, ...],
    tuple[CopySignal, ...],
    tuple[set[str], ...],
    dict[str, str],
]:
    cutoff = _published_cutoff_day()
    eligible = tuple(signal for signal in signals if _builder_eligible(signal, cutoff=cutoff))
    if len(eligible) < BUILDER_MIN_DISCOVERY_TRADES + BUILDER_VERIFY_TRADES:
        return (), (), (), {}
    discovery_count = min(
        BUILDER_MAX_DISCOVERY_TRADES,
        max(BUILDER_MIN_DISCOVERY_TRADES, len(eligible) - BUILDER_VERIFY_TRADES),
    )
    anchors = select_anchor_trades(eligible, max_trades=discovery_count)
    batches = tuple(
        {signal.signal_id for signal in anchors[index : index + BUILDER_BATCH_SIZE]}
        for index in range(0, len(anchors), BUILDER_BATCH_SIZE)
    )
    day_cache: dict[str, tuple[BuilderFill, ...] | None] = {}
    errors: dict[str, str] = {}
    matches_by_anchor: dict[str, dict[str, AnchorMatch]] = {}
    for signal in anchors:
        key = _day_key(signal.closed_at_ms)
        if key not in day_cache:
            rows = await client.day(key)
            day_cache[key] = rows
            if rows is None:
                errors[key] = "builder_day_unavailable"
        rows = day_cache[key]
        matches_by_anchor[signal.signal_id] = (
            _best_builder_matches(signal, rows) if rows is not None else {}
        )
    ranked = rank_candidates(matches_by_anchor, total_anchors=len(anchors))
    return ranked, anchors, batches, errors


def _official_fill_is_final_flatten(signal: CopySignal, row: Mapping[str, Any]) -> bool:
    try:
        if canonical_coin(str(row.get("coin") or "")) != canonical_coin(signal.coin):
            return False
        expected_dir = "close long" if signal.direction == "LONG" else "close short"
        if str(row.get("dir") or "").strip().lower() != expected_dir:
            return False
        size = D(str(row.get("sz")))
        start = D(str(row.get("startPosition")))
        tolerance = max(POSITION_EPSILON, abs(start) * D("0.000000001"))
        return size > 0 and abs(abs(start) - size) <= tolerance
    except (ArithmeticError, TypeError, ValueError):
        return False


def _official_execution_id(row: Mapping[str, Any]) -> str:
    tid = str(row.get("tid") or "").strip()
    if tid:
        return f"tid:{tid}"
    return "fill:" + hashlib.sha256(
        "|".join(
            str(row.get(key) or "")
            for key in ("time", "coin", "px", "sz", "oid", "hash")
        ).encode("utf-8")
    ).hexdigest()[:24]


def _match_official_close(
    signal: CopySignal,
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_offset_ms: float,
) -> tuple[Mapping[str, Any], int, Decimal] | None:
    best: tuple[tuple[Decimal, int], Mapping[str, Any], int, Decimal] | None = None
    for row in rows:
        if not _official_fill_is_final_flatten(signal, row):
            continue
        try:
            time_ms = int(row.get("time"))
            px = D(str(row.get("px")))
        except (ArithmeticError, TypeError, ValueError):
            continue
        raw_offset = time_ms - signal.closed_at_ms
        drift = abs(raw_offset - expected_offset_ms)
        if abs(raw_offset) > BUILDER_VERIFY_WINDOW_MS or drift > BUILDER_CLOSE_OFFSET_DRIFT_MS:
            continue
        price_bps = _price_bps(signal.exit_price, px)
        if price_bps > BUILDER_VERIFY_PRICE_BPS:
            continue
        key = (price_bps, drift)
        if best is None or key < best[0]:
            best = (key, row, raw_offset, price_bps)
    if best is None:
        return None
    return best[1], best[2], best[3]


async def _candidate_official_rows(
    http: HyperliquidHttpClient,
    *,
    address: str,
    signals: Sequence[CopySignal],
) -> list[Mapping[str, Any]]:
    by_day: dict[str, list[CopySignal]] = {}
    for signal in signals:
        by_day.setdefault(_day_key(signal.closed_at_ms), []).append(signal)
    rows: list[Mapping[str, Any]] = []
    for day_signals in by_day.values():
        start = min(signal.closed_at_ms for signal in day_signals) - BUILDER_VERIFY_WINDOW_MS
        end = max(signal.closed_at_ms for signal in day_signals) + BUILDER_VERIFY_WINDOW_MS
        pages = await http.user_fills_by_time(address, start, end)
        for page in pages:
            payload = page.response_payload
            if isinstance(payload, list):
                rows.extend(item for item in payload if isinstance(item, Mapping))
    return rows


async def _verify_builder_candidate(
    candidate: CandidateFingerprint,
    *,
    heldout: tuple[CopySignal, ...],
    http: HyperliquidHttpClient,
) -> BuilderCandidateVerification:
    rows = await _candidate_official_rows(http, address=candidate.address, signals=heldout)
    matched_ids: list[str] = []
    offsets: list[float] = []
    prices: list[Decimal] = []
    used_execution_ids: set[str] = set()
    for signal in heldout:
        matched = _match_official_close(
            signal,
            rows,
            expected_offset_ms=candidate.median_clock_offset_ms,
        )
        if matched is None:
            continue
        row, offset, price_bps = matched
        execution_id = _official_execution_id(row)
        if execution_id in used_execution_ids:
            continue
        used_execution_ids.add(execution_id)
        matched_ids.append(signal.signal_id)
        offsets.append(float(offset))
        prices.append(price_bps)
    attempted = len(heldout)
    matched_count = len(matched_ids)
    offset_mad = None
    if offsets:
        center = median(offsets)
        offset_mad = median(abs(value - center) for value in offsets)
    median_price = _median_decimal(prices)
    return BuilderCandidateVerification(
        address=candidate.address,
        attempted=attempted,
        matched=matched_count,
        ratio=D(matched_count) / D(attempted) if attempted else D("0"),
        clock_offset_mad_ms=offset_mad,
        median_price_bps=median_price,
        matched_signal_ids=tuple(matched_ids),
    )


def _select_verified_winner(
    results: Sequence[BuilderCandidateVerification],
) -> BuilderCandidateVerification | None:
    if not results:
        return None
    ranked = sorted(
        results,
        key=lambda item: (
            item.matched,
            item.ratio,
            -(item.clock_offset_mad_ms or float("inf")),
            -(item.median_price_bps or D("Infinity")),
            item.address,
        ),
        reverse=True,
    )
    best = ranked[0]
    if best.matched < BUILDER_MIN_VERIFY_MATCHES or best.ratio < BUILDER_MIN_VERIFY_RATIO:
        return None
    if best.clock_offset_mad_ms is None or best.clock_offset_mad_ms > BUILDER_MAX_CLOCK_MAD_MS:
        return None
    if best.median_price_bps is None or best.median_price_bps > BUILDER_MAX_MEDIAN_PRICE_BPS:
        return None
    runner_matches = ranked[1].matched if len(ranked) > 1 else 0
    if best.matched - runner_matches < BUILDER_MIN_WINNER_GAP:
        return None
    return best


def _confidence(
    *,
    discovery_matches: int,
    discovery_anchors: int,
    historical_matches: int,
    historical_attempted: int,
) -> Decimal:
    if historical_attempted <= 0 or historical_matches <= 0:
        return D("0")
    discovery_ratio = D(discovery_matches) / D(max(1, discovery_anchors))
    historical_ratio = D(historical_matches) / D(max(1, historical_attempted))
    return min(D("0.999"), discovery_ratio * D("0.40") + historical_ratio * D("0.60"))


async def identify_invo_wallet_builder_first(
    evidence_path: Path,
    *,
    output_dir: Path,
    cache_dir: Path,
    snapshot: EvidenceSnapshot | None = None,
    expected_source_identity: str | None = None,
    config: PublicTradeDiscoveryConfig | None = None,
    builder_client: InvoBuilderFillsClient | None = None,
    hyperliquid_client: HyperliquidHttpClient | None = None,
) -> WalletIdentificationResult:
    effective_config = config or DEFAULT_PUBLIC_TRADE_CONFIG
    snapshot = snapshot or EvidenceSnapshot.from_path(evidence_path)
    imported = _load_source_evidence(snapshot, effective_config)
    if imported.rejected_rows:
        raise ValueError(
            f"external evidence contains {len(imported.rejected_rows)} malformed rows; fail closed"
        )
    source_identity = _bind_source_identity(
        imported,
        expected_source_identity=expected_source_identity,
    )
    signals = imported.signals

    owns_builder = builder_client is None
    if builder_client is None:
        builder_client = InvoBuilderFillsClient(cache_dir=cache_dir)
        await builder_client.__aenter__()
    try:
        ranked, anchors, batches, day_errors = await _discover_builder_candidates(
            signals,
            client=builder_client,
        )
    finally:
        if owns_builder:
            await builder_client.__aexit__(None, None, None)

    shortlist = _shortlist_candidates(ranked, batches=batches)
    anchor_ids = {signal.signal_id for signal in anchors}
    cutoff = _published_cutoff_day()
    heldout_pool = tuple(
        signal
        for signal in signals
        if signal.signal_id not in anchor_ids and _builder_eligible(signal, cutoff=cutoff)
    )
    heldout = select_anchor_trades(heldout_pool, max_trades=BUILDER_VERIFY_TRADES)

    verifications: list[BuilderCandidateVerification] = []
    if len(heldout) == BUILDER_VERIFY_TRADES and shortlist:
        owns_http = hyperliquid_client is None
        if hyperliquid_client is None:
            hyperliquid_client = HyperliquidHttpClient(
                HYPERLIQUID_API_URL,
                "https://stats-data.hyperliquid.xyz",
                concurrency=3,
            )
            await hyperliquid_client.__aenter__()
        try:
            for candidate in shortlist:
                verifications.append(
                    await _verify_builder_candidate(
                        candidate,
                        heldout=heldout,
                        http=hyperliquid_client,
                    )
                )
        finally:
            if owns_http:
                await hyperliquid_client.__aexit__(None, None, None)

    winner = _select_verified_winner(verifications)
    discovery_best = ranked[0] if ranked else None
    winning_discovery = next(
        (item for item in shortlist if winner is not None and item.address == winner.address),
        None,
    )
    evidence_candidate = winning_discovery or discovery_best
    wallet = winner.address if winner is not None else None
    status = "VERIFIED" if wallet else "UNRESOLVED"
    candidate = wallet or (discovery_best.address if discovery_best else None)
    discovery_matches = evidence_candidate.matched_anchors if evidence_candidate else 0
    historical_matches = winner.matched if winner is not None else 0
    historical_attempted = winner.attempted if winner is not None else 0
    confidence = _confidence(
        discovery_matches=discovery_matches,
        discovery_anchors=len(anchors),
        historical_matches=historical_matches,
        historical_attempted=historical_attempted,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    attempt_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = output_dir / (
        f"wallet_identification_{evidence_path.stem}_{snapshot.sha256[:16]}_"
        f"builder_{attempt_id}.json"
    )
    _write_json_atomic(
        report_path,
        {
            "version": 13,
            "resolver_rule_version": "invo-hyperliquid-builder-close-proof-v13",
            "mode": "INVO_BUILDER_CLOSE_SIGNATURE",
            "input_file": str(evidence_path),
            "input_sha256": snapshot.sha256,
            "input_bytes": snapshot.size,
            "source_identity": source_identity,
            "builder_address": INVO_BUILDER_ADDRESS,
            "effective_config": jsonable_config(effective_config),
            "accepted_trades": len(signals),
            "status": status,
            "wallet": wallet,
            "candidate": candidate,
            "confidence": str(confidence),
            "discovery_anchor_ids": [signal.signal_id for signal in anchors],
            "heldout_signal_ids": [signal.signal_id for signal in heldout],
            "builder_day_errors": day_errors,
            "ranked_candidates": [item.to_dict() for item in ranked[:25]],
            "shortlist": [item.to_dict() for item in shortlist],
            "candidate_verifications": [item.to_dict() for item in verifications],
            "thresholds": {
                "discovery_max_trades": BUILDER_MAX_DISCOVERY_TRADES,
                "discovery_min_matches": BUILDER_MIN_DISCOVERY_MATCHES,
                "discovery_batch_size": BUILDER_BATCH_SIZE,
                "builder_window_ms": BUILDER_WINDOW_MS,
                "builder_max_price_bps": str(BUILDER_MAX_PRICE_BPS),
                "verify_trades": BUILDER_VERIFY_TRADES,
                "verify_min_matches": BUILDER_MIN_VERIFY_MATCHES,
                "verify_min_ratio": str(BUILDER_MIN_VERIFY_RATIO),
                "verify_min_winner_gap": BUILDER_MIN_WINNER_GAP,
                "verify_window_ms": BUILDER_VERIFY_WINDOW_MS,
                "verify_price_bps": str(BUILDER_VERIFY_PRICE_BPS),
                "close_offset_drift_ms": BUILDER_CLOSE_OFFSET_DRIFT_MS,
            },
            "verification_source": (
                "Hyperliquid builder_fills discovery + official userFillsByTime final-flatten proof"
            ),
            "safety": {
                "auto_validation_promotion": False,
                "auto_trading_promotion": False,
                "unverified_candidate_exposed_as_wallet": False,
                "builder_discovery_cannot_verify": True,
                "held_out_signals_disjoint_from_discovery": True,
                "official_user_fills_verification_required": True,
                "final_flatten_start_position_required": True,
                "entry_timestamp_not_required": True,
                "close_price_required": True,
                "close_clock_offset_consistency_required": True,
                "one_vote_per_official_execution": True,
                "unique_held_out_winner_required": True,
                "cross_identity_collision_quarantine_required": True,
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
        discovery_anchors=len(anchors),
        candidate_unique=winner is not None,
        historical_matches=historical_matches,
        historical_attempted=historical_attempted,
        verification_source="hyperliquid_builder_plus_official_user_fills",
        median_clock_offset_ms=(
            evidence_candidate.median_clock_offset_ms if evidence_candidate else None
        ),
        median_price_bps=(
            evidence_candidate.median_price_bps if evidence_candidate else None
        ),
        report_path=str(report_path),
    )
