from __future__ import annotations

from hlcopy.market.symbols import wire_coin
from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


class Lane3CausalBookProvider(CausalParquetL2BookProvider):
    """Return only market state received by a frozen order-arrival boundary."""

    def at_or_before(self, coin: str, arrival_ms: float):
        # Tape partitions use exchange-facing symbols, including case-sensitive
        # multiplier assets and lowercase HIP-3 namespaces.
        return super().first_at_or_after(wire_coin(coin), arrival_ms)
