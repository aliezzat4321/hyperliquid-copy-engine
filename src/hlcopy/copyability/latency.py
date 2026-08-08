from __future__ import annotations

DEFAULT_LATENCY_SCENARIOS_MS: tuple[int, ...] = (
    0,
    100,
    250,
    500,
    1_000,
    2_000,
    3_000,
    5_000,
    10_000,
    30_000,
)


def signal_age_ms(event_timestamp_ms: int, received_timestamp_ms: int) -> int:
    return max(0, received_timestamp_ms - event_timestamp_ms)
