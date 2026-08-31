#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
COIN_RE = re.compile(r"^coin=(.+)$")
CHANNEL_RE = re.compile(r"^channel=(.+)$")

DEFAULT_DATA_ROOT = Path("/mnt/HC_Volume_106576526/hyperliquid")
DEFAULT_MOUNT = Path("/mnt/HC_Volume_106576526")
DEFAULT_OUTPUT_DIR = Path("/root/hyperliquid-audit/storage-lifecycle")

# These classifications are deliberately conservative. They describe how the
# filesystem may be treated, not whether every row inside a managed database is
# valuable. PostgreSQL must be governed logically through SQL, never rm/rmtree.
DATASET_POLICY: dict[str, dict[str, Any]] = {
    "postgresql": {
        "tier": "DATABASE_MANAGED",
        "filesystem_delete_allowed": False,
        "rebuildable": False,
        "purpose": "canonical relational trading/research state; audit relations logically",
    },
    "market-shadow": {
        "tier": "HIGH_VALUE_RAW",
        "filesystem_delete_allowed": True,
        "rebuildable": False,
        "purpose": "live-observed execution replay tape; retention must be dependency aware",
    },
    "shadow": {
        "tier": "CRITICAL_DURABLE",
        "filesystem_delete_allowed": False,
        "rebuildable": False,
        "purpose": "shadow evidence and wallet stage state",
    },
    "profitability": {
        "tier": "CRITICAL_DURABLE",
        "filesystem_delete_allowed": False,
        "rebuildable": True,
        "purpose": "profitability outputs and evidence lineage",
    },
    "selective-shadow": {
        "tier": "CRITICAL_DURABLE",
        "filesystem_delete_allowed": False,
        "rebuildable": False,
        "purpose": "prospective selective shadow evidence",
    },
    "research": {
        "tier": "CRITICAL_DURABLE",
        "filesystem_delete_allowed": False,
        "rebuildable": False,
        "purpose": "research evidence and experiment lineage",
    },
    "resolver": {
        "tier": "CRITICAL_DURABLE",
        "filesystem_delete_allowed": False,
        "rebuildable": False,
        "purpose": "identity-resolution evidence",
    },
    "discovery": {
        "tier": "WARM_ANALYTICAL",
        "filesystem_delete_allowed": False,
        "rebuildable": True,
        "purpose": "discovery outputs; compact only with dependency proof",
    },
    "historical-data": {
        "tier": "WARM_ANALYTICAL",
        "filesystem_delete_allowed": False,
        "rebuildable": True,
        "purpose": "historical research inputs; prefer reproducible archive sources",
    },
    "outputs": {
        "tier": "WARM_ANALYTICAL",
        "filesystem_delete_allowed": False,
        "rebuildable": True,
        "purpose": "derived outputs; lineage must remain before cleanup",
    },
    "cache": {
        "tier": "LOW_VALUE_REBUILDABLE",
        "filesystem_delete_allowed": True,
        "rebuildable": True,
        "purpose": "rebuildable cache",
    },
}


@dataclass
class Usage:
    apparent_bytes: int = 0
    allocated_bytes: int = 0
    file_count: int = 0

    def add_stat(self, st: os.stat_result) -> None:
        self.apparent_bytes += int(st.st_size)
        self.allocated_bytes += int(getattr(st, "st_blocks", 0)) * 512
        self.file_count += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "apparent_bytes": self.apparent_bytes,
            "allocated_bytes": self.allocated_bytes,
            "file_count": self.file_count,
        }


def _parse_partition_components(path: Path, market_root: Path) -> tuple[str | None, str | None, str | None]:
    try:
        rel = path.relative_to(market_root)
    except ValueError:
        return None, None, None
    day = coin = channel = None
    for part in rel.parts:
        if match := DATE_RE.match(part):
            day = match.group(1)
        elif match := COIN_RE.match(part):
            coin = match.group(1)
        elif match := CHANNEL_RE.match(part):
            channel = match.group(1)
    return day, coin, channel


