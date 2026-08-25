from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hlcopy.discovery.invo_resolution_queue import (
    MIN_RESOLUTION_TRADES,
    materialize_resolution_queue,
)

BASE_TIME = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _row(index: int) -> dict[str, object]:
    opened = BASE_TIME + timedelta(hours=index)
    closed = opened + timedelta(minutes=15)
    return {
        "trade_id": f"trade-{index}",
        "username": "carmine",
        "ticker": "BTC" if index % 2 else "ETH",
        "direction": "LONG" if index % 2 else "SHORT",
        "leverage": 5,
        "entry_price": 100 + index,
        "closing_price": 101 + index,
        "entry_size": 10,
        "opened_at": opened.isoformat().replace("+00:00", "Z"),
        "closed_at": closed.isoformat().replace("+00:00", "Z"),
        "portfolio_id": "portfolio-carmine",
        "source_post_id": f"post-{index}",
    }


def _materialize(tmp_path: Path, count: int) -> dict[str, object]:
    evidence_path = tmp_path / f"closed-{count}.ndjson"
    evidence_path.write_text(
        "".join(json.dumps(_row(index)) + "\n" for index in range(count)),
        encoding="utf-8",
    )
    return materialize_resolution_queue(
        evidence_path=evidence_path,
        output_dir=tmp_path / f"queue-{count}",
        portfolios=[],
    )


def test_invo_default_queue_matches_size_agnostic_v3_threshold(tmp_path: Path) -> None:
    assert MIN_RESOLUTION_TRADES == 20
    assert _materialize(tmp_path, 19)["ready_count"] == 0
    ready = _materialize(tmp_path, 20)
    assert ready["minimum_evidence_trades"] == 20
    assert ready["ready_count"] == 1
    assert ready["queue"][0]["evidence_count"] == 20
