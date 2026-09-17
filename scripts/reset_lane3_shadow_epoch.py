#!/usr/bin/env python3
"""Reset contaminated Lane 3 derived shadow evidence without replaying old feeds.

The reset deliberately preserves source/discovery evidence and the notification
high-water marks used to prevent historical feed replay. It removes only
managed shadow exposure plus selector/performance artifacts that belong to a
superseded Lane 3 measurement epoch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc
CANONICAL_STATE_ROOT = Path(
    "/var/lib/hyperliquid-copy-engine/invo-notification-executor"
)
DEFAULT_ENV_FILE = Path(
    "/etc/hyperliquid-copy-engine/invo-notification-executor.env"
)
SELECTOR_VERSION = "invo-portfolio-hybrid-v3-20260916"
TOMBSTONE_NAME = "dataset-resets.jsonl"

# Raw/source discovery is intentionally retained. These hashes prove this reset
# does not rewrite those files.
PRESERVE_FILES = (
    "trader-population.json",
    "invo-leaderboards.json",
    "invo-leaderboard-snapshots.jsonl",
)

# These are derived from the flawed/superseded shadow measurement epoch and
# must not be mixed into the prospective v3 run.
DELETE_FILES = (
    "audit.jsonl",
    "portfolio-candidates.json",
    "portfolio-candidate-snapshots.jsonl",
    "elite-shadow-report.json",
    "elite-shadow-ledger.jsonl",
)

# Earlier deployment logic archived contaminated runtime state instead of
# deleting it. Remove only derived/shadow archives. Historical trader-population
# archives remain source discovery evidence and are deliberately preserved.
DELETE_ARCHIVE_PATTERNS = (
    "state.pre-*.json",
    "audit.pre-*.jsonl",
    "portfolio-candidates.pre-*.json",
    "portfolio-candidate-snapshots.pre-*.jsonl",
    "elite-shadow-report.pre-*.json",
    "elite-shadow-ledger.pre-*.jsonl",
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime) -> str:
    return (
        value.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked preserved file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def assert_shadow_only_env(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"missing or unsafe executor env file: {path}")
    values = read_env(path)
    if values.get("REAL_TRADING_ENABLED", "").upper() != "NO":
        raise RuntimeError("REAL_TRADING_ENABLED must be NO before Lane 3 reset")
    if values.get("NOTIFICATION_TRADER_LIVE", "").lower() != "false":
        raise RuntimeError("NOTIFICATION_TRADER_LIVE must be false before Lane 3 reset")


def _validated_state(state_path: Path) -> tuple[dict[str, Any], int, int, int]:
    if not state_path.exists():
        return {"seen": [], "managed": {}, "feedCursors": {}}, 0, 0, 0
    if state_path.is_symlink():
        raise RuntimeError(f"refusing symlinked state file: {state_path}")
    parsed = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Lane 3 state.json must contain an object")
    seen = parsed.get("seen", [])
    managed = parsed.get("managed", {})
    cursors = parsed.get("feedCursors", {})
    if not isinstance(seen, list):
        raise RuntimeError("Lane 3 state.json seen must be an array")
    if not isinstance(managed, dict):
        raise RuntimeError("Lane 3 state.json managed must be an object")
    if not isinstance(cursors, dict):
        raise RuntimeError("Lane 3 state.json feedCursors must be an object")
    return parsed, len(seen), len(cursors), len(managed)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.reset-tmp")
    temp.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _append_tombstone(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def reset_state_root(
    state_root: Path,
    *,
    epoch: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Perform the bounded data reset. Service stop/start is handled by deploy."""
    if not epoch.strip():
        raise RuntimeError("reset epoch must be non-empty")
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_root.is_symlink():
        raise RuntimeError(f"refusing symlinked state root: {state_root}")

    # Validate every mutable structure before deleting anything.
    state_path = state_root / "state.json"
    state, seen_count, cursor_count, managed_count = _validated_state(state_path)
    preserve_before = {
        name: sha256_file(state_root / name) for name in PRESERVE_FILES
    }

    # Keep dedupe/high-water marks so old Invo notifications cannot replay into
    # the clean run. Only simulated managed exposure is invalidated.
    state["managed"] = {}
    _atomic_json_write(state_path, state)

    deleted: list[dict[str, Any]] = []
    for name in DELETE_FILES:
        path = state_root / name
        if not path.exists() and not path.is_symlink():
            continue
        size = path.lstat().st_size
        path.unlink()
        deleted.append({"path": name, "bytes": size})

    archived: list[dict[str, Any]] = []
    archive_paths: set[Path] = set()
    for pattern in DELETE_ARCHIVE_PATTERNS:
        archive_paths.update(state_root.glob(pattern))
    for path in sorted(archive_paths):
        if not path.is_file() and not path.is_symlink():
            raise RuntimeError(f"refusing non-file archive path: {path}")
        size = path.lstat().st_size
        path.unlink()
        archived.append({"path": path.name, "bytes": size})

    preserve_after = {
        name: sha256_file(state_root / name) for name in PRESERVE_FILES
    }
    if preserve_before != preserve_after:
        raise RuntimeError("source/discovery files changed during Lane 3 reset")

    record = {
        "at": iso(now or utcnow()),
        "epoch": epoch,
        "selectorVersion": SELECTOR_VERSION,
        "reason": "discard_superseded_lane3_shadow_measurement",
        "managedPositionsRemoved": managed_count,
        "seenKeysPreserved": seen_count,
        "feedCursorsPreserved": cursor_count,
        "derivedFilesDeleted": deleted,
        "obsoleteArchivesDeleted": archived,
        "preservedSourceHashes": preserve_after,
        "realTradingEnabled": False,
        "polymarketTouched": False,
    }
    _append_tombstone(state_root / TOMBSTONE_NAME, record)
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, default=CANONICAL_STATE_ROOT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--epoch", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Lane 3 shadow reset requires root")
    if args.state_root != CANONICAL_STATE_ROOT:
        raise SystemExit(
            f"refusing non-canonical Lane 3 state root: {args.state_root}"
        )
    assert_shadow_only_env(args.env_file)
    result = reset_state_root(args.state_root, epoch=args.epoch)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
