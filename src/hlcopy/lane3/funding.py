from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.continuous_path_v2 import FundingRate
from hlcopy.profitability.path_inputs import load_funding_history_jsonl

from .reconstruction import ReconstructedPosition

D = Decimal
HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class FundingAttribution:
    funding_usd: D | None
    measured: bool
    missing_stamps_ms: tuple[int, ...]


def attribute_funding(
    position: ReconstructedPosition,
    rates: tuple[FundingRate, ...],
    *,
    end_ms: int,
) -> FundingAttribution:
    start = position.entry_legs[0].timestamp_ms
    by_stamp = {
        rate.payment_ts_ms: rate.funding_rate for rate in rates if rate.coin == position.coin
    }
    first_hour = ((start // HOUR_MS) + 1) * HOUR_MS
    expected = tuple(range(first_hour, end_ms + 1, HOUR_MS))
    missing = tuple(stamp for stamp in expected if stamp not in by_stamp)
    if missing:
        return FundingAttribution(None, False, missing)
    total = D("0")
    sign = D("-1") if position.side.lower() == "long" else D("1")
    for stamp in expected:
        notional = sum(
            (leg.notional for leg in position.entry_legs if leg.timestamp_ms <= stamp), D("0")
        )
        total += by_stamp[stamp] * notional * sign
    return FundingAttribution(total, True, ())


def load_cached_funding(
    path: Path, positions: list[ReconstructedPosition]
) -> tuple[FundingRate, ...]:
    if not path.exists():
        return ()
    return load_funding_history_jsonl(path, coins={position.coin for position in positions})


def append_funding_pages(path: Path, coin: str, pages: list[object]) -> None:
    """Append public-client responses without inventing or normalizing missing stamps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for page in pages:
            payload = getattr(page, "response_payload", page)
            rows = payload if isinstance(payload, list) else []
            handle.write(json.dumps({"coin": coin, "rows": rows}, sort_keys=True) + "\n")
