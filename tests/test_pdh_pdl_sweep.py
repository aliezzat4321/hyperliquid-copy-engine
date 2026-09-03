from datetime import UTC, date, datetime, timedelta

from hlcopy.research.pdh_pdl_sweep import Bar, build_setups, simulate_trade


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _bar(value: datetime, open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(_ms(value), open_, high, low, close, 1.0)


def _full_day(day: date, base: float = 100.0) -> list[Bar]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [
        _bar(start + timedelta(minutes=5 * index), base, 101.0, 99.0, base)
        for index in range(288)
    ]


def _current_day(day: date) -> list[Bar]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [
        _bar(start + timedelta(minutes=5 * index), 100.0, 100.5, 99.5, 100.0)
        for index in range(288)
    ]


def test_short_sweep_reclaim_confirmation_and_target() -> None:
    bars = _full_day(date(2025, 1, 1)) + _current_day(date(2025, 1, 2))
    by_time = {bar.open_time_ms: bar for bar in bars}

    sweep_dt = datetime(2025, 1, 2, 8, 10, tzinfo=UTC)
    confirm_dt = datetime(2025, 1, 2, 8, 15, tzinfo=UTC)
    target_dt = datetime(2025, 1, 2, 8, 20, tzinfo=UTC)
    by_time[_ms(sweep_dt)] = _bar(sweep_dt, 100.8, 102.0, 100.6, 100.9)
    by_time[_ms(confirm_dt)] = _bar(confirm_dt, 100.8, 100.9, 99.8, 100.5)
    by_time[_ms(target_dt)] = _bar(target_dt, 100.5, 100.6, 96.0, 97.0)

    bars = list(by_time.values())
    setups, _ = build_setups("BTCUSDT", bars)
    assert len(setups) == 1
    assert setups[0].direction == "SHORT"

    trade = simulate_trade(
        setups[0], {bar.open_time_ms: bar for bar in bars}, target_r=1.0
    )
    assert trade is not None
    assert trade.exit_reason == "TARGET"
    assert trade.net_r < trade.gross_r


def test_breach_without_reclaim_does_not_trade() -> None:
    bars = _full_day(date(2025, 1, 1)) + _current_day(date(2025, 1, 2))
    by_time = {bar.open_time_ms: bar for bar in bars}
    sweep_dt = datetime(2025, 1, 2, 8, 10, tzinfo=UTC)
    by_time[_ms(sweep_dt)] = _bar(sweep_dt, 101.0, 102.0, 100.8, 101.5)

    setups, _ = build_setups("BTCUSDT", list(by_time.values()))
    assert setups == []


def test_london_window_is_dst_aware() -> None:
    bars = _full_day(date(2025, 6, 1)) + _current_day(date(2025, 6, 2))
    by_time = {bar.open_time_ms: bar for bar in bars}

    # 07:10 UTC is 08:10 in London during BST.
    sweep_dt = datetime(2025, 6, 2, 7, 10, tzinfo=UTC)
    confirm_dt = datetime(2025, 6, 2, 7, 15, tzinfo=UTC)
    by_time[_ms(sweep_dt)] = _bar(sweep_dt, 100.8, 102.0, 100.6, 100.9)
    by_time[_ms(confirm_dt)] = _bar(confirm_dt, 100.8, 100.9, 99.8, 100.5)

    setups, _ = build_setups("BTCUSDT", list(by_time.values()))
    assert len(setups) == 1
    assert setups[0].sweep_time_ms == _ms(sweep_dt)


def test_same_bar_stop_and_target_is_scored_as_stop() -> None:
    bars = _full_day(date(2025, 1, 1)) + _current_day(date(2025, 1, 2))
    by_time = {bar.open_time_ms: bar for bar in bars}
    sweep_dt = datetime(2025, 1, 2, 8, 10, tzinfo=UTC)
    confirm_dt = datetime(2025, 1, 2, 8, 15, tzinfo=UTC)
    ambiguous_dt = datetime(2025, 1, 2, 8, 20, tzinfo=UTC)
    by_time[_ms(sweep_dt)] = _bar(sweep_dt, 100.8, 102.0, 100.6, 100.9)
    by_time[_ms(confirm_dt)] = _bar(confirm_dt, 100.8, 100.9, 99.8, 100.5)
    by_time[_ms(ambiguous_dt)] = _bar(ambiguous_dt, 100.5, 103.0, 96.0, 100.0)

    bars = list(by_time.values())
    setups, _ = build_setups("BTCUSDT", bars)
    trade = simulate_trade(
        setups[0], {bar.open_time_ms: bar for bar in bars}, target_r=1.0
    )
    assert trade is not None
    assert trade.exit_reason == "STOP"
    assert trade.gross_r == -1.0


def test_incomplete_previous_day_fails_closed() -> None:
    previous = _full_day(date(2025, 1, 1))[:-1]
    bars = previous + _current_day(date(2025, 1, 2))
    setups, diagnostics = build_setups("BTCUSDT", bars)
    assert setups == []
    day = next(item for item in diagnostics if item.trading_day == date(2025, 1, 2))
    assert day.eligible_previous_day is False
