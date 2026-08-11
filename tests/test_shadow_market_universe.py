import asyncio
from pathlib import Path

from hlcopy.shadow.capture import (
    HyperliquidWalletFillCollector,
    load_market_coin_file,
    required_market_coins,
)
from hlcopy.shadow.registry import WalletRegistry


class _MemorySink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    async def put(self, row: dict[str, object]) -> None:
        self.rows.append(row)


def test_market_coin_file_preserves_multiplier_and_canonicalizes_hip3(
    tmp_path: Path,
) -> None:
    path = tmp_path / "active.txt"
    path.write_text("kBONK\nxyz:SNDK\nbtc\n", encoding="utf-8")

    assert load_market_coin_file(path) == ("kBONK", "XYZ:SNDK", "BTC")


def test_required_market_coins_does_not_destroy_multiplier_case(tmp_path: Path) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()

    assert required_market_coins(registry, ("kBONK", "xyz:SNDK")) == (
        "kBONK",
        "XYZ:SNDK",
    )


def test_live_wallet_fill_preserves_multiplier_symbol_case(tmp_path: Path) -> None:
    registry = WalletRegistry(tmp_path / "wallets.json")
    registry.init()
    sink = _MemorySink()
    collector = HyperliquidWalletFillCollector(
        ws_url="wss://example.invalid/ws",
        registry=registry,
        sink=sink,  # type: ignore[arg-type]
    )
    collector.started_ms = 0

    asyncio.run(
        collector._record_fill(
            user="0x" + "1" * 40,
            wallet=None,
            fill={"time": 1, "tid": 2, "coin": "kBONK"},
            is_snapshot=False,
            received_at_ns=1_000_000,
            received_monotonic_ns=1,
        )
    )

    wallet_rows = [row for row in sink.rows if row.get("kind") == "wallet_fill"]
    assert len(wallet_rows) == 1
    assert wallet_rows[0]["coin"] == "kBONK"