def _scan_tree(
    root: Path,
    *,
    market_root: Path | None = None,
) -> tuple[Usage, dict[str, Usage], dict[str, Usage], dict[str, Usage]]:
    total = Usage()
    by_date: dict[str, Usage] = defaultdict(Usage)
    by_coin: dict[str, Usage] = defaultdict(Usage)
    by_channel: dict[str, Usage] = defaultdict(Usage)
    if not root.exists():
        return total, by_date, by_coin, by_channel

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Never traverse symlinked directories during an audit.
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        day = coin = channel = None
        if market_root is not None:
            day, coin, channel = _parse_partition_components(current, market_root)
        for filename in filenames:
            path = current / filename
            try:
                st = path.lstat()
            except FileNotFoundError:
                # A live writer may atomically rotate a file while the audit scans.
                continue
            if not os.path.isfile(path) or path.is_symlink():
                continue
            total.add_stat(st)
            if day:
                by_date[day].add_stat(st)
            if coin:
                by_coin[coin].add_stat(st)
            if channel:
                by_channel[channel].add_stat(st)
    return total, by_date, by_coin, by_channel


def _df_bytes(mount: Path) -> dict[str, int | float]:
    output = subprocess.check_output(
        ["df", "-P", "-B1", str(mount)],
        text=True,
    ).splitlines()
    fields = output[-1].split()
    total = int(fields[1])
    used = int(fields[2])
    available = int(fields[3])
    # df Use% is defined over used + user-available blocks and therefore reflects
    # ext reserved blocks differently from shutil.disk_usage.
    df_used_pct = 100.0 * used / (used + available) if used + available else 100.0
    usage = shutil.disk_usage(mount)
    shutil_used_pct = 100.0 * usage.used / usage.total if usage.total else 100.0
    return {
        "df_total_bytes": total,
        "df_used_bytes": used,
        "df_available_bytes": available,
        "df_used_pct": round(df_used_pct, 4),
        "shutil_total_bytes": int(usage.total),
        "shutil_used_bytes": int(usage.used),
        "shutil_free_bytes": int(usage.free),
        "shutil_used_pct": round(shutil_used_pct, 4),
        "reserved_or_semantic_gap_bytes": max(0, total - used - available),
    }


