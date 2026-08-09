from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from hlcopy.models import Fill
from hlcopy.positions.state_machine import (
    InstrumentState,
    PositionEpisode,
    PositionReconstructionError,
)


def _order_timestamp_group(
    fills: list[Fill],
    *,
    expected_start: Decimal | None,
) -> list[Fill]:
    """Order fills sharing one exchange timestamp by their position-state transitions.

    Hyperliquid gives each fill an exact ``startPosition``. Multiple fills for the same
    instrument can share the same millisecond timestamp, and ``tid`` is not a safe
    substitute for execution order. Treat each fill as a directed edge from
    ``startPosition`` to ``startPosition + signed_size`` and reconstruct the only state-
    valid trail. If the available fills cannot form one trail, fail closed.
    """
    if len(fills) <= 1:
        return fills

    adjacency: dict[Decimal, list[tuple[int, Fill, Decimal]]] = defaultdict(list)
    indegree: Counter[Decimal] = Counter()
    outdegree: Counter[Decimal] = Counter()

    for fill in fills:
        start = fill.start_position
        end = start + fill.signed_size
        adjacency[start].append((fill.tid, fill, end))
        outdegree[start] += 1
        indegree[end] += 1

    # Pop lower tids first only as a deterministic tie-break among multiple valid edges.
    # State continuity, not tid, determines whether the final sequence is admissible.
    for edges in adjacency.values():
        edges.sort(key=lambda item: item[0], reverse=True)

    vertices = set(indegree) | set(outdegree)
    if expected_start is not None:
        start_vertex = expected_start
    else:
        heads = [vertex for vertex in vertices if outdegree[vertex] - indegree[vertex] == 1]
        tails = [vertex for vertex in vertices if indegree[vertex] - outdegree[vertex] == 1]
        invalid_degree = [
            vertex
            for vertex in vertices
            if abs(outdegree[vertex] - indegree[vertex]) > 1
        ]
        if invalid_degree or len(heads) > 1 or len(tails) > 1 or len(heads) != len(tails):
            timestamp = fills[0].timestamp_ms
            raise PositionReconstructionError(
                f"{fills[0].wallet_address} {fills[0].coin} time={timestamp}: "
                "same-timestamp fills do not form one position-state trail"
            )
        if heads:
            start_vertex = heads[0]
        else:
            # Euler circuit: any vertex on the circuit is state-valid. Use the start
            # position of the lowest-tid fill only to keep reconstruction deterministic.
            start_vertex = min(fills, key=lambda fill: fill.tid).start_position

    stack: list[tuple[Decimal, Fill | None]] = [(start_vertex, None)]
    reverse_path: list[Fill] = []

    while stack:
        vertex, _incoming = stack[-1]
        edges = adjacency.get(vertex)
        if edges:
            _tid, fill, end = edges.pop()
            stack.append((end, fill))
            continue

        _vertex, incoming = stack.pop()
        if incoming is not None:
            reverse_path.append(incoming)

    ordered = list(reversed(reverse_path))
    if len(ordered) != len(fills):
        timestamp = fills[0].timestamp_ms
        raise PositionReconstructionError(
            f"{fills[0].wallet_address} {fills[0].coin} time={timestamp}: "
            "same-timestamp fills are disconnected or incomplete"
        )

    qty = start_vertex
    for fill in ordered:
        if fill.start_position != qty:
            timestamp = fills[0].timestamp_ms
            raise PositionReconstructionError(
                f"{fills[0].wallet_address} {fills[0].coin} time={timestamp}: "
                "unable to derive a state-continuous same-timestamp fill order"
            )
        qty += fill.signed_size

    return ordered


def reconstruct_positions(
    fills: list[Fill],
) -> tuple[list[PositionEpisode], dict[str, InstrumentState]]:
    states: dict[str, InstrumentState] = {}
    completed: list[PositionEpisode] = []
    grouped: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        grouped[fill.coin].append(fill)

    for coin, coin_fills in grouped.items():
        state = InstrumentState(wallet_address=coin_fills[0].wallet_address, coin=coin)
        states[coin] = state

        by_timestamp: dict[int, list[Fill]] = defaultdict(list)
        for fill in coin_fills:
            by_timestamp[fill.timestamp_ms].append(fill)

        first_timestamp = True
        for timestamp in sorted(by_timestamp):
            timestamp_fills = by_timestamp[timestamp]
            ordered = _order_timestamp_group(
                timestamp_fills,
                expected_start=None if first_timestamp else state.qty,
            )
            first_timestamp = False
            for fill in ordered:
                completed.extend(state.apply(fill))

    return completed, states
