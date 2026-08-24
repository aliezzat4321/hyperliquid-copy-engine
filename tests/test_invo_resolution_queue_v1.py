from __future__ import annotations

import json
from pathlib import Path

from hlcopy.discovery.invo_resolution_queue import materialize_resolution_queue


def _row(index: int, *, post_id: str | None = None) -> dict[str, object]:
    return {
        "trade_id": f"trade-{index}",
        "username": "carmine",
        "ticker": "HYPE" if index % 2 else "SOL",
        "direction": "LONG" if index % 2 else "SHORT",
        "leverage": 5,
        "entry_price": 100 + index,
        "closing_price": 101 + index,
        "opened_at": 1_780_000_000_000 + index * 10_000,
        "closed_at": 1_780_000_005_000 + index * 10_000,
        "portfolio_id": "portfolio-carmine",
        "source_post_id": post_id or f"post-{index}",
    }


def test_resolution_queue_requires_independent_trades_and_deduplicates_trade_id(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "closed.ndjson"
    rows = [_row(index) for index in range(12)]
    duplicate = _row(3, post_id="duplicate-post")
    duplicate["closed_at"] = int(duplicate["closed_at"]) + 1_000
    rows.append(duplicate)
    evidence_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = materialize_resolution_queue(
        evidence_path=evidence_path,
        output_dir=tmp_path / "queue",
        portfolios=[
            {
                "portfolio_id": "portfolio-carmine",
                "username": "carmine",
                "name": "10k-300k(OPEN)",
            }
        ],
        min_trades=12,
    )

    assert result["ready_count"] == 1
    item = result["queue"][0]
    assert item["username"] == "carmine"
    assert item["evidence_count"] == 12
    assert item["distinct_coin_count"] == 2
    csv_path = Path(str(item["resolver_csv"]))
    assert csv_path.exists()
    assert len(csv_path.read_text(encoding="utf-8").splitlines()) == 13


def test_resolution_queue_waits_for_minimum_evidence(tmp_path: Path) -> None:
    evidence_path = tmp_path / "closed.ndjson"
    evidence_path.write_text(
        "".join(json.dumps(_row(index)) + "\n" for index in range(11)),
        encoding="utf-8",
    )

    result = materialize_resolution_queue(
        evidence_path=evidence_path,
        output_dir=tmp_path / "queue",
        portfolios=[],
        min_trades=12,
    )

    assert result["ready_count"] == 0
