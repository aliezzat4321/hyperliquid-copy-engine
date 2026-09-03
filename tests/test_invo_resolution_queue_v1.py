from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hlcopy.discovery.invo_resolution_queue import materialize_resolution_queue
from hlcopy.signals.generic_csv import load_generic_closed_trades
from hlcopy.signals.invo import load_invo_closed_trades

BASE_TIME = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row(index: int, *, post_id: str | None = None) -> dict[str, object]:
    opened = BASE_TIME + timedelta(minutes=index * 10)
    closed = opened + timedelta(minutes=5)
    return {
        "trade_id": f"trade-{index}",
        "username": "carmine",
        "ticker": "HYPE" if index % 2 else "SOL",
        "direction": "LONG" if index % 2 else "SHORT",
        "leverage": 5,
        "entry_price": 100 + index,
        "closing_price": 101 + index,
        "entry_size": 1.0,
        "opened_at": _iso(opened),
        "closed_at": _iso(closed),
        "portfolio_id": "portfolio-carmine",
        "source_post_id": post_id or f"post-{index}",
    }


def test_resolution_queue_requires_independent_trades_and_deduplicates_trade_id(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "closed.ndjson"
    rows = [_row(index) for index in range(12)]
    duplicate = _row(3, post_id="duplicate-post")
    duplicate["closed_at"] = _iso(BASE_TIME + timedelta(minutes=36))
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
    assert result["generated_at"]
    item = result["queue"][0]
    assert item["resolution_ready_at"] == result["generated_at"]
    assert item["username"] == "carmine"
    assert item["evidence_count"] == 12
    assert item["distinct_coin_count"] == 2
    csv_path = Path(str(item["resolver_csv"]))
    assert csv_path.exists()
    assert len(csv_path.read_text(encoding="utf-8").splitlines()) == 13

    imported = load_invo_closed_trades(csv_path)
    assert len(imported.signals) == 12
    assert imported.rejected_rows == ()
    generic = load_generic_closed_trades(csv_path)
    assert len(generic.signals) == 12
    assert generic.rejected_rows == ()

    queue_path = tmp_path / "queue" / "resolution_queue.json"
    persisted = json.loads(queue_path.read_text(encoding="utf-8"))
    persisted["queue"][0]["resolution_ready_at"] = "2026-08-01T00:00:00+00:00"
    queue_path.write_text(json.dumps(persisted), encoding="utf-8")

    rematerialized = materialize_resolution_queue(
        evidence_path=evidence_path,
        output_dir=tmp_path / "queue",
        portfolios=[],
        min_trades=12,
    )

    assert rematerialized["queue"][0]["resolution_ready_at"] == (
        "2026-08-01T00:00:00+00:00"
    )


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


def test_resolution_queue_canonicalizes_base_id_aliases(tmp_path: Path) -> None:
    evidence_path = tmp_path / "closed.ndjson"
    rows = [_row(index) for index in range(11)]
    aliased = _row(11)
    aliased["trade_id"] = "base-long"
    aliased["trade_alias_ids"] = ["base-long", "base-short"]
    duplicate_alias = _row(12)
    duplicate_alias["trade_id"] = "base-short"
    duplicate_alias["trade_alias_ids"] = ["base-short"]
    rows.extend((aliased, duplicate_alias))
    evidence_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    result = materialize_resolution_queue(
        evidence_path=evidence_path,
        output_dir=tmp_path / "queue",
        portfolios=[],
        min_trades=12,
    )

    assert result["ready_count"] == 1
    assert result["queue"][0]["evidence_count"] == 12
