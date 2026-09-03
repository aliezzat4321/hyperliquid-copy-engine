from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.discovery.invo_store import InvoRecordStore

MIN_RESOLUTION_TRADES = 20
RESOLVER_FIELDS = (
    "trade_id",
    "username",
    "ticker",
    "direction",
    "leverage",
    "entry_price",
    "closing_price",
    "entry_size",
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
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"corrupt Invo evidence at {path}:{line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"non-object Invo evidence at {path}:{line_number}"
                )
            rows.append(value)
    return rows


def _timestamp_ms(value: object, *, field: str) -> int:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Invo evidence is missing {field}")
    if text.isdigit():
        numeric = int(text)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invo evidence has invalid {field}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _closed_at(row: Mapping[str, object]) -> int:
    return _timestamp_ms(row.get("closed_at"), field="closed_at")


def _validate_resolver_row(row: Mapping[str, object]) -> None:
    required = (
        "trade_id",
        "ticker",
        "direction",
        "leverage",
        "entry_price",
        "closing_price",
        "entry_size",
        "opened_at",
        "closed_at",
        "portfolio_id",
    )
    missing = [field for field in required if str(row.get(field) or "").strip() == ""]
    if missing:
        raise ValueError("Invo evidence is missing resolver fields: " + ", ".join(missing))
    opened_at = _timestamp_ms(row.get("opened_at"), field="opened_at")
    closed_at = _timestamp_ms(row.get("closed_at"), field="closed_at")
    if closed_at <= opened_at:
        raise ValueError("Invo evidence close must be after open")


def _group_evidence(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, object]]]:
    parents: dict[tuple[str, str], tuple[str, str]] = {}

    def find(value: tuple[str, str]) -> tuple[str, str]:
        parent = parents.setdefault(value, value)
        if parent != value:
            parents[value] = find(parent)
        return parents[value]

    def union(left: tuple[str, str], right: tuple[str, str]) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        canonical, alias = sorted((left_root, right_root), key=lambda item: item[1])
        parents[alias] = canonical

    for source in rows:
        portfolio_id = str(source.get("portfolio_id") or "").strip()
        trade_id = str(source.get("trade_id") or "").strip()
        if not portfolio_id or not trade_id:
            raise ValueError("Invo evidence is missing portfolio_id or trade_id")
        aliases_raw = source.get("trade_alias_ids")
        aliases = (
            [str(value).strip() for value in aliases_raw]
            if isinstance(aliases_raw, Sequence) and not isinstance(aliases_raw, (str, bytes))
            else []
        )
        identities = [trade_id, *(value for value in aliases if value)]
        anchor = (portfolio_id, trade_id)
        find(anchor)
        for alias in identities:
            union(anchor, (portfolio_id, alias))

    deduped: dict[tuple[str, str], dict[str, object]] = {}
    for source in rows:
        portfolio_id = str(source.get("portfolio_id") or "").strip()
        trade_id = str(source.get("trade_id") or "").strip()
        if not portfolio_id or not trade_id:
            raise ValueError("Invo evidence is missing portfolio_id or trade_id")
        canonical_trade_id = find((portfolio_id, trade_id))[1]
        row = {field: source.get(field, "") for field in RESOLVER_FIELDS}
        row["trade_id"] = canonical_trade_id
        _validate_resolver_row(row)
        key = (portfolio_id, canonical_trade_id)
        previous = deduped.get(key)
        if previous is None or _closed_at(row) >= _closed_at(previous):
            deduped[key] = row

    grouped: dict[str, list[dict[str, object]]] = {}
    for (portfolio_id, _), row in deduped.items():
        grouped.setdefault(portfolio_id, []).append(row)
    for portfolio_rows in grouped.values():
        portfolio_rows.sort(key=_closed_at)
    return grouped


def materialize_resolution_queue(
    *,
    evidence_path: Path,
    output_dir: Path,
    portfolios: Sequence[Mapping[str, object]],
    min_trades: int = MIN_RESOLUTION_TRADES,
) -> dict[str, object]:
    grouped = _group_evidence(read_evidence_ndjson(evidence_path))
    return _materialize_grouped(
        grouped.items(),
        output_dir=output_dir,
        portfolios=portfolios,
        min_trades=min_trades,
    )


def materialize_resolution_queue_from_store(
    *,
    store: InvoRecordStore,
    output_dir: Path,
    portfolios: Sequence[Mapping[str, object]],
    min_trades: int = MIN_RESOLUTION_TRADES,
) -> dict[str, object]:
    def grouped_rows():
        for portfolio_id, rows in store.evidence_groups():
            grouped = _group_evidence(rows)
            if portfolio_id in grouped:
                yield portfolio_id, grouped[portfolio_id]

    return _materialize_grouped(
        grouped_rows(),
        output_dir=output_dir,
        portfolios=portfolios,
        min_trades=min_trades,
    )


def _materialize_grouped(
    grouped: Iterable[tuple[str, list[dict[str, object]]]],
    *,
    output_dir: Path,
    portfolios: Sequence[Mapping[str, object]],
    min_trades: int,
) -> dict[str, object]:
    generated_at = datetime.now(tz=UTC).isoformat()
    queue_path = output_dir / "resolution_queue.json"
    previous_ready_at: dict[str, str] = {}
    if queue_path.exists():
        try:
            previous = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        previous_rows = previous.get("queue") if isinstance(previous, Mapping) else None
        if isinstance(previous_rows, list):
            for previous_row in previous_rows:
                if not isinstance(previous_row, Mapping):
                    continue
                previous_id = str(previous_row.get("portfolio_id") or "").strip()
                ready_at = str(previous_row.get("resolution_ready_at") or "").strip()
                if previous_id and ready_at:
                    previous_ready_at[previous_id] = ready_at
    metadata = {
        str(row.get("portfolio_id") or ""): row
        for row in portfolios
        if str(row.get("portfolio_id") or "").strip()
    }
    output_dir.mkdir(parents=True, exist_ok=True)

    queue: list[dict[str, object]] = []
    for portfolio_id, rows in grouped:
        if len(rows) < max(3, min_trades):
            continue
        digest = hashlib.sha256(portfolio_id.encode("utf-8")).hexdigest()[:16]
        csv_path = output_dir / f"portfolio_{digest}.csv"
        temporary = csv_path.with_suffix(".csv.tmp")
        with temporary.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(RESOLVER_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(csv_path)

        meta = metadata.get(portfolio_id, {})
        coins = sorted(
            {str(row.get("ticker")) for row in rows if row.get("ticker")}
        )
        username = meta.get("username") or rows[-1].get("username") or "unknown"
        queue.append(
            {
                "portfolio_id": portfolio_id,
                "username": username,
                "portfolio_name": meta.get("name"),
                "evidence_count": len(rows),
                "distinct_coins": coins,
                "distinct_coin_count": len(coins),
                "resolver_csv": str(csv_path),
                "status": "READY_FOR_WALLET_RESOLUTION",
                "resolution_ready_at": previous_ready_at.get(
                    portfolio_id, generated_at
                ),
            }
        )

    queue.sort(
        key=lambda row: (
            int(row["distinct_coin_count"]),
            int(row["evidence_count"]),
        ),
        reverse=True,
    )
    payload: dict[str, object] = {
        "source": "invo",
        "minimum_evidence_trades": max(3, min_trades),
        "ready_count": len(queue),
        "generated_at": generated_at,
        "queue": queue,
    }
    path = queue_path
    temporary = path.with_suffix(".json.tmp")
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)
    return payload
