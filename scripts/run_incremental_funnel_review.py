#!/usr/bin/env python3
from __future__ import annotations

import os

from hlcopy.profitability import incremental_funnel_cli as funnel
from hlcopy.profitability.memory_bounded_wide import WideEventStream


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("Lane 1 funnel review refuses REAL_TRADING_ENABLED=YES")

    def memory_bounded_loader(enriched_dir, *, cutoff_ns):
        return WideEventStream(enriched_dir, cutoff_ns=cutoff_ns)

    funnel.load_wide_events = memory_bounded_loader
    funnel.main()


if __name__ == "__main__":
    main()
