from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

SUPPORTED_ADAPTERS = {"invo_closed_trades_csv", "generic_closed_trades_csv"}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ExternalSourceSpec:
    id: str
    label: str
    adapter: str
    evidence_path: str
    enabled: bool = True
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.id or any(char.isspace() for char in self.id):
            raise ValueError("external source id must be a non-empty slug without whitespace")
        if self.adapter not in SUPPORTED_ADAPTERS:
            raise ValueError(f"unsupported external source adapter: {self.adapter}")
        if not self.evidence_path:
            raise ValueError("evidence_path is required")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, object]) -> ExternalSourceSpec:
        return cls(
            id=str(row["id"]),
            label=str(row.get("label", row["id"])),
            adapter=str(row["adapter"]),
            evidence_path=str(row["evidence_path"]),
            enabled=bool(row.get("enabled", True)),
            notes=str(row.get("notes", "")),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )


class ExternalSourceRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        if not self.path.exists():
            self._save(())

    def load(self) -> tuple[ExternalSourceSpec, ...]:
        if not self.path.exists():
            return ()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("unsupported external source registry version")
        sources = tuple(ExternalSourceSpec.from_dict(row) for row in payload.get("sources", []))
        ids = [source.id for source in sources]
        if len(ids) != len(set(ids)):
            raise ValueError("external source registry contains duplicate ids")
        return sources

    def get(self, source_id: str) -> ExternalSourceSpec:
        for source in self.load():
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def add(self, source: ExternalSourceSpec) -> ExternalSourceSpec:
        sources = list(self.load())
        if any(existing.id == source.id for existing in sources):
            raise ValueError(f"external source already exists: {source.id}")
        now = _now()
        stored = ExternalSourceSpec(
            id=source.id,
            label=source.label,
            adapter=source.adapter,
            evidence_path=source.evidence_path,
            enabled=source.enabled,
            notes=source.notes,
            created_at=source.created_at or now,
            updated_at=now,
        )
        sources.append(stored)
        self._save(sources)
        return stored

    def _save(self, sources: tuple[ExternalSourceSpec, ...] | list[ExternalSourceSpec]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now(),
            "sources": [source.to_dict() for source in sources],
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
