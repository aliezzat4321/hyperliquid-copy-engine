from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from hlcopy.market.symbols import canonical_coin, wire_coin

D = Decimal
BPS = D("10000")
DEFAULT_SQD_URL = "https://portal.sqd.dev/datasets/hyperliquid-fills"


class SqdPortalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SqdFill:
    block_number: int
    user: str
    coin: str
    px: Decimal
    sz: Decimal
    side: str
    direction: str
    time_ms: int
    oid: str
    closed_pnl: Decimal
    tid: str

    @classmethod
    def from_raw(cls, block_number: int, row: dict[str, Any]) -> SqdFill | None:
        try:
            user = str(row.get("user") or "").lower()
            if not user.startswith("0x") or len(user) != 42:
                return None
            px = D(str(row.get("px")))
            sz = D(str(row.get("sz")))
            time_ms = int(row.get("time"))
            if px <= 0 or sz <= 0 or time_ms <= 0:
                return None
            return cls(
                block_number=block_number,
                user=user,
                coin=canonical_coin(str(row.get("coin") or "")),
                px=px,
                sz=sz,
                side=str(row.get("side") or ""),
                direction=str(row.get("dir") or ""),
                time_ms=time_ms,
                oid=str(row.get("oid") or ""),
                closed_pnl=D(str(row.get("closedPnl", row.get("closed_pnl", 0)) or 0)),
                tid=str(row.get("tid") or ""),
            )
        except (ArithmeticError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class AggregatedClose:
    user: str
    coin: str
    direction: str
    first_time_ms: int
    last_time_ms: int
    avg_price: Decimal
    size: Decimal
    closed_pnl: Decimal
    group_id: str
    fill_count: int


@dataclass(frozen=True, slots=True)
class EpisodeEvidence:
    signal_id: str
    matched: bool
    close_time_error_ms: int | None
    close_price_bps: Decimal | None
    close_size_ratio_error: Decimal | None
    entry_time_error_ms: int | None
    entry_price_bps: Decimal | None
    entry_size_ratio_error: Decimal | None
    reconstructed_entry: Decimal | None
    reconstructed_size: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "matched": self.matched,
            "close_time_error_ms": self.close_time_error_ms,
            "close_price_bps": (
                str(self.close_price_bps) if self.close_price_bps is not None else None
            ),
            "close_size_ratio_error": (
                str(self.close_size_ratio_error)
                if self.close_size_ratio_error is not None
                else None
            ),
            "entry_time_error_ms": self.entry_time_error_ms,
            "entry_price_bps": (
                str(self.entry_price_bps) if self.entry_price_bps is not None else None
            ),
            "entry_size_ratio_error": (
                str(self.entry_size_ratio_error)
                if self.entry_size_ratio_error is not None
                else None
            ),
            "reconstructed_entry": (
                str(self.reconstructed_entry) if self.reconstructed_entry is not None else None
            ),
            "reconstructed_size": (
                str(self.reconstructed_size) if self.reconstructed_size is not None else None
            ),
        }


def _price_bps(left: Decimal, right: Decimal) -> Decimal:
    if left <= 0 or right <= 0:
        return D("Infinity")
    return abs(right / left - D("1")) * BPS


def signal_position_size(signal: Any) -> Decimal | None:
    raw = getattr(signal, "raw", {})
    if not isinstance(raw, dict):
        return None
    lowered = {str(key).strip().lower(): value for key, value in raw.items()}
    for key in ("position_size", "size", "quantity", "qty", "contracts"):
        value = lowered.get(key)
        if value in (None, ""):
            continue
        try:
            size = D(str(value))
        except (ArithmeticError, ValueError):
            continue
        if size > 0:
            return size
    return None


