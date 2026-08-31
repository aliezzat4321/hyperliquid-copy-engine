from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from hlcopy.profitability.notification_edge import (
    EdgePolicy,
    ShadowTrade,
    bootstrap_mean_ci,
    build_report,
    load_audit_rows,
    reconstruct_ledger,
    score_slice,
    signal_age_bucket,
    stale_signal_report,
)

D = Decimal


def open_row(
    source_id: str,
    *,
    ts: str,
    coin: str = "BTC",
    side: str = "long",
    user: str = "carmine",
    entry_mid: str = "100",
    size: str = "1",
    leverage: int = 10,
    latency_ms: int | None = 3000,
    chase_bps: str | None = "1.5",
    entry_price: str | None = "100",
    row_type: str = "shadow_opened",
) -> dict:
    return {
        "ts": ts,
        "type": row_type,
        "entryMid": entry_mid,
        "size": size,
        "notionalUsd": str(D(entry_mid) * D(size)),
        "marginUsd": str(D(entry_mid) * D(size) / D(leverage)),
        "chaseBps": chase_bps,
        "detectionLatencyMs": latency_ms,
        "signal": {
            "sourceBaseId": source_id,
            "username": user,
            "coin": coin,
            "side": side,
            "leverage": leverage,
            "entryPrice": entry_price,
        },
    }


def close_row(
    source_id: str,
    *,
    ts: str,
    exit_mid: str,
    coin: str = "BTC",
    side: str = "long",
    user: str = "carmine",
    source_closing_price: str | None = None,
    gross_pnl: str | None = None,
) -> dict:
    return {
        "ts": ts,
        "type": "shadow_closed",
        "username": user,
        "coin": coin,
        "side": side,
        "sourceBaseId": source_id,
        "exitMid": exit_mid,
        "sourceClosingPrice": source_closing_price,
        "grossPnlUsd": gross_pnl,
        "signal": {"sourceBaseId": source_id, "username": user, "coin": coin, "side": side},
    }


def test_open_and_close_pair_into_a_scored_trade():
    ledger = reconstruct_ledger(
        [
            open_row("a", ts="2026-08-01T00:00:00.000Z", entry_mid="100", size="2"),
            close_row("a", ts="2026-08-01T01:00:00.000Z", exit_mid="101"),
        ]
    )
    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.closed
    assert trade.gross_pnl_usd == D("2")
    assert trade.gross_return_bps == D("100")
    assert trade.hold_ms == 3_600_000
    assert trade.notional_usd == D("200")


def test_short_side_pnl_is_signed_correctly():
    ledger = reconstruct_ledger(
        [
            open_row("s", ts="2026-08-01T00:00:00Z", side="short", entry_mid="100", size="1"),
            close_row("s", ts="2026-08-01T00:30:00Z", side="short", exit_mid="98"),
        ]
    )
    trade = ledger.trades[0]
    assert trade.gross_pnl_usd == D("2")
    assert trade.gross_return_bps == D("200")


def test_duplicate_close_posts_are_counted_once():
    rows = [
        open_row("dup", ts="2026-08-01T00:00:00Z"),
        close_row("dup", ts="2026-08-01T00:10:00Z", exit_mid="101"),
        close_row("dup", ts="2026-08-01T00:10:05Z", exit_mid="101"),
    ]
    ledger = reconstruct_ledger(rows)
    assert len(ledger.trades) == 1
    assert ledger.integrity.duplicate_close_rows == 1
    assert ledger.integrity.orphan_close_rows == 0


def test_close_without_a_matching_open_is_never_scored():
    ledger = reconstruct_ledger([close_row("ghost", ts="2026-08-01T00:00:00Z", exit_mid="101")])
    assert ledger.trades == ()
    assert ledger.integrity.orphan_close_rows == 1


