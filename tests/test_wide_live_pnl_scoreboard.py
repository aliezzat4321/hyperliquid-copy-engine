import json
from decimal import Decimal
from pathlib import Path

from hlcopy.copyability.slippage import BookLevel
from hlcopy.shadow.evaluator import TapeBook
from hlcopy.shadow.latency import LatencyScenario
from hlcopy.shadow.wide_score import (
    WideEpisode,
    WideScoreConfig,
    WideSignal,
    build_wide_episodes,
    load_wide_signals,
    score_wide_episode,
)

D = Decimal


class _Books:
    def __init__(self, books: list[TapeBook]) -> None:
        self.books = books

    def first_at_or_after(self, coin: str, exchange_ts_ms: float):  # type: ignore[no-untyped-def]
        eligible = [
            book
            for book in self.books
            if book.coin == coin and book.exchange_ts_ms >= exchange_ts_ms
        ]
        return min(eligible, key=lambda book: book.exchange_ts_ms, default=None)


def _book(coin: str, ts: int, bid: str, ask: str) -> TapeBook:
    return TapeBook(
        coin=coin,
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        bids=(BookLevel(D(bid), D("1000")),),
        asks=(BookLevel(D(ask), D("1000")),),
    )


def test_load_wide_signals_classifies_fresh_open_and_close(tmp_path: Path) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    address = "0x" + "a" * 40
    rows = [
        {
            "kind": "wide_official_fill",
            "wallet_id": "w",
            "wallet_address": address,
            "public_received_at_ns": 1_000_300_000_000,
            "official_fill": {
                "tid": 1,
                "oid": 1,
                "time": 1_000_000,
                "coin": "xyz:LLY",
                "side": "B",
                "dir": "Open Long",
                "px": "100",
                "sz": "1",
                "startPosition": "0",
                "closedPnl": "0",
                "fee": "0",
            },
        },
        {
            "kind": "wide_official_fill",
            "wallet_id": "w",
            "wallet_address": address,
            "public_received_at_ns": 1_060_300_000_000,
            "official_fill": {
                "tid": 2,
                "oid": 2,
                "time": 1_060_000,
                "coin": "xyz:LLY",
                "side": "A",
                "dir": "Close Long",
                "px": "101",
                "sz": "1",
                "startPosition": "1",
                "closedPnl": "1",
                "fee": "0",
            },
        },
    ]
    path = folder / "2026-08-11.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    signals = load_wide_signals(folder, cutoff_ns=1_000_000_000_000)
    assert [(signal.action, signal.direction, signal.coin) for signal in signals] == [
        ("OPEN", "LONG", "XYZ:LLY"),
        ("CLOSE", "LONG", "XYZ:LLY"),
    ]
    episodes = build_wide_episodes(signals)
    assert len(episodes) == 1
    assert episodes[0].exit is not None


def test_score_wide_open_uses_wire_hip3_book_and_net_fees() -> None:
    address = "0x" + "b" * 40
    entry = WideSignal(
        wallet_id="w",
        wallet_address=address,
        coin="XYZ:LLY",
        direction="LONG",
        action="OPEN",
        exchange_ts_ms=1_000_000,
        public_received_at_ns=1_000_300_000_000,
        source_price=D("100"),
        tid=1,
    )
    episode = WideEpisode(
        wallet_id="w",
        wallet_address=address,
        coin="XYZ:LLY",
        direction="LONG",
        entry=entry,
    )
    books = _Books(
        [
            _book("xyz:LLY", 1_000_600, "99.9", "100.1"),
            _book("xyz:LLY", 1_060_000, "100.9", "101.1"),
            _book("xyz:LLY", 1_300_000, "101.9", "102.1"),
        ]
    )
    score = score_wide_episode(
        episode,
        provider=books,  # type: ignore[arg-type]
        scenario=LatencyScenario("test", 50, 100, 100),
        config=WideScoreConfig(
            notional_usd=D("1000"),
            taker_fee_bps=D("4.5"),
            max_slippage_bps=D("20"),
            max_book_forward_ms=750,
            horizons_minutes=(1, 5),
        ),
        now_ms=1_400_000,
    )
    assert score.entry_vwap == D("100.1")
    assert score.markouts_net_bps["1m"] is not None
    assert score.markouts_net_bps["5m"] is not None
    assert score.markouts_net_bps["5m"] > score.markouts_net_bps["1m"]
    assert score.status == "OPEN_MARKOUT"
