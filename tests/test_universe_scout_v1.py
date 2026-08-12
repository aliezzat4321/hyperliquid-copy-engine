from hlcopy.discovery.leaderboard import LeaderboardCandidate, WindowPerformance
from hlcopy.discovery.universe import movement_signals, rank_universe
from hlcopy.discovery.universe_watch_cli import _registration_rows


def _candidate(
    address_suffix: str,
    *,
    account: float,
    day: float,
    week: float,
    month: float,
    all_time: float,
    month_pnl: float = 1_000.0,
    month_volume: float = 100_000.0,
) -> LeaderboardCandidate:
    return LeaderboardCandidate(
        address="0x" + address_suffix.rjust(40, "0"),
        display_name=None,
        account_value=account,
        windows={
            "day": WindowPerformance(pnl=day * account, roi=day, volume=month_volume / 30),
            "week": WindowPerformance(pnl=week * account, roi=week, volume=month_volume / 4),
            "month": WindowPerformance(pnl=month_pnl, roi=month, volume=month_volume),
            "allTime": WindowPerformance(pnl=max(month_pnl, all_time * account), roi=all_time),
        },
        raw={},
    )


def test_rank_universe_rewards_persistent_multi_horizon_edge() -> None:
    persistent = _candidate(
        "1", account=20_000, day=0.03, week=0.12, month=0.35, all_time=1.2
    )
    one_spike = _candidate(
        "2", account=20_000, day=0.80, week=-0.10, month=-0.20, all_time=0.05
    )
    tiny = _candidate(
        "3", account=100, day=1.0, week=1.0, month=1.0, all_time=1.0
    )

    rows = rank_universe([one_spike, tiny, persistent], min_account_value=1_000)

    assert [row.address for row in rows] == [persistent.address, one_spike.address]
    assert rows[0].positive_windows == 4


def test_movement_signals_detect_new_top_and_rank_jump() -> None:
    candidates = [
        _candidate(
            str(i),
            account=10_000,
            day=0.01 + i / 1000,
            week=0.02 + i / 1000,
            month=0.03 + i / 1000,
            all_time=0.10 + i / 1000,
        )
        for i in range(1, 5)
    ]
    rows = rank_universe(candidates)
    leader = rows[0]
    previous = {leader.address: 40}

    signals = movement_signals(rows, previous)

    assert "RANK_JUMP_25" in signals[leader.address]
    assert "TOP_50" in signals[leader.address]
    new_wallet = rows[1]
    assert "NEW_TO_OBSERVED_LEADERBOARD" in signals[new_wallet.address]
    assert "ENTERED_TOP_100" in signals[new_wallet.address]


def test_registration_includes_top_universe_and_movers() -> None:
    candidates = [
        _candidate(
            str(i),
            account=10_000,
            day=0.01 + i / 1000,
            week=0.02 + i / 1000,
            month=0.03 + i / 1000,
            all_time=0.10 + i / 1000,
        )
        for i in range(1, 8)
    ]
    rows = rank_universe(candidates)
    mover = rows[5]
    signals = {mover.address: ("RANK_JUMP_25",)}

    selected = _registration_rows(rows, signals, top_n=2, movement_limit=10)

    addresses = {row.address for row in selected}
    assert rows[0].address in addresses
    assert rows[1].address in addresses
    assert mover.address in addresses
    assert len(selected) == 3
