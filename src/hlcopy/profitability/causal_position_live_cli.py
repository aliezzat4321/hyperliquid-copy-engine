from __future__ import annotations

from hlcopy.profitability import position_live_cli
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


def main() -> None:
    # The position simulator's target timestamp is the follower's simulated local
    # send/arrival clock. Use only L2 state already received by that time.
    position_live_cli.ParquetL2BookProvider = CausalParquetL2BookProvider
    position_live_cli.main()


if __name__ == "__main__":
    main()
