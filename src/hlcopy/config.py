from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _market_coins_from_env() -> tuple[str, ...]:
    raw = os.getenv("HLCOPY_MARKET_COINS", "BTC,ETH,SOL")
    coins = tuple(dict.fromkeys(part.strip().upper() for part in raw.split(",") if part.strip()))
    if not coins:
        raise ValueError("HLCOPY_MARKET_COINS must contain at least one coin")
    return coins


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    network: str = "mainnet"
    max_candidates: int = 25
    min_account_value: float = 10_000.0
    min_month_roi: float = 0.0
    min_month_volume: float = 50_000.0
    http_concurrency: int = 3
    output_dir: Path = Path("outputs")
    profile_candidates: int = 20
    profile_lookback_days: int = 90
    leaderboard_snapshot_interval_minutes: int = 240
    market_data_dir: Path = Path("data/market")
    market_coins: tuple[str, ...] = ("BTC", "ETH", "SOL")
    market_flush_rows: int = 5_000
    # Busy partitions flush at market_flush_rows; this is only the maximum
    # durability interval for low-volume partitions. The old five-second global
    # flush produced ~1.5M tiny files on the research volume.
    market_flush_seconds: float = 120.0
    market_queue_size: int = 50_000
    ws_heartbeat_seconds: float = 30.0
    ws_reconnect_base_seconds: float = 1.0
    ws_reconnect_max_seconds: float = 30.0

    @property
    def api_url(self) -> str:
        if self.network == "testnet":
            return "https://api.hyperliquid-testnet.xyz"
        return "https://api.hyperliquid.xyz"

    @property
    def ws_url(self) -> str:
        if self.network == "testnet":
            return "wss://api.hyperliquid-testnet.xyz/ws"
        return "wss://api.hyperliquid.xyz/ws"

    @property
    def leaderboard_url(self) -> str:
        network = "Testnet" if self.network == "testnet" else "Mainnet"
        return f"https://stats-data.hyperliquid.xyz/{network}/leaderboard"

    @classmethod
    def from_env(cls) -> Settings:
        network = os.getenv("HLCOPY_NETWORK", "mainnet").lower().strip()
        if network not in {"mainnet", "testnet"}:
            raise ValueError("HLCOPY_NETWORK must be 'mainnet' or 'testnet'")
        return cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://hlcopy:hlcopy@localhost:5432/hlcopy"
            ),
            network=network,
            max_candidates=max(1, int(os.getenv("HLCOPY_MAX_CANDIDATES", "25"))),
            min_account_value=float(os.getenv("HLCOPY_MIN_ACCOUNT_VALUE", "10000")),
            min_month_roi=float(os.getenv("HLCOPY_MIN_MONTH_ROI", "0")),
            min_month_volume=float(os.getenv("HLCOPY_MIN_MONTH_VOLUME", "50000")),
            http_concurrency=max(1, int(os.getenv("HLCOPY_HTTP_CONCURRENCY", "3"))),
            output_dir=Path(os.getenv("HLCOPY_OUTPUT_DIR", "outputs")),
            profile_candidates=max(1, int(os.getenv("HLCOPY_PROFILE_CANDIDATES", "20"))),
            profile_lookback_days=max(
                1,
                int(os.getenv("HLCOPY_PROFILE_LOOKBACK_DAYS", "90")),
            ),
            leaderboard_snapshot_interval_minutes=max(
                1,
                int(os.getenv("HLCOPY_LEADERBOARD_SNAPSHOT_INTERVAL_MINUTES", "240")),
            ),
            market_data_dir=Path(os.getenv("HLCOPY_MARKET_DATA_DIR", "data/market")),
            market_coins=_market_coins_from_env(),
            market_flush_rows=max(1, int(os.getenv("HLCOPY_MARKET_FLUSH_ROWS", "5000"))),
            market_flush_seconds=max(
                1.0,
                float(os.getenv("HLCOPY_MARKET_FLUSH_SECONDS", "120")),
            ),
            market_queue_size=max(100, int(os.getenv("HLCOPY_MARKET_QUEUE_SIZE", "50000"))),
            ws_heartbeat_seconds=max(
                5.0,
                float(os.getenv("HLCOPY_WS_HEARTBEAT_SECONDS", "30")),
            ),
            ws_reconnect_base_seconds=max(
                0.1,
                float(os.getenv("HLCOPY_WS_RECONNECT_BASE_SECONDS", "1")),
            ),
            ws_reconnect_max_seconds=max(
                1.0,
                float(os.getenv("HLCOPY_WS_RECONNECT_MAX_SECONDS", "30")),
            ),
        )