def aggregate_close_fills(
    fills: list[SqdFill],
    *,
    direction: str,
    cluster_gap_ms: int = 2_500,
) -> tuple[AggregatedClose, ...]:
    close_name = "Close Long" if direction == "LONG" else "Close Short"
    selected = [fill for fill in fills if fill.direction.lower() == close_name.lower()]
    if not selected:
        return ()

    groups: dict[tuple[str, str], list[SqdFill]] = {}
    for fill in selected:
        if fill.oid:
            groups.setdefault((fill.user, f"oid:{fill.oid}"), []).append(fill)

    by_user: dict[str, list[SqdFill]] = {}
    for fill in selected:
        by_user.setdefault(fill.user, []).append(fill)
    for user, rows in by_user.items():
        rows.sort(key=lambda item: (item.time_ms, item.tid))
        cluster: list[SqdFill] = []
        cluster_index = 0
        for fill in rows:
            if cluster and fill.time_ms - cluster[-1].time_ms > cluster_gap_ms:
                groups[(user, f"cluster:{cluster_index}")] = cluster
                cluster = []
                cluster_index += 1
            cluster.append(fill)
        if cluster:
            groups[(user, f"cluster:{cluster_index}")] = cluster

    output: list[AggregatedClose] = []
    seen: set[tuple[str, int, int, Decimal, Decimal]] = set()
    for (user, group_id), rows in groups.items():
        size = sum((fill.sz for fill in rows), D("0"))
        if size <= 0:
            continue
        notional = sum((fill.sz * fill.px for fill in rows), D("0"))
        avg_price = notional / size
        first_time = min(fill.time_ms for fill in rows)
        last_time = max(fill.time_ms for fill in rows)
        key = (user, first_time, last_time, size, avg_price)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            AggregatedClose(
                user=user,
                coin=rows[0].coin,
                direction=direction,
                first_time_ms=first_time,
                last_time_ms=last_time,
                avg_price=avg_price,
                size=size,
                closed_pnl=sum((fill.closed_pnl for fill in rows), D("0")),
                group_id=group_id,
                fill_count=len(rows),
            )
        )
    return tuple(output)


def _empty_episode(signal_id: str) -> EpisodeEvidence:
    return EpisodeEvidence(
        signal_id=signal_id,
        matched=False,
        close_time_error_ms=None,
        close_price_bps=None,
        close_size_ratio_error=None,
        entry_time_error_ms=None,
        entry_price_bps=None,
        entry_size_ratio_error=None,
        reconstructed_entry=None,
        reconstructed_size=None,
    )


def match_episode(
    signal: Any,
    fills: list[SqdFill],
    *,
    close_time_tolerance_ms: int,
    close_price_tolerance_bps: Decimal,
    max_size_ratio_error: Decimal,
    entry_time_tolerance_ms: int,
    entry_price_tolerance_bps: Decimal,
) -> EpisodeEvidence:
    source_size = signal_position_size(signal)
    closes = aggregate_close_fills(fills, direction=signal.direction)
    close_options: list[tuple[Decimal, AggregatedClose, Decimal | None]] = []
    for close in closes:
        time_error = min(
            abs(close.first_time_ms - signal.closed_at_ms),
            abs(close.last_time_ms - signal.closed_at_ms),
        )
        price_bps = _price_bps(signal.exit_price, close.avg_price)
        if time_error > close_time_tolerance_ms or price_bps > close_price_tolerance_bps:
            continue
        size_error = None
        if source_size is not None:
            size_error = abs(close.size / source_size - D("1"))
            if size_error > max_size_ratio_error:
                continue
        cost = D(time_error) / D("1000") + price_bps
        if size_error is not None:
            cost += size_error * D("10")
        close_options.append((cost, close, size_error))

    if not close_options:
        return _empty_episode(signal.signal_id)

    _, close, close_size_error = min(close_options, key=lambda item: item[0])
    close_time_error = min(
        abs(close.first_time_ms - signal.closed_at_ms),
        abs(close.last_time_ms - signal.closed_at_ms),
    )
    close_price_bps = _price_bps(signal.exit_price, close.avg_price)
    target_size = source_size if source_size is not None else close.size

    if signal.direction == "LONG":
        open_name = "open long"
        add_name = "long > long"
        reduce_name = "close long"
    else:
        open_name = "open short"
        add_name = "short > short"
        reduce_name = "close short"

    seed_options = [
        fill
        for fill in fills
        if fill.time_ms < close.first_time_ms
        and fill.direction.lower() == open_name
        and abs(fill.time_ms - signal.opened_at_ms) <= entry_time_tolerance_ms
    ]
    if not seed_options:
        return EpisodeEvidence(
            signal_id=signal.signal_id,
            matched=False,
            close_time_error_ms=close_time_error,
            close_price_bps=close_price_bps,
            close_size_ratio_error=close_size_error,
            entry_time_error_ms=None,
            entry_price_bps=None,
            entry_size_ratio_error=None,
            reconstructed_entry=None,
            reconstructed_size=None,
        )

    seed = min(
        seed_options,
        key=lambda fill: (abs(fill.time_ms - signal.opened_at_ms), fill.time_ms, fill.tid),
    )
    entry_time_error = abs(seed.time_ms - signal.opened_at_ms)

    total = D("0")
    notional = D("0")
    episode_ended_early = False
    rows = sorted(
        (
            fill
            for fill in fills
            if seed.time_ms <= fill.time_ms < close.first_time_ms
        ),
        key=lambda fill: (fill.time_ms, fill.tid),
    )
    for fill in rows:
        text = fill.direction.lower()
        if fill is seed or text == add_name:
            total += fill.sz
            notional += fill.sz * fill.px
            continue
        if text == open_name:
            episode_ended_early = True
            break
        if text == reduce_name and total > 0:
            average_entry = notional / total
            total = max(D("0"), total - fill.sz)
            notional = average_entry * total
            if total == 0:
                episode_ended_early = True
                break

    if total <= 0 or episode_ended_early:
        return EpisodeEvidence(
            signal_id=signal.signal_id,
            matched=False,
            close_time_error_ms=close_time_error,
            close_price_bps=close_price_bps,
            close_size_ratio_error=close_size_error,
            entry_time_error_ms=entry_time_error,
            entry_price_bps=None,
            entry_size_ratio_error=None,
            reconstructed_entry=None,
            reconstructed_size=total if total > 0 else None,
        )

    reconstructed_entry = notional / total
    entry_bps = _price_bps(signal.entry_price, reconstructed_entry)
    entry_size_error = abs(total / target_size - D("1"))
    matched = (
        entry_time_error <= entry_time_tolerance_ms
        and entry_bps <= entry_price_tolerance_bps
        and entry_size_error <= max_size_ratio_error
    )
    return EpisodeEvidence(
        signal_id=signal.signal_id,
        matched=matched,
        close_time_error_ms=close_time_error,
        close_price_bps=close_price_bps,
        close_size_ratio_error=close_size_error,
        entry_time_error_ms=entry_time_error,
        entry_price_bps=entry_bps,
        entry_size_ratio_error=entry_size_error,
        reconstructed_entry=reconstructed_entry,
        reconstructed_size=total,
    )


class SqdHyperliquidFillsClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_SQD_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Accept": "application/x-ndjson",
                "User-Agent": "hyperliquid-copy-engine/0.1",
            },
        )
        self._bounds: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._headers: dict[int, int] = {}

    async def __aenter__(self) -> SqdHyperliquidFillsClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        retries: int = 6,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(retries + 1):
            try:
                response = await self._client.request(method, url, json=payload)
                if response.status_code in {429, 521, 522, 523, 529} or response.status_code >= 500:
                    if attempt == retries:
                        response.raise_for_status()
                    await asyncio.sleep(min(8.0, 0.4 * (2**attempt)))
                    continue
                if response.status_code == 204:
                    return response
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == retries:
                    raise SqdPortalError(f"SQD request failed after retries: {url}") from exc
                await asyncio.sleep(min(8.0, 0.4 * (2**attempt)))
        raise AssertionError("unreachable")

    async def _stream(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self._request("POST", "finalized-stream", payload=payload)
        if response.status_code == 204 or not response.text.strip():
            return []
        rows: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SqdPortalError("SQD returned invalid NDJSON") from exc
            if isinstance(item, dict):
                rows.append(item)
        return rows

    async def _header_timestamp(self, block_number: int) -> int:
        cached = self._headers.get(block_number)
        if cached is not None:
            return cached
        payload = {
            "type": "hyperliquidFills",
            "fromBlock": block_number,
            "toBlock": block_number,
            "includeAllBlocks": True,
            "fields": {"block": {"number": True, "timestamp": True}},
            "fills": [],
        }
        rows = await self._stream(payload)
        if not rows:
            raise SqdPortalError(f"SQD returned no header for block {block_number}")
        header = rows[0].get("header")
        if not isinstance(header, dict):
            raise SqdPortalError(f"SQD header missing for block {block_number}")
        timestamp = int(header["timestamp"])
        self._headers[block_number] = timestamp
        return timestamp

    async def _coverage_bounds(self) -> tuple[tuple[int, int], tuple[int, int]]:
        if self._bounds is not None:
            return self._bounds
        metadata_response = await self._request("GET", "metadata")
        head_response = await self._request("GET", "finalized-head")
        metadata = metadata_response.json()
        head = head_response.json()
        start_block = int(metadata.get("start_block") or metadata.get("first_block") or 0)
        head_block = int(head["number"])
        if head_block <= start_block:
            raise SqdPortalError("SQD Hyperliquid fills dataset has invalid coverage bounds")
        start_ts = await self._header_timestamp(start_block)
        head_ts = await self._header_timestamp(head_block)
        self._bounds = ((start_block, start_ts), (head_block, head_ts))
        return self._bounds

    async def coverage_start_ms(self) -> int:
        bounds = await self._coverage_bounds()
        return bounds[0][1]

    async def coverage_end_ms(self) -> int:
        bounds = await self._coverage_bounds()
        return bounds[1][1]

    async def locate_block(self, timestamp_ms: int) -> int:
        (low_n, low_t), (high_n, high_t) = await self._coverage_bounds()
        if timestamp_ms < low_t or timestamp_ms > high_t:
            raise SqdPortalError(
                f"timestamp {timestamp_ms} outside SQD coverage {low_t}..{high_t}"
            )
        for _ in range(10):
            if high_n - low_n <= 2:
                candidates = [(low_n, low_t), (high_n, high_t)]
                return min(candidates, key=lambda item: abs(item[1] - timestamp_ms))[0]
            span_t = max(1, high_t - low_t)
            fraction = (timestamp_ms - low_t) / span_t
            guess = low_n + int((high_n - low_n) * fraction)
            guess = max(low_n + 1, min(high_n - 1, guess))
            guess_t = await self._header_timestamp(guess)
            if abs(guess_t - timestamp_ms) <= 250:
                return guess
            if guess_t < timestamp_ms:
                low_n, low_t = guess, guess_t
            else:
                high_n, high_t = guess, guess_t
        candidates = [(low_n, low_t), (high_n, high_t)]
        return min(candidates, key=lambda item: abs(item[1] - timestamp_ms))[0]

    async def _stream_range(
        self,
        *,
        from_block: int,
        to_block: int,
        coin: str,
        user: str | None = None,
    ) -> list[SqdFill]:
        current = from_block
        output: list[SqdFill] = []
        for _ in range(100):
            fill_filter: dict[str, object] = {"coin": [wire_coin(coin)]}
            if user is not None:
                fill_filter["user"] = [user.lower()]
            payload = {
                "type": "hyperliquidFills",
                "fromBlock": current,
                "toBlock": to_block,
                "fields": {
                    "block": {"number": True, "timestamp": True},
                    "fill": {
                        "user": True,
                        "coin": True,
                        "px": True,
                        "sz": True,
                        "side": True,
                        "dir": True,
                        "time": True,
                        "oid": True,
                        "closedPnl": True,
                        "crossed": True,
                        "tid": True,
                    },
                },
                "fills": [fill_filter],
            }
            rows = await self._stream(payload)
            if not rows:
                break
            last_block = current - 1
            for row in rows:
                header = row.get("header")
                if not isinstance(header, dict):
                    continue
                block_number = int(header["number"])
                last_block = max(last_block, block_number)
                for raw in row.get("fills", []):
                    if not isinstance(raw, dict):
                        continue
                    fill = SqdFill.from_raw(block_number, raw)
                    if fill is not None:
                        output.append(fill)
            if last_block >= to_block:
                break
            if last_block < current:
                raise SqdPortalError("SQD stream did not advance")
            current = last_block + 1
        else:
            raise SqdPortalError("SQD stream exceeded continuation limit")
        return output

    async def fills_around(
        self,
        *,
        timestamp_ms: int,
        coin: str,
        window_ms: int,
        user: str | None = None,
    ) -> list[SqdFill]:
        (low_n, low_t), (high_n, high_t) = await self._coverage_bounds()
        if timestamp_ms < low_t or timestamp_ms > high_t:
            return []
        center = await self.locate_block(timestamp_ms)
        ms_per_block = (high_t - low_t) / max(1, high_n - low_n)
        ms_per_block = max(50.0, min(1_000.0, ms_per_block))
        block_padding = max(20, math.ceil(window_ms / ms_per_block) + 40)
        rows = await self._stream_range(
            from_block=max(low_n, center - block_padding),
            to_block=min(high_n, center + block_padding),
            coin=coin,
            user=user,
        )
        canonical = canonical_coin(coin)
        return [
            fill
            for fill in rows
            if fill.coin == canonical and abs(fill.time_ms - timestamp_ms) <= window_ms
        ]

    async def fills_between_times(
        self,
        *,
        start_ms: int,
        end_ms: int,
        coin: str,
        user: str,
    ) -> list[SqdFill]:
        if end_ms < start_ms:
            raise ValueError("end_ms cannot precede start_ms")
        (coverage_low, coverage_low_ts), (coverage_high, coverage_high_ts) = (
            await self._coverage_bounds()
        )
        bounded_start = max(start_ms, coverage_low_ts)
        bounded_end = min(end_ms, coverage_high_ts)
        if bounded_end < bounded_start:
            return []
        low_block = await self.locate_block(bounded_start)
        high_block = await self.locate_block(bounded_end)
        padding = 50
        rows = await self._stream_range(
            from_block=max(coverage_low, low_block - padding),
            to_block=min(coverage_high, high_block + padding),
            coin=coin,
            user=user,
        )
        canonical = canonical_coin(coin)
        return [
            fill
            for fill in rows
            if fill.user == user.lower()
            and fill.coin == canonical
            and bounded_start <= fill.time_ms <= bounded_end
        ]