def _load_previous(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _hours_between(current: datetime, previous: dict[str, Any] | None) -> float | None:
    if not previous:
        return None
    raw = previous.get("generated_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    seconds = (current - ts.astimezone(timezone.utc)).total_seconds()
    return seconds / 3600 if seconds > 0 else None


def _forecast_thresholds(
    *,
    mount: dict[str, int | float],
    previous: dict[str, Any] | None,
    elapsed_hours: float | None,
) -> dict[str, Any]:
    current_used = int(mount["df_used_bytes"])
    previous_mount = (previous or {}).get("mount") or {}
    previous_used = previous_mount.get("df_used_bytes")
    growth_bph: float | None = None
    if elapsed_hours and isinstance(previous_used, int):
        delta = current_used - previous_used
        growth_bph = delta / elapsed_hours

    forecasts: dict[str, float | None] = {}
    denominator_total = int(mount["df_used_bytes"]) + int(mount["df_available_bytes"])
    for threshold in (75, 80, 85, 90, 95, 100):
        target_used = denominator_total * threshold / 100
        if current_used >= target_used:
            forecasts[str(threshold)] = 0.0
        elif growth_bph and growth_bph > 0:
            forecasts[str(threshold)] = round((target_used - current_used) / growth_bph, 3)
        else:
            forecasts[str(threshold)] = None

    return {
        "observed_growth_bytes_per_hour": None if growth_bph is None else round(growth_bph, 3),
        "elapsed_hours_since_previous": elapsed_hours,
        "hours_to_df_threshold": forecasts,
    }


def _dataset_growth(
    current: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None,
    elapsed_hours: float | None,
) -> dict[str, dict[str, float | int | None]]:
    previous_datasets = (previous or {}).get("datasets") or {}
    result: dict[str, dict[str, float | int | None]] = {}
    for name, row in current.items():
        prior = previous_datasets.get(name) or {}
        current_bytes = int(row.get("allocated_bytes") or 0)
        prior_bytes = prior.get("allocated_bytes")
        delta: int | None = None
        bph: float | None = None
        if isinstance(prior_bytes, int):
            delta = current_bytes - prior_bytes
            if elapsed_hours:
                bph = delta / elapsed_hours
        result[name] = {
            "delta_allocated_bytes": delta,
            "growth_bytes_per_hour": None if bph is None else round(bph, 3),
        }
    return result


def _sorted_usage(rows: dict[str, Usage]) -> list[dict[str, Any]]:
    return [
        {"name": name, **usage.as_dict()}
        for name, usage in sorted(
            rows.items(),
            key=lambda item: item[1].allocated_bytes,
            reverse=True,
        )
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--mount", type=Path, default=DEFAULT_MOUNT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    mount = _df_bytes(args.mount)
    datasets: dict[str, dict[str, Any]] = {}
    market_by_date: dict[str, Usage] = defaultdict(Usage)
    market_by_coin: dict[str, Usage] = defaultdict(Usage)
    market_by_channel: dict[str, Usage] = defaultdict(Usage)

    names = set(DATASET_POLICY)
    if args.data_root.exists():
        names.update(path.name for path in args.data_root.iterdir() if path.is_dir())

    for name in sorted(names):
        path = args.data_root / name
        if name == "market-shadow":
            usage, market_by_date, market_by_coin, market_by_channel = _scan_tree(
                path,
                market_root=path,
            )
        else:
            usage, _, _, _ = _scan_tree(path)
        policy = DATASET_POLICY.get(
            name,
            {
                "tier": "UNCLASSIFIED_FAIL_CLOSED",
                "filesystem_delete_allowed": False,
                "rebuildable": False,
                "purpose": "unclassified dataset; destructive action forbidden",
            },
        )
        datasets[name] = {
            "path": str(path),
            **usage.as_dict(),
            **policy,
        }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    latest = args.output_dir / "storage_value_audit.json"
    previous = _load_previous(latest)
    elapsed = _hours_between(now, previous)
    growth = _dataset_growth(datasets, previous, elapsed)
    for name, row in datasets.items():
        row.update(growth[name])

    report: dict[str, Any] = {
        "mode": "READ_ONLY_STORAGE_VALUE_AUDIT",
        "generated_at": now.isoformat(),
        "data_root": str(args.data_root),
        "mount_path": str(args.mount),
        "destructive_action_performed": False,
        "postgresql_filesystem_deletion_allowed": False,
        "mount": mount,
        "forecast": _forecast_thresholds(
            mount=mount,
            previous=previous,
            elapsed_hours=elapsed,
        ),
        "datasets": datasets,
        "market_shadow": {
            "by_channel": _sorted_usage(market_by_channel),
            "by_date": _sorted_usage(market_by_date),
            "top_coins": _sorted_usage(market_by_coin)[:50],
        },
        "policy": {
            "version": "storage-value-audit-v1",
            "automatic_deletion_enabled": False,
            "unclassified_datasets_fail_closed": True,
        },
    }

    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    tmp.replace(latest)
    history = args.output_dir / "storage_value_history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "generated_at": report["generated_at"],
                    "mount": report["mount"],
                    "forecast": report["forecast"],
                    "datasets": {
                        name: {
                            "allocated_bytes": row["allocated_bytes"],
                            "tier": row["tier"],
                        }
                        for name, row in datasets.items()
                    },
                },
                separators=(",", ":"),
            )
            + "\n"
        )

    print("========== VALUE-AWARE STORAGE AUDIT ==========")
    print("mode=READ_ONLY_STORAGE_VALUE_AUDIT")
    print(f"df_used_pct={mount['df_used_pct']}")
    print(f"shutil_used_pct={mount['shutil_used_pct']}")
    print(f"df_available_bytes={mount['df_available_bytes']}")
    print(f"reserved_or_semantic_gap_bytes={mount['reserved_or_semantic_gap_bytes']}")
    print(
        "observed_growth_bytes_per_hour="
        f"{report['forecast']['observed_growth_bytes_per_hour']}"
    )
    print("DATASETS_BY_ALLOCATED_BYTES")
    for name, row in sorted(
        datasets.items(),
        key=lambda item: int(item[1]["allocated_bytes"]),
        reverse=True,
    ):
        print(
            f"{name} tier={row['tier']} allocated_gib={row['allocated_bytes']/1024**3:.3f} "
            f"apparent_gib={row['apparent_bytes']/1024**3:.3f} files={row['file_count']} "
            f"growth_bph={row['growth_bytes_per_hour']}"
        )
    print("MARKET_CHANNELS_BY_ALLOCATED_BYTES")
    for row in report["market_shadow"]["by_channel"]:
        print(
            f"{row['name']} allocated_gib={row['allocated_bytes']/1024**3:.3f} "
            f"files={row['file_count']}"
        )
    print(f"report={latest}")
    print(f"history={history}")
    print("DESTRUCTIVE_ACTION_PERFORMED=NO")
    print("POSTGRESQL_FILESYSTEM_DELETION=NO")


if __name__ == "__main__":
    main()
