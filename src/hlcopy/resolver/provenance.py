from __future__ import annotations

import hashlib
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jsonable_config(config: Any) -> dict[str, object]:
    if not is_dataclass(config):
        raise TypeError("config must be a dataclass instance")
    raw = asdict(config)
    return {
        str(key): str(value) if isinstance(value, Decimal) else value
        for key, value in raw.items()
    }
