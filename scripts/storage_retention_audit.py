#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

DATE_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
COIN_RE = re.compile(r"^coin=(.+)$")


def _du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    out = subprocess.check_output(["du", "-sb", str(path)], text=True)
    return int(out.split()[0])


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _coin_dir_name(coin: str) -> str:
    return coin.replace(":", "_").replace("/", "_")


def _date_from_dir(path: Path) -> date | None:
    match = DATE_RE.match(path.name)
    if not match:
        return None
    return date.fromisoformat(match.group(1))


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f}{unit}"
        value /= 1024
    return f"{value:.2f}TiB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hyperliquid-root",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526/hyperliquid"),
    )
    parser.add_argument(
        "--funnel-dir", type=Path, default=Path("/root/hyperliquid-audit/funnel")
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/root/hyperliquid-audit/storage-retention"),
    )
    parser.add_argument("--recent-days", type=int, default=3)
    args = parser.parse_args()

    root = args.hyperliquid_root
    market = root / "market-shadow"
    report_path = args.funnel_dir / "funnel_report.json"
    screening_path = args.funnel_dir / "screening.jsonl"

    if not report_path.exists():
        raise SystemExit(f"missing funnel report: {report_path}")

    report = _read_json(report_path)
    robust_rows = report.get("robust_candidates") or []
    robust_coins = {str(row["coin"]) for row in robust_rows}
    robust_wallets = {str(row["wallet_address"]).lower() for row in robust_rows}

    screening = _read_jsonl(screening_path)
    positive_rows = [
        row
        for row in screening
        if float(row.get("net_return_bps") or 0) > 0
        and int(row.get("realized_actions") or 0) >= 3
    ]
    positive_coins = {str(row["coin"]) for row in positive_rows}
    positive_wallets = {str(row["wallet_address"]).lower() for row in positive_rows}

    today = datetime.now(timezone.utc).date()
    keep_cutoff = today.toordinal() - max(1, args.recent_days) + 1

    robust_dirs = {_coin_dir_name(c) for c in robust_coins}
    positive_dirs = {_coin_dir_name(c) for c in positive_coins}

    rows: list[dict] = []
    totals = defaultdict(int)

    if market.exists():
        for date_dir in sorted(p for p in market.iterdir() if p.is_dir()):
            day = _date_from_dir(date_dir)
            if day is None:
                continue
            recent = day.toordinal() >= keep_cutoff
            for coin_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
                match = COIN_RE.match(coin_dir.name)
                if not match:
                    continue
                coin_key = match.group(1)
                size = _du_bytes(coin_dir)
                if recent:
                    action = "KEEP_RECENT_FULL_FIDELITY"
                    reason = f"within last {args.recent_days} UTC days"
                elif coin_key in robust_dirs:
                    action = "KEEP_ROBUST_FULL_FIDELITY"
                    reason = "coin appears in robust funnel candidate set"
                elif coin_key in positive_dirs:
                    action = "COMPRESS_CANDIDATE"
                    reason = "coin screened positive but is not currently robust"
                else:
                    action = "DELETE_CANDIDATE"
                    reason = "old raw market tape with no positive/robust cohort dependency"
                rows.append(
                    {
                        "path": str(coin_dir),
                        "date": day.isoformat(),
                        "coin_dir": coin_key,
                        "bytes": size,
                        "action": action,
                        "reason": reason,
                    }
                )
                totals[action] += size

    protected_paths = []
    for name in (
        "shadow",
        "profitability",
        "selective-shadow",
        "research",
        "outputs",
        "resolver",
        "discovery",
        "historical-data",
        "cache",
    ):
        path = root / name
        if path.exists():
            protected_paths.append(
                {
                    "path": str(path),
                    "bytes": _du_bytes(path),
                    "action": "KEEP_PENDING_DEPENDENCY_AUDIT",
                }
            )

    postgres = root / "postgresql"
    postgres_bytes = _du_bytes(postgres)
    protected_paths.append(
        {
            "path": str(postgres),
            "bytes": postgres_bytes,
            "action": "KEEP_PENDING_DATABASE_TABLE_AUDIT",
            "reason": "never prune PostgreSQL by filesystem deletion",
        }
    )

    manifest = {
        "mode": "DRY_RUN_ONLY_NO_DELETION",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "real_trading": False,
        "recent_days_kept_full_fidelity": args.recent_days,
        "funnel": {
            "screened_cohorts": report.get("screened_cohort_count"),
            "positive_screens": report.get("positive_screen_count"),
            "robust_candidates": report.get("robust_candidate_count"),
            "robust_coin_count": len(robust_coins),
            "positive_coin_count": len(positive_coins),
            "robust_wallet_count": len(robust_wallets),
            "positive_wallet_count": len(positive_wallets),
            "robust_coins": sorted(robust_coins),
        },
        "market_shadow": {
            "partition_count": len(rows),
            "totals_bytes": dict(totals),
            "recoverable_delete_candidate_bytes": totals["DELETE_CANDIDATE"],
            "compress_candidate_bytes": totals["COMPRESS_CANDIDATE"],
            "partitions": rows,
        },
        "protected_paths": protected_paths,
        "postgresql_bytes": postgres_bytes,
        "safety": {
            "deletion_performed": False,
            "postgres_filesystem_deletion_allowed": False,
            "apply_requires_separate_explicit_reviewed_manifest": True,
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "storage_retention_manifest.json"
    tmp = output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    tmp.replace(output)

    print("========== PROFITABILITY-DRIVEN STORAGE AUDIT ==========")
    print("mode=DRY_RUN_ONLY_NO_DELETION")
    print(f"robust_candidates={report.get('robust_candidate_count')}")
    print(f"robust_coins={len(robust_coins)}")
    print(f"positive_coins={len(positive_coins)}")
    print(f"market_partitions={len(rows)}")
    for action in (
        "KEEP_RECENT_FULL_FIDELITY",
        "KEEP_ROBUST_FULL_FIDELITY",
        "COMPRESS_CANDIDATE",
        "DELETE_CANDIDATE",
    ):
        print(f"{action}_bytes={totals[action]} ({_human(totals[action])})")
    print(f"postgresql_bytes={postgres_bytes} ({_human(postgres_bytes)})")
    print(f"manifest={output}")
    print("DELETION_PERFORMED=NO")


if __name__ == "__main__":
    main()
