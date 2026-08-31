#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path


def _run(command: list[str], *, timeout: int = 180) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ERROR: {exc}"
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        return f"ERROR rc={completed.returncode}: {stderr}"
    return completed.stdout


def _du_depth(path: Path, depth: int = 2) -> list[dict[str, int | str]]:
    output = _run(
        ["du", "-x", "-B1", f"--max-depth={depth}", str(path)],
        timeout=300,
    )
    if output.startswith("ERROR"):
        return [{"path": str(path), "error": output}]

    rows: list[dict[str, int | str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        size_text, _, name = line.partition("\t")
        if not size_text.isdigit() or not name:
            continue
        rows.append({"path": name, "bytes": int(size_text)})
    rows.sort(key=lambda row: int(row["bytes"]), reverse=True)
    return rows


def _relative_bucket(path: str, mount: Path, depth: int = 3) -> str:
    try:
        rel = Path(path).relative_to(mount)
    except ValueError:
        return path
    parts = rel.parts[:depth]
    return str(Path(*parts)) if parts else "."


def _recent_writes(mount: Path, minutes: int) -> dict:
    output = _run(
        [
            "find",
            str(mount),
            "-xdev",
            "-type",
            "f",
            "-mmin",
            f"-{minutes}",
            "-printf",
            "%s\t%T@\t%p\n",
        ],
        timeout=300,
    )
    if output.startswith("ERROR"):
        return {"error": output, "files": [], "buckets": []}

    files: list[dict[str, int | float | str]] = []
    bucket_bytes: defaultdict[str, int] = defaultdict(int)
    bucket_files: defaultdict[str, int] = defaultdict(int)
    for line in output.splitlines():
        try:
            size_text, mtime_text, path = line.split("\t", 2)
            size = int(size_text)
            mtime = float(mtime_text)
        except (ValueError, TypeError):
            continue
        bucket = _relative_bucket(path, mount)
        bucket_bytes[bucket] += size
        bucket_files[bucket] += 1
        files.append({"path": path, "bytes": size, "mtime_epoch": mtime})

    files.sort(key=lambda row: float(row["mtime_epoch"]), reverse=True)
    buckets = [
        {"bucket": bucket, "bytes": size, "file_count": bucket_files[bucket]}
        for bucket, size in bucket_bytes.items()
    ]
    buckets.sort(key=lambda row: int(row["bytes"]), reverse=True)
    return {
        "window_minutes": minutes,
        "file_count": len(files),
        "bytes_by_current_file_size": sum(int(row["bytes"]) for row in files),
        "buckets": buckets[:40],
        "newest_files": files[:40],
    }


def _open_files(mount: Path) -> dict:
    if shutil.which("lsof") is None:
        return {"available": False, "reason": "lsof not installed"}

    output = _run(["lsof", "-nP"], timeout=60)
    if output.startswith("ERROR"):
        return {"available": True, "error": output}

    matches = [line for line in output.splitlines() if str(mount) in line]
    return {"available": True, "count": len(matches), "rows": matches[:100]}


def _deleted_open_files(mount: Path) -> dict:
    if shutil.which("lsof") is None:
        return {"available": False, "reason": "lsof not installed"}

    output = _run(["lsof", "+L1", "-nP"], timeout=60)
    if output.startswith("ERROR"):
        return {"available": True, "error": output}

    matches = [line for line in output.splitlines() if str(mount) in line]
    return {"available": True, "count": len(matches), "rows": matches[:100]}


def _snapshot_sizes(rows: list[dict[str, int | str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        path = row.get("path")
        size = row.get("bytes")
        if isinstance(path, str) and isinstance(size, int):
            result[path] = size
    return result


def _deltas(previous: dict | None, current_rows: list[dict[str, int | str]]) -> list[dict]:
    if not previous:
        return []
    before = previous.get("size_snapshot")
    if not isinstance(before, dict):
        return []
    current = _snapshot_sizes(current_rows)
    rows = []
    for path, size in current.items():
        old = before.get(path)
        if isinstance(old, int):
            rows.append({"path": path, "delta_bytes": size - old, "bytes": size})
    rows.sort(key=lambda row: int(row["delta_bytes"]), reverse=True)
    return rows[:50]


def _human(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.2f}{unit}"
        amount /= 1024
    return f"{amount:.2f}TiB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mount",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "/root/hyperliquid-audit/storage-diagnostics/storage_volume_diagnostics.json"
        ),
    )
    parser.add_argument("--recent-minutes", type=int, default=120)
    args = parser.parse_args()

    mount = args.mount.resolve()
    if not mount.exists() or not mount.is_dir():
        raise SystemExit(f"missing storage mount: {mount}")

    previous = None
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None

    usage = shutil.disk_usage(mount)
    du_rows = _du_depth(mount, depth=3)
    recent = _recent_writes(mount, args.recent_minutes)
    report = {
        "mode": "READ_ONLY_NO_DELETION",
        "generated_at": datetime.now(UTC).isoformat(),
        "mount": str(mount),
        "filesystem": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_pct": round((usage.used / usage.total) * 100, 3) if usage.total else None,
        },
        "largest_paths": du_rows[:80],
        "recent_writes": recent,
        "open_files": _open_files(mount),
        "deleted_but_open_files": _deleted_open_files(mount),
        "growth_since_previous_snapshot": _deltas(previous, du_rows),
        "size_snapshot": _snapshot_sizes(du_rows),
        "safety": {"deletion_performed": False, "real_trading_changed": False},
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.output)

    print("========== STORAGE VOLUME ATTRIBUTION ==========")
    print("mode=READ_ONLY_NO_DELETION")
    print(
        "filesystem="
        f"used={_human(usage.used)} free={_human(usage.free)} "
        f"total={_human(usage.total)} used_pct={report['filesystem']['used_pct']}"
    )
    print("TOP_PATHS")
    for row in du_rows[:25]:
        if "bytes" in row:
            print(f"{_human(int(row['bytes'])):>12} | {row['path']}")
        else:
            print(f"ERROR | {row}")

    print(f"RECENT_WRITES_LAST_{args.recent_minutes}M")
    for row in recent.get("buckets", [])[:20]:
        print(
            f"{_human(int(row['bytes'])):>12} | files={row['file_count']} | "
            f"{row['bucket']}"
        )

    deltas = report["growth_since_previous_snapshot"]
    if deltas:
        print("GROWTH_SINCE_PREVIOUS_SNAPSHOT")
        for row in deltas[:20]:
            print(f"{_human(int(row['delta_bytes'])):>12} | {row['path']}")
    else:
        print("GROWTH_SINCE_PREVIOUS_SNAPSHOT=UNAVAILABLE_FIRST_SNAPSHOT")

    deleted_open = report["deleted_but_open_files"]
    print(f"DELETED_BUT_OPEN_COUNT={deleted_open.get('count', 0)}")
    print(f"manifest={args.output}")
    print("DELETION_PERFORMED=NO")


if __name__ == "__main__":
    main()
