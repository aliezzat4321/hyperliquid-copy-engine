from __future__ import annotations

from collections import defaultdict

from hlcopy.models import Fill
from hlcopy.positions.state_machine import InstrumentState, PositionEpisode


def reconstruct_positions(fills: list[Fill]) -> tuple[list[PositionEpisode], dict[str, InstrumentState]]:
    states: dict[str, InstrumentState] = {}
    completed: list[PositionEpisode] = []
    grouped: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        grouped[fill.coin].append(fill)

    for coin, coin_fills in grouped.items():
        state = InstrumentState(wallet_address=coin_fills[0].wallet_address, coin=coin)
        states[coin] = state
        for fill in sorted(coin_fills, key=lambda f: (f.timestamp_ms, f.tid)):
            completed.extend(state.apply(fill))
    return completed, states
