from __future__ import annotations

from pathlib import Path

from hlcopy.profitability import position_live_cli
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


_PROVIDER_CACHE: dict[Path, CausalParquetL2BookProvider] = {}


def _shared_causal_provider(market_dir: Path) -> CausalParquetL2BookProvider:
    """Reuse one in-memory L2 index when direct and wide lanes share the same tape."""
    key = market_dir.resolve()
    provider = _PROVIDER_CACHE.get(key)
    if provider is None:
        provider = CausalParquetL2BookProvider(key)
        _PROVIDER_CACHE[key] = provider
    return provider


def main() -> None:
    # The position simulator's target timestamp is the follower's simulated local
    # send/arrival clock. Use only L2 state already received by that time. Both live
    # lanes point at market-shadow, so sharing this provider avoids loading/parsing the
    # same Parquet tape twice and roughly halves the scorer's market-data memory.
    position_live_cli.ParquetL2BookProvider = _shared_causal_provider
    position_live_cli.main()


if __name__ == "__main__":
    main()
