from pathlib import Path

from hlcopy.discovery.leaderboard import LeaderboardCandidate, WindowPerformance
from hlcopy.discovery.universe import movement_signals, rank_universe
from hlcopy.discovery.universe_watch_cli import (
    SCOUT_MARKER,
    _register_research_wallets,
    _registration_rows,
)
from hlcopy.shadow.registry import WalletRegistry, WalletSpec


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


def _wallet(
    suffix: str,
    *,
    stage: str = "research",
    notes: str = "manual",
    coins: tuple[str, ...] = (),
) -> WalletSpec:
    address = "0x" + suffix.rjust(40, "0")
    return WalletSpec(
        id=f"hl-{address[2:]}",
        label=f"wallet-{suffix}",
        source_type="hyperliquid_wallet",
        source_ref=address,
        stage=stage,
        enabled=True,
        coins=coins,
        notes=notes,
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


def test_full_scout_pool_rotates_stale_wallets_and_preserves_protected(
    tmp_path: Path,
) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()

    manual = registry.add(_wallet("90", notes="manual-research"))
    validation = registry.add(
        _wallet("91", stage="validation", notes=f"{SCOUT_MARKER}; promoted")
    )
    approved = registry.add(
        _wallet("92", stage="approved", notes="manual-approved", coins=("BTC",))
    )
    stale_a = registry.add(_wallet("1", notes=f"{SCOUT_MARKER}; old-rank=500"))
    stale_b = registry.add(_wallet("2", notes=f"{SCOUT_MARKER}; old-rank=600"))

    candidates = [
        _candidate(
            suffix,
            account=20_000,
            day=day,
            week=day * 2,
            month=day * 4,
            all_time=day * 8,
        )
        for suffix, day in (("10", 0.30), ("11", 0.20), ("12", 0.10))
    ]
    rows = rank_universe(candidates)
    signals = {rows[0].address: ("ENTERED_TOP_100",)}

    added, removed, refreshed, skipped = _register_research_wallets(
        registry,
        rows,
        signals,
        max_total_research=3,
    )

    loaded = registry.load()
    by_address = {wallet.source_ref.lower(): wallet for wallet in loaded}
    assert set(added) == {rows[0].address, rows[1].address}
    assert set(removed) == {stale_a.source_ref.lower(), stale_b.source_ref.lower()}
    assert refreshed == []
    assert skipped == [rows[2].address]

    assert manual.source_ref.lower() in by_address
    assert by_address[manual.source_ref.lower()].notes == "manual-research"
    assert validation.source_ref.lower() in by_address
    assert by_address[validation.source_ref.lower()].stage == "validation"
    assert approved.source_ref.lower() in by_address
    assert by_address[approved.source_ref.lower()].stage == "approved"
    assert stale_a.source_ref.lower() not in by_address
    assert stale_b.source_ref.lower() not in by_address

    research_hl = [
        wallet
        for wallet in loaded
        if wallet.source_type == "hyperliquid_wallet" and wallet.stage == "research"
    ]
    assert len(research_hl) == 3
    assert SCOUT_MARKER in by_address[rows[0].address].notes
    assert "leaderboard_rank=1" in by_address[rows[0].address].notes


def test_existing_scout_wallet_metadata_is_refreshed(tmp_path: Path) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()
    existing = registry.add(
        _wallet("20", notes=f"{SCOUT_MARKER}; leaderboard_rank=99; score=1")
    )
    candidate = _candidate(
        "20", account=50_000, day=0.2, week=0.3, month=0.4, all_time=0.8
    )
    row = rank_universe([candidate])[0]

    added, removed, refreshed, skipped = _register_research_wallets(
        registry,
        [row],
        {row.address: ("RANK_JUMP_25",)},
        max_total_research=10,
    )

    assert added == []
    assert removed == []
    assert refreshed == [existing.source_ref.lower()]
    assert skipped == []
    stored = registry.load()[0]
    assert "leaderboard_rank=1" in stored.notes
    assert "RANK_JUMP_25" in stored.notes