def test_unclosed_positions_are_retained_as_open_not_dropped():
    ledger = reconstruct_ledger(
        [
            open_row("closed", ts="2026-08-01T00:00:00Z"),
            close_row("closed", ts="2026-08-01T01:00:00Z", exit_mid="103"),
            open_row("still-open", ts="2026-08-01T02:00:00Z"),
        ]
    )
    assert len(ledger.closed) == 1
    assert len(ledger.open) == 1
    assert ledger.open[0].gross_pnl_usd is None


def test_reup_updates_size_entry_and_notional():
    rows = [
        open_row("r", ts="2026-08-01T00:00:00Z", entry_mid="100", size="1"),
        {
            "ts": "2026-08-01T00:05:00Z",
            "type": "shadow_reupped",
            "sourceBaseId": "r",
            "newSize": "2",
            "newEntryMid": "105",
            "addNotionalUsd": "110",
            "addMarginUsd": "11",
        },
        close_row("r", ts="2026-08-01T01:00:00Z", exit_mid="110"),
    ]
    trade = reconstruct_ledger(rows).trades[0]
    assert trade.size == D("2")
    assert trade.entry_mid == D("105")
    assert trade.add_count == 1
    assert trade.notional_usd == D("210")
    assert trade.gross_pnl_usd == D("10")


def test_reported_pnl_disagreement_is_surfaced_not_silently_trusted():
    rows = [
        open_row("m", ts="2026-08-01T00:00:00Z", entry_mid="100", size="1"),
        close_row("m", ts="2026-08-01T01:00:00Z", exit_mid="101", gross_pnl="99"),
    ]
    ledger = reconstruct_ledger(rows)
    assert ledger.integrity.pnl_mismatch_rows == 1
    # The recomputed value wins; the reported one is evidence, not truth.
    assert ledger.trades[0].gross_pnl_usd == D("1")


def test_exit_vs_source_measures_our_exit_against_the_source_close():
    rows = [
        open_row("x", ts="2026-08-01T00:00:00Z", entry_mid="100", size="1"),
        close_row(
            "x", ts="2026-08-01T01:00:00Z", exit_mid="99", source_closing_price="100"
        ),
    ]
    trade = reconstruct_ledger(rows).trades[0]
    # Long exiting 1% below where the source closed is a 100 bps worse exit.
    assert trade.exit_vs_source_bps == D("-100")


def test_cost_is_charged_on_both_legs():
    trade = ShadowTrade(
        source_base_id="c",
        username="u",
        coin="BTC",
        side="long",
        leverage=1,
        opened_at_ms=0,
        entry_mid=D("100"),
        size=D("1"),
        notional_usd=D("100"),
        margin_usd=D("100"),
        add_count=0,
        closed_at_ms=1000,
        exit_mid=D("100"),
    )
    # Flat price, 20 bps round trip: 10 bps on each 100 USD leg.
    assert trade.cost_usd(D("20")) == D("0.20")
    assert trade.net_pnl_usd(D("20")) == D("-0.20")
    assert trade.net_return_bps(D("20")) == D("-20")


def test_breakeven_cost_equals_mean_gross_return():
    trades = [
        reconstruct_ledger(
            [
                open_row(f"t{i}", ts=f"2026-08-0{i}T00:00:00Z", entry_mid="100", size="1"),
                close_row(f"t{i}", ts=f"2026-08-0{i}T01:00:00Z", exit_mid=exit_mid),
            ]
        ).trades[0]
        for i, exit_mid in enumerate(["100.1", "100.3", "100.2"], start=1)
    ]
    scored = score_slice("all", "ALL", trades)
    assert scored.mean_gross_return_bps == D("20")
    assert scored.breakeven_cost_bps == D("20")
    net = scored.net_by_cost_bps["15"]
    assert net["mean_net_return_bps"] is not None
    assert net["mean_net_return_bps"] < D("20")


