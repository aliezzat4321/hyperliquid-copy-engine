from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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

    @property
    def api_url(self) -> str:
        if self.network == "testnet":
            return "https://api.hyperliquid-testnet.xyz"
        return "https://api.hyperliquid.xyz"

    @property
    def leaderboard_url(self) -> str:
        network = "Testnet" if self.network == "testnet" else "Mainnet"
        return f"https://stats-data.hyperliquid.xyz/{network}/leaderboard"

    @classmethod
    def from_env(cls) -> "Settings":
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
        )
