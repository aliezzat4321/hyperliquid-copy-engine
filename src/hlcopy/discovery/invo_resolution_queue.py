from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

MIN_RESOLUTION_TRADES = 12
RESOLVER_FIELDS = (
    "trade_id",
    "username",
    "ticker",
    "direction",
    "leverage",
    "entry_price",
    "closing_price",
    "opened_at",
    "closed_at",
    "portfolio_id",
    "source_post_id",
)


def read_evidence_ndjson(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _group_evidence(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    deduped: dict[tuple[str, str], dict[str, object]] = {}
    for source in rows:
        portfolio_id = str(source.get("portfolio_id") or "").strip()
        trade_id = str(source.get("trade_id") or "").strip()
        if not portfolio_id or not trade_id:
            continue
        row = {field: source.get(field, "") for field in RESOLVER_FIELDS}
        key = (portfolio_id, trade_id)
        previous = deduped.get(key)
        if previous is None or int(row.get("closed_at") or 0) >= int(
            previous.get("closed_at") or 0
        ):
            deduped[key] = row

    grouped: dict[str, list[dict[str, object]]] = {}
    for (portfolio_id, _), row in deduped.items():
        grouped.setdefault(portfolio_id, []).append(row)
    for portfolio_rows in grouped.values():
        portfolio_rows.sort(key=lambda row: int(row.get("closed_at") or 0))
    return grouped


def materialize_resolution_queue(
    *,
    evidence_path: Path,
    output_dir: Path,
    portfolios: Sequence[Mapping[str, object]],
    min_trades: int = MIN_RESOLUTION_TRADES,
) -> dict[str, object]:
    grouped = _group_evidence(read_evidence_ndjson(evidence_path))
    metadata = {
        str(row.get("portfolio_id") or ""): row
        for row in portfolios
        if str(row.get("portfolio_id") or "").strip()
    }
    output_dir.mkdir(parents=True, exist_ok=True)

    queue: list[dict[str, object]] = []
    for portfolio_id, rows in grouped.items():
        if len(rows) < max(3, min_trades):
            continue
        digest = hashlib.sha256(portfolio_id.encode()).hexdigest()[:16]
        csv_path = output_dir / f"portfolio_{digest}.csv"
        temporary = csv_path.with_suffix(".csv.tmp")
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(RESOLVER_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(csv_path)

        meta = metadata.get(portfolio_id, {})
        coins = sorted({str(row.get("ticker")) for row in rows if row.get("ticker")})
        queue.append(
            {
                "portfolio_id": portfolio_id,
                "username": meta.get("username") or rows[-1].get("username") or "unknown",
                "portfolio_name": meta.get("name"),
                "evidence_count": len(rows),
                "distinct_coins": coins,
                "distinct_coin_count": len(coins),
                "resolver_csv": str(csv_path),
                "status": "READY_FOR_WALLET_RESOLUTION",
            }
        )

    queue.sort(
        key=lambda row: (int(row["distinct_coin_count"]), int(row["evidence_count"])),
        reverse=True,
    )
    payload: dict[str, object] = {
        "source": "invo",
        "minimum_evidence_trades": max(3, min_trades),
        "ready_count": len(queue),
        "queue": queue,
    }
    path = output_dir / "resolution_queue.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return payload
