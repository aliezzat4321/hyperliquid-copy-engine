from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from hlcopy.copyability.slippage import BookLevel
from hlcopy.shadow.evaluator import (
    ExecutionConfig,
    SourceEpisode,
    SourceEvent,
    TapeBook,
    evaluate_episode,
    load_prospective_episodes,
    summarize_executions,
)
from hlcopy.shadow.latency import LatencyScenario

D = Decimal
ADDRESS = "0x1111111111111111111111111111111111111111"


class StaticBooks:
    def __init__(self, books: list[TapeBook]) -> None:
        self.books = sorted(books, key=lambda book: book.exchange_ts_ms)

    def first_at_or_after(self, coin: str, exchange_ts_ms: float):
        return next(
            (
                book
                for book in self.books
                if book.coin == coin and book.exchange_ts_ms >= exchange_ts_ms
            ),
            None,
        )


def _book(ts: int, bid: str, ask: str) -> TapeBook:
    return TapeBook(
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        bids=(BookLevel(D(bid), D("100")),),
        asks=(BookLevel(D(ask), D("100")),),
    )


def _event(action: str, direction: str, ts: int, receipt_ms: int, px: str, tid: int):
    return SourceEvent(
        wallet_id="alpha",
        wallet_address=ADDRESS,
        coin="BTC",
        action=action,
        direction=direction,
        exchange_ts_ms=ts,
        received_at_ns=receipt_ms * 1_000_000,
        source_fill_price=D(px),
        source_tid=tid,
    )


def test_evaluator_separates_measured_feed_lag_from_explicit_order_path():
    episode = SourceEpisode(
        wallet_id="alpha",
        wallet_address=ADDRESS,
        coin="BTC",
        direction="LONG",
        entry=_event("OPEN", "LONG", 1_000, 1_100, "100", 1),
        exit=_event("CLOSE", "LONG", 11_000, 11_120, "101", 2),
    )
    provider = StaticBooks(
        [
            _book(1_160, "100", "100.1"),
            _book(11_180, "101", "101.1"),
        ]
    )
    scenario = LatencyScenario(
        "measured",
        decision_ms=10,
        outbound_order_ms=40,
        exchange_processing_ms=5,
    )
    config = ExecutionConfig(
        notional_usd=D("1000"),
        follower_leverage=D("5"),
        taker_fee_bps=D("4.5"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=750,
    )
    result = evaluate_episode(episode, provider=provider, scenario=scenario, config=config)
    assert result.status == "EXECUTED"
    assert result.entry_signal_feed_ms == 100.0
    assert result.entry_target_arrival_ms == 1_155.0
    assert result.entry_book_forward_ms == 5.0
    assert result.net_underlying_bps is not None
    assert result.net_underlying_bps > 0
    assert result.net_return_on_margin_pct is not None
    assert result.net_return_on_margin_pct > 0

    summary = summarize_executions("alpha", scenario, config, [result])
    assert summary.execution_fraction == D("1")
    assert summary.net_win_rate == D("1")
    assert summary.funding_mode == "NOT_MODELED"
    assert summary.liquidation_path_mode == "NOT_MODELED"


def test_evaluator_fails_closed_when_next_l2_snapshot_is_too_far_forward():
    episode = SourceEpisode(
        wallet_id="alpha",
        wallet_address=ADDRESS,
        coin="BTC",
        direction="SHORT",
        entry=_event("OPEN", "SHORT", 1_000, 1_100, "100", 1),
        exit=_event("CLOSE", "SHORT", 11_000, 11_100, "99", 2),
    )
    provider = StaticBooks([_book(3_000, "99", "100"), _book(13_000, "98", "99")])
    config = ExecutionConfig(
        notional_usd=D("1000"),
        follower_leverage=D("5"),
        taker_fee_bps=D("4.5"),
        max_slippage_bps=D("20"),
        max_book_forward_ms=750,
    )
    result = evaluate_episode(
        episode,
        provider=provider,
        scenario=LatencyScenario("measured", 0, 0),
        config=config,
    )
    assert result.status == "MISSED"
    assert result.reason == "ENTRY_BOOK_TOO_FAR_FORWARD"


def test_fill_stream_is_reconstructed_into_flat_to_flat_episode(tmp_path: Path):
    fills_dir = tmp_path / "fills"
    fills_dir.mkdir(parents=True)
    rows = [
        {
            "kind": "wallet_fill",
            "wallet_id": "alpha",
            "wallet_address": ADDRESS,
            "received_at_ns": 1_100_000_000,
            "is_snapshot": False,
            "fill": {
                "coin": "BTC",
                "px": "100",
                "sz": "1",
                "side": "B",
                "time": 1000,
                "startPosition": "0",
                "dir": "Open Long",
                "closedPnl": "0",
                "hash": "0x1",
                "oid": 1,
                "tid": 1,
                "fee": "0",
            },
        },
        {
            "kind": "wallet_fill",
            "wallet_id": "alpha",
            "wallet_address": ADDRESS,
            "received_at_ns": 2_100_000_000,
            "is_snapshot": False,
            "fill": {
                "coin": "BTC",
                "px": "100.5",
                "sz": "1",
                "side": "B",
                "time": 2000,
                "startPosition": "1",
                "dir": "Add Long",
                "closedPnl": "0",
                "hash": "0x2",
                "oid": 2,
                "tid": 2,
                "fee": "0",
            },
        },
        {
            "kind": "wallet_fill",
            "wallet_id": "alpha",
            "wallet_address": ADDRESS,
            "received_at_ns": 3_100_000_000,
            "is_snapshot": False,
            "fill": {
                "coin": "BTC",
                "px": "101",
                "sz": "2",
                "side": "A",
                "time": 3000,
                "startPosition": "2",
                "dir": "Close Long",
                "closedPnl": "2",
                "hash": "0x3",
                "oid": 3,
                "tid": 3,
                "fee": "0",
            },
        },
    ]
    path = fills_dir / "2026-08-10.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    episodes = load_prospective_episodes(tmp_path, "alpha")
    assert len(episodes) == 1
    assert episodes[0].direction == "LONG"
    assert episodes[0].entry.source_tid == 1
    assert episodes[0].exit.source_tid == 3
