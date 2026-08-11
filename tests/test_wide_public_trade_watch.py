import asyncio
from pathlib import Path

from hlcopy.shadow.registry import WalletRegistry, WalletSpec
from hlcopy.shadow.wide_watch import HyperliquidWideTradeCollector, tracked_wallet_map


class _MemorySink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    async def put(self, row: dict[str, object]) -> None:
        self.rows.append(row)


def _address(digit: str) -> str:
    return "0x" + digit * 40


def test_wide_watch_tracks_enabled_research_and_validation_wallets(tmp_path: Path) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()
    registry.add(
        WalletSpec(
            id="research",
            label="research",
            source_type="hyperliquid_wallet",
            source_ref=_address("1"),
            stage="research",
        )
    )
    registry.add(
        WalletSpec(
            id="validation",
            label="validation",
            source_type="hyperliquid_wallet",
            source_ref=_address("2"),
            stage="validation",
            coins=("BTC",),
        )
    )
    registry.add(
        WalletSpec(
            id="rejected",
            label="rejected",
            source_type="hyperliquid_wallet",
            source_ref=_address("3"),
            stage="rejected",
        )
    )
    registry.add(
        WalletSpec(
            id="disabled",
            label="disabled",
            source_type="hyperliquid_wallet",
            source_ref=_address("4"),
            stage="research",
            enabled=False,
        )
    )

    tracked = tracked_wallet_map(registry)
    assert set(tracked) == {_address("1"), _address("2")}


def test_wide_watch_records_only_tracked_trade_participants(tmp_path: Path) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()
    tracked_address = _address("a")
    registry.add(
        WalletSpec(
            id="tracked",
            label="tracked",
            source_type="hyperliquid_wallet",
            source_ref=tracked_address,
            stage="research",
        )
    )
    coins_file = tmp_path / "coins.txt"
    coins_file.write_text("BTC\n", encoding="utf-8")
    sink = _MemorySink()
    collector = HyperliquidWideTradeCollector(
        ws_url="wss://example.invalid/ws",
        registry=registry,
        coins_file=coins_file,
        sink=sink,  # type: ignore[arg-type]
    )
    tracked = tracked_wallet_map(registry)

    asyncio.run(
        collector._record_trade(
            trade={
                "coin": "BTC",
                "side": "B",
                "px": "64000",
                "sz": "0.1",
                "hash": "0xabc",
                "time": 1_780_000_000_000,
                "tid": 123,
                "users": [tracked_address, _address("b")],
            },
            tracked=tracked,
            received_at_ns=1_780_000_000_250_000_000,
            received_monotonic_ns=99,
        )
    )

    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["kind"] == "public_wallet_trade"
    assert row["wallet_address"] == tracked_address
    assert row["target_side"] == "BUY"
    assert row["coin"] == "BTC"
    assert row["tid"] == 123
    assert row["observed_event_lag_ms"] == 250.0

    asyncio.run(
        collector._record_trade(
            trade={
                "coin": "BTC",
                "side": "A",
                "px": "64001",
                "sz": "0.2",
                "hash": "0xdef",
                "time": 1_780_000_000_001,
                "tid": 124,
                "users": [_address("c"), _address("d")],
            },
            tracked=tracked,
            received_at_ns=1_780_000_000_251_000_000,
            received_monotonic_ns=100,
        )
    )

    assert len(sink.rows) == 1


def test_wide_watch_service_is_separate_from_direct_user_fill_validator() -> None:
    unit = Path("deploy/systemd/hyperliquid-wide-trade-watch.service").read_text()
    assert "hlcopy.shadow.wide_cli" in unit
    assert "active_perp_markets.txt" in unit
    assert "shadow/wide-trades" in unit
    assert "REAL_TRADING_ENABLED=NO" in unit
