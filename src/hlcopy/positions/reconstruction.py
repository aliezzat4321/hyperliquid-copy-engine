from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from hlcopy.models import Fill
from hlcopy.positions.state_machine import (
    InstrumentState,
    PositionEpisode,
    PositionReconstructionError,
    normalize_position,
    positions_match,
)


def _order_timestamp_group(
    fills: list[Fill],
    *,
    expected_start: Decimal | None,
) -> list[Fill]:
    """Order fills sharing one exchange timestamp by their position-state transitions.

    Hyperliquid gives each fill a ``startPosition``. Multiple fills for the same
    instrument can share the same millisecond timestamp, and ``tid`` is not a safe
    substitute for execution order. Treat each fill as a directed edge from
    ``startPosition`` to ``startPosition + signed_size`` and reconstruct a state-valid
    trail. Position vertices are canonicalized at sub-nanounit precision so harmless
    API decimal artifacts do not split an otherwise valid trail.
    """
    if len(fills) <= 1:
        return fills

    adjacency: dict[Decimal, list[tuple[int, Fill, Decimal]]] = defaultdict(list)
    indegree: Counter[Decimal] = Counter()
    outdegree: Counter[Decimal] = Counter()
    source_order = {id(fill): index for index, fill in enumerate(fills)}

    for fill in fills:
        start = normalize_position(fill.start_position)
        end = normalize_position(fill.start_position + fill.signed_size)
        adjacency[start].append((source_order[id(fill)], fill, end))
        outdegree[start] += 1
        indegree[end] += 1

    # Pop source-earlier edges first among multiple state-valid choices.
    for edges in adjacency.values():
        edges.sort(key=lambda item: item[0], reverse=True)

    vertices = set(indegree) | set(outdegree)
    if expected_start is not None:
        start_vertex = normalize_position(expected_start)
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
            # Closed circuit at the first visible timestamp: state continuity cannot
            # identify a unique rotation, so preserve the exchange response ordering.
            start_vertex = normalize_position(fills[0].start_position)

    stack: list[tuple[Decimal, Fill | None]] = [(start_vertex, None)]
    reverse_path: list[Fill] = []

    while stack:
        vertex, _incoming = stack[-1]
        edges = adjacency.get(vertex)
        if edges:
            _source_index, fill, end = edges.pop()
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
        if not positions_match(fill.start_position, qty):
            timestamp = fills[0].timestamp_ms
            raise PositionReconstructionError(
                f"{fills[0].wallet_address} {fills[0].coin} time={timestamp}: "
                "unable to derive a state-continuous same-timestamp fill order"
            )
        qty = normalize_position(fill.start_position + fill.signed_size)

    return ordered


def order_fills_by_position_state(fills: list[Fill]) -> list[Fill]:
    """Return fills in state-valid per-instrument order for analytics consumers."""
    grouped: dict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        grouped[fill.coin].append(fill)

    ordered_all: list[Fill] = []
    for coin_fills in grouped.values():
        by_timestamp: dict[int, list[Fill]] = defaultdict(list)
        for fill in coin_fills:
            by_timestamp[fill.timestamp_ms].append(fill)

        expected_start: Decimal | None = None
        for timestamp in sorted(by_timestamp):
            ordered = _order_timestamp_group(
                by_timestamp[timestamp],
                expected_start=expected_start,
            )
            ordered_all.extend(ordered)
            if ordered:
                last = ordered[-1]
                expected_start = normalize_position(last.start_position + last.signed_size)

    return ordered_all


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