def test_thin_sample_is_blocked_from_promotion():
    trades = [
        reconstruct_ledger(
            [
                open_row("s1", ts="2026-08-01T00:00:00Z"),
                close_row("s1", ts="2026-08-01T01:00:00Z", exit_mid="150"),
            ]
        ).trades[0]
    ]
    scored = score_slice("trader", "lucky", trades, policy=EdgePolicy())
    assert scored.verdict == "NOT_READY"
    assert any(blocker.startswith("SAMPLE_") for blocker in scored.blockers)


def test_slice_mostly_still_open_is_flagged_as_survivorship_biased():
    rows = [
        open_row("c1", ts="2026-08-01T00:00:00Z"),
        close_row("c1", ts="2026-08-01T01:00:00Z", exit_mid="110"),
    ]
    for i in range(5):
        rows.append(open_row(f"o{i}", ts=f"2026-08-02T0{i}:00:00Z"))
    ledger = reconstruct_ledger(rows)
    scored = score_slice("trader", "carmine", ledger.trades)
    assert any(blocker.startswith("UNRESOLVED_") for blocker in scored.blockers)


def test_concentrated_profit_is_blocked():
    rows = []
    for i in range(1, 10):
        rows.append(open_row(f"p{i}", ts=f"2026-08-{i:02d}T00:00:00Z", entry_mid="100", size="1"))
        rows.append(close_row(f"p{i}", ts=f"2026-08-{i:02d}T01:00:00Z", exit_mid="100.01"))
    rows.append(open_row("big", ts="2026-08-20T00:00:00Z", entry_mid="100", size="1"))
    rows.append(close_row("big", ts="2026-08-20T01:00:00Z", exit_mid="200"))
    ledger = reconstruct_ledger(rows)
    scored = score_slice(
        "trader",
        "carmine",
        ledger.trades,
        policy=EdgePolicy(min_closed_trades=5, min_distinct_days=3),
    )
    assert scored.profit_concentration is not None
    assert scored.profit_concentration > D("0.9")
    assert any(blocker.startswith("CONCENTRATED_") for blocker in scored.blockers)


def test_a_genuinely_strong_slice_clears_every_gate():
    rows = []
    for i in range(1, 41):
        day = f"2026-08-{((i - 1) % 10) + 1:02d}"
        rows.append(open_row(f"g{i}", ts=f"{day}T{i % 24:02d}:00:00Z", entry_mid="100", size="1"))
        # Tight, repeatable 60 bps gross: comfortably above the 15 bps reference cost.
        exit_mid = "100.55" if i % 4 else "100.75"
        rows.append(close_row(f"g{i}", ts=f"{day}T{(i % 24):02d}:30:00Z", exit_mid=exit_mid))
    ledger = reconstruct_ledger(rows)
    scored = score_slice("trader", "carmine", ledger.trades, policy=EdgePolicy())
    assert scored.closed_trades == 40
    assert scored.blockers == ()
    assert scored.verdict == "ELIGIBLE_FOR_MICRO_LIVE"
    assert scored.gross_return_ci_low_bps is not None
    assert scored.gross_return_ci_low_bps > D("15")


def test_the_same_strong_slice_fails_once_costs_exceed_its_edge():
    rows = []
    for i in range(1, 41):
        day = f"2026-08-{((i - 1) % 10) + 1:02d}"
        rows.append(open_row(f"g{i}", ts=f"{day}T{i % 24:02d}:00:00Z", entry_mid="100", size="1"))
        rows.append(close_row(f"g{i}", ts=f"{day}T{(i % 24):02d}:30:00Z", exit_mid="100.20"))
    ledger = reconstruct_ledger(rows)
    scored = score_slice(
        "trader", "carmine", ledger.trades, policy=EdgePolicy(reference_cost_bps=D("40"))
    )
    assert scored.verdict == "NOT_READY"
    assert any(blocker.startswith("CI_LOW_") for blocker in scored.blockers)


