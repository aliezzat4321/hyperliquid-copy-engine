#!/usr/bin/env python3
"""Reviewed, lossless compaction lifecycle for historical Hyperliquid tape."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from hlcopy.storage.metrics import disk_usage

MODE = "DRY_RUN_ONLY_NO_MUTATION"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _record(directory: Path, commit: dict[str, Any], final: Path) -> None:
    sidecar = directory / "_lifecycle.json"
    previous = json.loads(sidecar.read_text()) if sidecar.exists() else {
        "format_version": 1, "groups": []}
    previous["groups"].append({
        **commit,
        "bytes_before": sum(x["bytes"] for x in commit["sources"]),
        "bytes_after": final.stat().st_size,
        "observed_at": datetime.now(UTC).isoformat(),
    })
    _write(sidecar, previous)


def _safe(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    valid_layout = (
        len(relative.parts) == 3
        and relative.parts[0].startswith("date=")
        and relative.parts[1].startswith("coin=")
        and relative.parts[2].startswith("channel=")
    )
    if not valid_layout:
        raise ValueError(f"unsafe lifecycle partition: {path}")
    cursor = root
    if cursor.is_symlink():
        raise ValueError("market root may not be a symlink")
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"symlinked lifecycle path: {cursor}")


def build_plan(
    root: Path, policy: dict[str, Any], output: Path, *, today: date
) -> dict[str, Any]:
    recent = int(policy.get("recent_days", 0))
    if recent < 3:
        raise ValueError("recent_days must be at least 3")
    cutoff = today - timedelta(days=recent - 1)
    groups = []
    for channel_dir in sorted(root.glob("date=*/coin=*/channel=*")):
        _safe(channel_dir, root)
        partition_date = date.fromisoformat(channel_dir.parents[1].name.removeprefix("date="))
        if partition_date >= cutoff:
            continue
        sources = [
            p for p in sorted(channel_dir.glob("*.parquet"))
            if not p.name.startswith("part-lifecycle-")
        ]
        if not sources:
            continue
        source_rows = [
            {"path": str(p.relative_to(root)), "sha256": _sha(p),
             "bytes": p.stat().st_size,
             "rows": pl.scan_parquet(p).select(pl.len()).collect().item()}
            for p in sources
        ]
        maximum = int(policy.get("max_group_bytes", 268435456))
        chunk: list[dict[str, Any]] = []
        for source in source_rows:
            if chunk and sum(x["bytes"] for x in chunk) + source["bytes"] > maximum:
                groups.append({"partition": str(channel_dir.relative_to(root)),
                               "channel": channel_dir.name.removeprefix("channel="),
                               "sources": chunk,
                               "transform": "LOSSLESS_NORMALIZED_V1",
                               "estimated_output_bytes": sum(x["bytes"] for x in chunk)})
                chunk = []
            chunk.append(source)
        if chunk:
            groups.append({"partition": str(channel_dir.relative_to(root)),
                           "channel": channel_dir.name.removeprefix("channel="),
                           "sources": chunk,
                           "transform": "LOSSLESS_NORMALIZED_V1",
                           "estimated_output_bytes": sum(x["bytes"] for x in chunk)})
    manifest = {"schema_version": 1, "mode": MODE,
                "generated_at": datetime.now(UTC).isoformat(),
                "root": str(root.resolve()), "policy_version": policy["policy_version"],
                "recent_days": recent, "groups": groups,
                "totals": {
                    "groups": len(groups),
                    "source_bytes": sum(
                        sum(x["bytes"] for x in group["sources"])
                        for group in groups),
                    "source_rows": sum(
                        sum(x["rows"] for x in group["sources"])
                        for group in groups)},
                "real_trading": False, "polymarket_mutation": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    _write(output, manifest)
    return manifest


def _compact_group(
    root: Path, group: dict[str, Any], policy: dict[str, Any], min_free: int
) -> dict[str, Any]:
    directory = root / group["partition"]
    _safe(directory, root)
    sources = [root / x["path"] for x in group["sources"]]
    sidecar = directory / "_lifecycle.json"
    if not any(path.exists() for path in sources) and sidecar.exists():
        recorded = json.loads(sidecar.read_text()).get("groups", [])
        wanted = {x["sha256"] for x in group["sources"]}
        if any({x["sha256"] for x in item.get("sources", [])} == wanted for item in recorded):
            return {"partition": group["partition"], "already_applied": True}
    for path, expected in zip(sources, group["sources"], strict=True):
        if not path.is_file() or _sha(path) != expected["sha256"]:
            raise ValueError(f"source identity changed: {path}")
    estimate = int(group["estimated_output_bytes"])
    if disk_usage(root).available < estimate + min_free:
        raise ValueError(f"insufficient free bytes for group: {group['partition']}")
    frame = pl.concat([pl.read_parquet(path) for path in sources], how="diagonal_relaxed")
    channel = str(group["channel"])
    required = set(policy["reader_required_columns"].get(channel, []))
    if missing := required - set(frame.columns):
        raise ValueError(f"reader-required columns missing: {sorted(missing)}")
    dropped = []
    if channel == "l2Book" and "raw_json" in frame.columns:
        source_columns = [
            "coin", "exchange_ts_ms", "bid_levels_json", "ask_levels_json"]
        reconstructed = pl.struct(source_columns).map_elements(
            lambda x: json.dumps(
                {"coin": x["coin"],
                 "levels": [json.loads(x["bid_levels_json"]),
                            json.loads(x["ask_levels_json"])],
                 "time": x["exchange_ts_ms"]},
                separators=(",", ":"), sort_keys=True),
            return_dtype=pl.String,
        )
        if frame.select((reconstructed == pl.col("raw_json")).all()).item():
            frame = frame.drop("raw_json")
            dropped.append("raw_json")
    if "received_at_ns" in frame.columns:
        frame = frame.sort("received_at_ns")
    name = f"part-lifecycle-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.parquet"
    temporary, final = directory / f".{name}.tmp", directory / name
    frame.write_parquet(temporary, compression="zstd", compression_level=19, statistics=True)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    checked = pl.read_parquet(temporary)
    wrong_rows = checked.height != sum(int(x["rows"]) for x in group["sources"])
    if wrong_rows or not required <= set(checked.columns):
        temporary.unlink(missing_ok=True)
        raise ValueError("output verification failed")
    commit = {"format_version": 1, "sources": group["sources"], "output": name,
              "output_sha256": _sha(temporary), "rows": checked.height, "dropped_columns": dropped,
              "verification": "PASS"}
    marker = directory / ".lifecycle-commit.json"
    _write(marker, commit)
    os.replace(temporary, final)
    for source in sources:
        source.unlink()
    _record(directory, commit, final)
    marker.unlink()
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {"partition": group["partition"], "rows": checked.height,
            "bytes_after": final.stat().st_size}


def resume(root: Path) -> list[dict[str, Any]]:
    """Finish interrupted groups whose verified output was already committed."""
    completed = []
    for marker in sorted(root.glob("date=*/coin=*/channel=*/.lifecycle-commit.json")):
        directory = marker.parent
        _safe(directory, root)
        commit = json.loads(marker.read_text())
        final = directory / commit["output"]
        temporary = directory / f".{commit['output']}.tmp"
        if not final.exists() and temporary.is_file():
            if _sha(temporary) != commit["output_sha256"]:
                raise ValueError(f"interrupted output identity mismatch: {directory}")
            os.replace(temporary, final)
        if not final.exists():
            marker.unlink()
            completed.append({
                "partition": str(directory.relative_to(root)), "rolled_back": True})
            continue
        if not final.is_file() or _sha(final) != commit["output_sha256"]:
            raise ValueError(f"interrupted output identity mismatch: {directory}")
        for source in commit["sources"]:
            path = root / source["path"]
            if path.exists():
                if _sha(path) != source["sha256"]:
                    raise ValueError(f"interrupted source identity mismatch: {path}")
                path.unlink()
        _record(directory, commit, final)
        marker.unlink()
        completed.append({"partition": str(directory.relative_to(root)), "resumed": True})
    return completed


def apply(
    manifest_path: Path,
    expected_sha: str,
    policy: dict[str, Any],
    *,
    max_age_minutes: int,
    min_free: int,
) -> dict[str, Any]:
    if _sha(manifest_path) != expected_sha:
        raise ValueError("manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text())
    age = datetime.now(UTC) - datetime.fromisoformat(manifest["generated_at"])
    if (manifest.get("mode") != MODE
            or age > timedelta(minutes=max_age_minutes)
            or age < timedelta(minutes=-5)):
        raise ValueError("manifest mode or age invalid")
    root = Path(manifest["root"])
    results = [_compact_group(root, group, policy, min_free) for group in manifest["groups"]]
    return {"success": True, "manifest_sha256": expected_sha, "groups": results,
            "rows_lost": 0, "real_trading_changed": False, "polymarket_mutation": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hyperliquid-root", type=Path)
    parser.add_argument(
        "--policy", type=Path, default=Path("config/market_tape_lifecycle.json"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--expected-manifest-sha256", default="")
    parser.add_argument("--max-manifest-age-minutes", type=int, default=30)
    parser.add_argument("--min-free-bytes", type=int, default=536870912)
    parser.add_argument("--audit-log", type=Path)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    if args.resume:
        if args.hyperliquid_root is None:
            raise SystemExit("resume requires --hyperliquid-root")
        print(json.dumps({"resumed": resume(args.hyperliquid_root)}, indent=2))
    elif args.apply:
        if not args.expected_manifest_sha256:
            raise SystemExit("--apply requires --expected-manifest-sha256")
        result = apply(
            args.manifest,
            args.expected_manifest_sha256,
            policy,
            max_age_minutes=args.max_manifest_age_minutes,
            min_free=args.min_free_bytes,
        )
        if args.audit_log:
            args.audit_log.parent.mkdir(parents=True, exist_ok=True)
            _write(args.audit_log, result)
        print(json.dumps(result, indent=2))
    else:
        if args.hyperliquid_root is None:
            raise SystemExit("planning requires --hyperliquid-root")
        result = build_plan(
            args.hyperliquid_root,
            policy,
            args.manifest,
            today=datetime.now(UTC).date(),
        )
        print(json.dumps(result, indent=2))
        print(f"MANIFEST_SHA256={_sha(args.manifest)}")


if __name__ == "__main__":
    main()
