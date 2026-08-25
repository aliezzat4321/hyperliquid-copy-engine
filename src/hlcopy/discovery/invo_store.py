from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any


class InvoRecordStore:
    """Crash-safe, indexed archive for the unbounded Invo event history."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                stream TEXT NOT NULL,
                record_key TEXT NOT NULL,
                portfolio_id TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                PRIMARY KEY (stream, record_key)
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS records_stream_portfolio
            ON records (stream, portfolio_id)
            """
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> InvoRecordStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert(
        self,
        stream: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_field: str,
    ) -> int:
        inserted = 0
        with self.connection:
            for source in rows:
                row = dict(source)
                record_key = str(row.get(key_field) or "").strip()
                if not record_key:
                    raise ValueError(f"new Invo {stream} row is missing {key_field}")
                portfolio_id = str(row.get("portfolio_id") or "").strip()
                payload = json.dumps(row, separators=(",", ":"), sort_keys=True)
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO records
                        (stream, record_key, portfolio_id, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (stream, record_key, portfolio_id, payload),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    self.connection.execute(
                        """
                        UPDATE records
                        SET portfolio_id = ?, payload = ?
                        WHERE stream = ? AND record_key = ?
                        """,
                        (portfolio_id, payload, stream, record_key),
                    )
        return inserted

    def evidence_groups(self) -> Iterator[tuple[str, list[dict[str, object]]]]:
        portfolio_rows = self.connection.execute(
            """
            SELECT DISTINCT portfolio_id
            FROM records
            WHERE stream = 'evidence' AND portfolio_id <> ''
            ORDER BY portfolio_id
            """
        ).fetchall()
        for (portfolio_id,) in portfolio_rows:
            records = self.connection.execute(
                """
                SELECT payload
                FROM records
                WHERE stream = 'evidence' AND portfolio_id = ?
                ORDER BY record_key
                """,
                (portfolio_id,),
            )
            rows: list[dict[str, object]] = []
            for (payload,) in records:
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ValueError("corrupt Invo record-store payload") from exc
                if not isinstance(value, dict):
                    raise ValueError("non-object Invo record-store payload")
                rows.append(value)
            yield str(portfolio_id), rows


def read_legacy_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt Invo NDJSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"non-object Invo NDJSON at {path}:{line_number}")
            rows.append(value)
    return rows