def test_bootstrap_is_reproducible_and_brackets_the_mean():
    values = [D(str(v)) for v in (10, 12, 8, 15, 9, 11, 13, 7, 14, 10)]
    first = bootstrap_mean_ci(values)
    second = bootstrap_mean_ci(values)
    assert first == second
    assert first is not None
    low, high = first
    assert low < D("10.9") < high


def test_bootstrap_needs_at_least_two_observations():
    assert bootstrap_mean_ci([D("5")]) is None


def test_signal_age_buckets_split_the_freshness_window():
    def trade_with_age(age_ms: int | None) -> ShadowTrade:
        return ShadowTrade(
            source_base_id="a",
            username="u",
            coin="BTC",
            side="long",
            leverage=1,
            opened_at_ms=0,
            entry_mid=D("1"),
            size=D("1"),
            notional_usd=D("1"),
            margin_usd=D("1"),
            add_count=0,
            signal_age_ms=age_ms,
        )

    assert signal_age_bucket(trade_with_age(500)) == "age_0_2s"
    assert signal_age_bucket(trade_with_age(11_000)) == "age_10_15s"
    assert signal_age_bucket(trade_with_age(24_000)) == "age_15s_plus"
    assert signal_age_bucket(trade_with_age(None)) == "unknown"


def test_stale_skips_quantify_what_the_freshness_gate_refused():
    rows = [
        open_row("k", ts="2026-08-01T00:00:00Z"),
        close_row("k", ts="2026-08-01T01:00:00Z", exit_mid="101"),
    ]
    for i, age in enumerate([26_000, 30_000, 41_000, 120_000]):
        rows.append(
            {
                "ts": f"2026-08-01T0{i}:00:00Z",
                "type": "skip",
                "reason": "stale_signal_over_25s_window",
                "ageMs": age,
                "signal": {"username": "rps", "coin": "ETH", "side": "short"},
            }
        )
    report = stale_signal_report(reconstruct_ledger(rows))
    assert report.skipped == 4
    assert report.admitted == 1
    assert report.median_age_ms == D("35500")
    assert report.max_age_ms == 120_000
    assert report.by_trader == {"rps": 4}
    assert report.rejection_rate == D("4") / D("5")


def test_other_skip_reasons_are_not_counted_as_stale():
    rows = [
        {
            "ts": "2026-08-01T00:00:00Z",
            "type": "skip",
            "reason": "close_not_owned_by_service",
            "signal": {"username": "rps", "coin": "ETH", "side": "short"},
        }
    ]
    assert stale_signal_report(reconstruct_ledger(rows)).skipped == 0


def test_report_is_json_serialisable_and_carries_the_safety_flag():
    rows = [
        open_row("j", ts="2026-08-01T00:00:00Z"),
        close_row("j", ts="2026-08-01T01:00:00Z", exit_mid="101"),
    ]
    report = build_report(reconstruct_ledger(rows))
    encoded = json.dumps(report, sort_keys=True)
    assert '"real_trading": false' in encoded
    assert report["model_version"] == "invo-notification-edge-v1"
    assert report["integrity"]["close_rows"] == 1


def test_loader_tolerates_blank_and_corrupt_lines(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(open_row("l", ts="2026-08-01T00:00:00Z")),
                "",
                "{not json",
                json.dumps(close_row("l", ts="2026-08-01T01:00:00Z", exit_mid="101")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rows = load_audit_rows(path)
    assert len(rows) == 2
    assert len(reconstruct_ledger(rows).closed) == 1


def test_loader_returns_empty_for_a_missing_file(tmp_path: Path):
    assert load_audit_rows(tmp_path / "nope.jsonl") == ()


@pytest.mark.parametrize("bad_entry", ["0", "-1"])
def test_non_positive_entry_prices_are_rejected_as_unpriced(bad_entry: str):
    ledger = reconstruct_ledger(
        [open_row("bad", ts="2026-08-01T00:00:00Z", entry_mid=bad_entry)]
    )
    assert ledger.trades == ()
    assert ledger.integrity.unpriced_rows == 1
