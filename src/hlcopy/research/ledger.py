from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hlcopy.shadow.manifest import fingerprint


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    observed_at_ns: int
    candidate_address: str
    rank: int
    composite_score: float
    style: str
    warning_flags: str
    source_snapshot_ms: int | None
    screened_count: int | None
    shortlisted_count: int | None
    ranked_count: int | None
    source_artifact: str
    artifact_fingerprint: str
    raw_metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def append_observation(path: Path, observation: CandidateObservation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(observation.to_dict(), separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def artifact_fingerprint(rows: list[dict[str, Any]]) -> str:
    return fingerprint(rows)


def now_ns() -> int:
    return time.time_ns()
