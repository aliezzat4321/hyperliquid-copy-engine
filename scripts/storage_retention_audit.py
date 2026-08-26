#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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
            if line:
                rows.append(json.loads(line))
    return rows


def _canonical_coin(value: str) -> str:
    """Canonical comparison key across logical coins and partition directory names.

    Examples: XYZ:SNDK, xyz:SNDK and xyz_SNDK all become XYZ:SNDK.
    We only reinterpret the first underscore as a namespace separator for known
    namespaced assets, avoiding unsafe global underscore/colon substitution.
    """
    value = str(value).strip().replace("/", ":")
    if ":" in value:
        namespace, symbol = value.split(":", 1)
        return f"{namespace.upper()}:{symbol.upper()}"
    if "_" in value:
        namespace, symbol = value.split("_", 1)
        if namespace.lower() in {"xyz", "para"}:
            return f"{namespace.upper()}:{symbol.upper()}"
    return value.upper()


def _date_from_dir(path: Path) -> date | None:
    match = DATE_RE.match(path.name)
    return date.fromisoformat(match.group(1)) if match else None


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f}{unit}"
        value /= 1024
    return f"{value:.2f}TiB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hyperliquid-root", type=Path, default=Path("/mnt/HC_Volume_106576526/hyperliquid"))
    parser.add_argument("--funnel-dir", type=Path, default=Path("/root/hyperliquid-audit/funnel"))
    parser.add_argument("--output-dir", type=Path, default=Path("/root/hyperliquid-audit/storage-retention"))
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
    robust_coins_raw = {str(row["coin"]) for row in robust_rows}
    robust_coins = {_canonical_coin(c) for c in robust_coins_raw}
    robust_wallets = {str(row["wallet_address"]).lower() for row in robust_rows}

    screening = _read_jsonl(screening_path)
    positive_rows = [row for row in screening if float(row.get("net_return_bps") or 0) > 0 and int(row.get("realized_actions") or 0) >= 3]
    positive_coins_raw = {str(row["coin"]) for row in positive_rows}
    positive_coins = {_canonical_coin(c) for c in positive_coins_raw}
    positive_wallets = {str(row["wallet_address"]).lower() for row in positive_rows}

    today = datetime.now(timezone.utc).date()
    keep_cutoff = today.toordinal() - max(1, args.recent_days) + 1
    rows: list[dict] = []
    totals = defaultdict(int)
    observed_aliases: dict[str, set[str]] = defaultdict(set)

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
                canonical = _canonical_coin(coin_key)
                observed_aliases[canonical].add(coin_key)
                size = _du_bytes(coin_dir)
                if recent:
                    action, reason = "KEEP_RECENT_FULL_FIDELITY", f"within last {args.recent_days} UTC days"
                elif canonical in robust_coins:
                    action, reason = "KEEP_ROBUST_FULL_FIDELITY", "canonical coin appears in robust funnel candidate set"
                elif canonical in positive_coins:
                    action, reason = "COMPRESS_CANDIDATE", "canonical coin screened positive but is not currently robust"
                else:
                    action, reason = "DELETE_CANDIDATE", "old raw market tape with no positive/robust cohort dependency"
                rows.append({"path": str(coin_dir), "date": day.isoformat(), "coin_dir": coin_key, "canonical_coin": canonical, "bytes": size, "action": action, "reason": reason})
                totals[action] += size

    # Fail closed: every robust canonical coin that exists in market-shadow must
    # have zero DELETE/COMPRESS classifications, regardless of alias spelling.
    robust_present = robust_coins & set(observed_aliases)
    unsafe = [r for r in rows if r["canonical_coin"] in robust_present and r["action"] not in {"KEEP_RECENT_FULL_FIDELITY", "KEEP_ROBUST_FULL_FIDELITY"}]
    if unsafe:
        examples = [(r["coin_dir"], r["canonical_coin"], r["action"]) for r in unsafe[:20]]
        raise SystemExit(f"SAFETY_FAIL robust coin aliases classified destructively: {examples}")

    protected_paths = []
    for name in ("shadow", "profitability", "selective-shadow", "research", "outputs", "resolver", "discovery", "historical-data", "cache"):
        path = root / name
        if path.exists():
            protected_paths.append({"path": str(path), "bytes": _du_bytes(path), "action": "KEEP_PENDING_DEPENDENCY_AUDIT"})

    postgres = root / "postgresql"
    postgres_bytes = _du_bytes(postgres)
    protected_paths.append({"path": str(postgres), "bytes": postgres_bytes, "action": "KEEP_PENDING_DATABASE_TABLE_AUDIT", "reason": "never prune PostgreSQL by filesystem deletion"})

    aliases = {k: sorted(v) for k, v in observed_aliases.items() if len(v) > 1}
    manifest = {
        "mode": "DRY_RUN_ONLY_NO_DELETION",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "real_trading": False,
        "recent_days_kept_full_fidelity": args.recent_days,
        "normalization": {"scheme": "canonical namespace:symbol uppercase", "alias_groups": aliases, "robust_alias_safety_passed": True},
        "funnel": {"screened_cohorts": report.get("screened_cohort_count"), "positive_screens": report.get("positive_screen_count"), "robust_candidates": report.get("robust_candidate_count"), "robust_coin_count": len(robust_coins), "positive_coin_count": len(positive_coins), "robust_wallet_count": len(robust_wallets), "positive_wallet_count": len(positive_wallets), "robust_coins": sorted(robust_coins)},
        "market_shadow": {"partition_count": len(rows), "totals_bytes": dict(totals), "recoverable_delete_candidate_bytes": totals["DELETE_CANDIDATE"], "compress_candidate_bytes": totals["COMPRESS_CANDIDATE"], "partitions": rows},
        "protected_paths": protected_paths,
        "postgresql_bytes": postgres_bytes,
        "safety": {"deletion_performed": False, "postgres_filesystem_deletion_allowed": False, "apply_requires_separate_explicit_reviewed_manifest": True, "robust_alias_safety_passed": True},
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
    print(f"alias_groups={len(aliases)}")
    print("ROBUST_ALIAS_SAFETY=PASS")
    print(f"market_partitions={len(rows)}")
    for action in ("KEEP_RECENT_FULL_FIDELITY", "KEEP_ROBUST_FULL_FIDELITY", "COMPRESS_CANDIDATE", "DELETE_CANDIDATE"):
        print(f"{action}_bytes={totals[action]} ({_human(totals[action])})")
    print(f"postgresql_bytes={postgres_bytes} ({_human(postgres_bytes)})")
    print(f"manifest={output}")
    print("DELETION_PERFORMED=NO")


if __name__ == "__main__":
    main()
