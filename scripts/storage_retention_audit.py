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
GIB = 1024**3


def _du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    out = subprocess.check_output(["du", "-sb", str(path)], text=True)
    return int(out.split()[0])


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _read_required_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"missing required screening evidence: {path}")
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise SystemExit(f"invalid screening row at {path}:{lineno}")
            rows.append(value)
    if not rows:
        raise SystemExit(f"empty required screening evidence: {path}")
    return rows


def _canonical_coin(value: str) -> str:
    """Canonical comparison key across logical coins and partition directory names."""
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


def _require_nonempty_robust_evidence(report: dict) -> tuple[list[dict], int, bool]:
    raw = report.get("robust_candidates")
    if not isinstance(raw, list) or not raw:
        raise SystemExit(
            "SAFETY_FAIL robust_candidates is missing/empty; destructive classification is forbidden"
        )
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or not str(row.get("coin") or "").strip():
            raise SystemExit(f"SAFETY_FAIL robust candidate {index} has no coin")
        if not str(row.get("wallet_address") or "").strip():
            raise SystemExit(f"SAFETY_FAIL robust candidate {index} has no wallet_address")
    try:
        reported = int(report.get("robust_candidate_count"))
    except (TypeError, ValueError) as exc:
        raise SystemExit("SAFETY_FAIL robust_candidate_count is missing/invalid") from exc
    if reported <= 0 or reported < len(raw):
        raise SystemExit(
            "SAFETY_FAIL robust_candidate_count cannot be smaller than robust_candidates payload"
        )
    # incremental_funnel_cli intentionally caps robust_candidates at 100 while
    # robust_candidate_count is the full count. A larger count therefore means
    # the payload is a preview, not corruption. We compensate below by protecting
    # EVERY positive screening coin from deletion, and we require screening.jsonl
    # to exactly match screened_cohort_count.
    return raw, reported, reported > len(raw)


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
    parser.add_argument("--max-delete-candidate-gib", type=float, default=6.0)
    args = parser.parse_args()

    if args.recent_days < 3:
        raise SystemExit("SAFETY_FAIL at least 3 recent UTC days must remain full fidelity")
    if args.max_delete_candidate_gib <= 0:
        raise SystemExit("SAFETY_FAIL emergency delete-candidate budget must be positive")

    root = args.hyperliquid_root
    market = root / "market-shadow"
    report_path = args.funnel_dir / "funnel_report.json"
    screening_path = args.funnel_dir / "screening.jsonl"
    if not market.exists():
        raise SystemExit(f"missing market-shadow root: {market}")
    if not report_path.exists():
        raise SystemExit(f"missing funnel report: {report_path}")

    report = _read_json(report_path)
    if report.get("mode") != "INCREMENTAL_PROFITABILITY_FUNNEL_V1":
        raise SystemExit("SAFETY_FAIL unexpected profitability funnel mode")
    if report.get("real_trading") is not False:
        raise SystemExit("SAFETY_FAIL funnel report must explicitly record real_trading=false")

    robust_rows, robust_candidate_count, robust_payload_truncated = (
        _require_nonempty_robust_evidence(report)
    )
    robust_coins_raw = {str(row["coin"]) for row in robust_rows}
    robust_coins = {_canonical_coin(c) for c in robust_coins_raw}
    robust_wallets = {str(row["wallet_address"]).lower() for row in robust_rows}
    if not robust_coins:
        raise SystemExit("SAFETY_FAIL robust coin set is empty")

    screening = _read_required_jsonl(screening_path)
    try:
        reported_screened = int(report.get("screened_cohort_count"))
    except (TypeError, ValueError) as exc:
        raise SystemExit("SAFETY_FAIL screened_cohort_count is missing/invalid") from exc
    if reported_screened <= 0 or reported_screened != len(screening):
        raise SystemExit(
            "SAFETY_FAIL screening row count disagrees with funnel screened_cohort_count; "
            "screening may be truncated"
        )

    # Deliberately conservative: any cohort that was profitable at all with at
    # least one realized action protects its entire coin from raw-tape deletion.
    # This is a superset of every robust cohort regardless of the funnel's
    # min-screen-actions setting, so a >100 robust preview cannot create a hole.
    positive_rows = [
        row
        for row in screening
        if float(row.get("net_return_bps") or 0) > 0
        and int(row.get("realized_actions") or 0) >= 1
    ]
    positive_coins_raw = {
        str(row["coin"]) for row in positive_rows if str(row.get("coin") or "").strip()
    }
    positive_coins = {_canonical_coin(c) for c in positive_coins_raw}
    positive_wallets = {
        str(row["wallet_address"]).lower()
        for row in positive_rows
        if str(row.get("wallet_address") or "").strip()
    }
    profitability_protected_coins = robust_coins | positive_coins
    if not profitability_protected_coins:
        raise SystemExit("SAFETY_FAIL profitability protection set is empty")
    missing_from_positive = robust_coins - positive_coins
    if missing_from_positive:
        raise SystemExit(
            "SAFETY_FAIL listed robust coins are absent from conservative positive screening set: "
            f"{sorted(missing_from_positive)}"
        )

    today = datetime.now(timezone.utc).date()
    keep_cutoff = today.toordinal() - max(1, args.recent_days) + 1
    rows: list[dict] = []
    totals = defaultdict(int)
    observed_aliases: dict[str, set[str]] = defaultdict(set)

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
                action, reason = (
                    "KEEP_RECENT_FULL_FIDELITY",
                    f"within last {args.recent_days} UTC days",
                )
            elif canonical in robust_coins:
                action, reason = (
                    "KEEP_ROBUST_FULL_FIDELITY",
                    "canonical coin appears in robust funnel preview",
                )
            elif canonical in positive_coins:
                action, reason = (
                    "COMPRESS_CANDIDATE",
                    "canonical coin has positive profitability evidence; never emergency-delete raw tape",
                )
            else:
                action, reason = (
                    "DELETE_CANDIDATE",
                    "old raw market tape with no positive/robust cohort dependency",
                )
            rows.append(
                {
                    "path": str(coin_dir),
                    "date": day.isoformat(),
                    "coin_dir": coin_key,
                    "canonical_coin": canonical,
                    "bytes": size,
                    "action": action,
                    "reason": reason,
                }
            )
            totals[action] += size

    protected_present = profitability_protected_coins & set(observed_aliases)
    unsafe = [
        row
        for row in rows
        if row["canonical_coin"] in protected_present
        and row["action"] == "DELETE_CANDIDATE"
    ]
    profitability_safety_passed = bool(profitability_protected_coins) and not unsafe
    if not profitability_safety_passed:
        examples = [
            (row["coin_dir"], row["canonical_coin"], row["action"])
            for row in unsafe[:20]
        ]
        raise SystemExit(
            f"SAFETY_FAIL profitability-protected coin aliases classified for delete: {examples}"
        )

    robust_present = robust_coins & set(observed_aliases)
    robust_unsafe = [
        row
        for row in rows
        if row["canonical_coin"] in robust_present
        and row["action"]
        not in {"KEEP_RECENT_FULL_FIDELITY", "KEEP_ROBUST_FULL_FIDELITY"}
    ]
    robust_alias_safety_passed = bool(robust_coins) and not robust_unsafe
    if not robust_alias_safety_passed:
        examples = [
            (row["coin_dir"], row["canonical_coin"], row["action"])
            for row in robust_unsafe[:20]
        ]
        raise SystemExit(
            f"SAFETY_FAIL listed robust coin aliases classified destructively: {examples}"
        )

    delete_budget_bytes = int(args.max_delete_candidate_gib * GIB)
    if totals["DELETE_CANDIDATE"] > delete_budget_bytes:
        raise SystemExit(
            "SAFETY_FAIL delete-candidate pool exceeds reviewed emergency budget: "
            f"{_human(totals['DELETE_CANDIDATE'])} > {_human(delete_budget_bytes)}"
        )

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

    aliases = {k: sorted(v) for k, v in observed_aliases.items() if len(v) > 1}
    delete_count = sum(1 for row in rows if row["action"] == "DELETE_CANDIDATE")
    source_evidence_complete = bool(
        robust_rows
        and screening
        and robust_coins
        and profitability_protected_coins
        and profitability_safety_passed
    )
    manifest = {
        "mode": "DRY_RUN_ONLY_NO_DELETION",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "real_trading": False,
        "recent_days_kept_full_fidelity": args.recent_days,
        "deletion_budget_bytes": delete_budget_bytes,
        "source_evidence": {
            "complete": source_evidence_complete,
            "funnel_report": str(report_path),
            "screening": str(screening_path),
            "screening_row_count": len(screening),
            "reported_screened_cohort_count": reported_screened,
            "robust_preview_row_count": len(robust_rows),
            "reported_robust_candidate_count": robust_candidate_count,
            "robust_payload_truncated": robust_payload_truncated,
        },
        "normalization": {
            "scheme": "canonical namespace:symbol uppercase",
            "alias_groups": aliases,
            "robust_alias_safety_passed": robust_alias_safety_passed,
            "profitability_protection_safety_passed": profitability_safety_passed,
        },
        "funnel": {
            "screened_cohorts": reported_screened,
            "positive_screens": report.get("positive_screen_count"),
            "robust_candidates": robust_candidate_count,
            "robust_preview_count": len(robust_rows),
            "robust_payload_truncated": robust_payload_truncated,
            "robust_coin_count": len(robust_coins),
            "positive_coin_count": len(positive_coins),
            "profitability_protected_coin_count": len(profitability_protected_coins),
            "robust_wallet_count": len(robust_wallets),
            "positive_wallet_count": len(positive_wallets),
            "robust_coins": sorted(robust_coins),
            "profitability_protected_coins": sorted(profitability_protected_coins),
        },
        "market_shadow": {
            "partition_count": len(rows),
            "totals_bytes": dict(totals),
            "recoverable_delete_candidate_bytes": totals["DELETE_CANDIDATE"],
            "delete_candidate_count": delete_count,
            "compress_candidate_bytes": totals["COMPRESS_CANDIDATE"],
            "partitions": rows,
        },
        "protected_paths": protected_paths,
        "postgresql_bytes": postgres_bytes,
        "safety": {
            "deletion_performed": False,
            "postgres_filesystem_deletion_allowed": False,
            "apply_requires_separate_explicit_reviewed_manifest": True,
            "source_evidence_complete": source_evidence_complete,
            "robust_set_nonempty": bool(robust_coins),
            "profitability_protection_set_nonempty": bool(profitability_protected_coins),
            "robust_alias_safety_passed": robust_alias_safety_passed,
            "profitability_protection_safety_passed": profitability_safety_passed,
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "storage_retention_manifest.json"
    tmp = output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    tmp.replace(output)

    print("========== PROFITABILITY-DRIVEN STORAGE AUDIT ==========")
    print("mode=DRY_RUN_ONLY_NO_DELETION")
    print(f"source_evidence_complete={source_evidence_complete}")
    print(f"screening_rows={len(screening)}")
    print(f"robust_candidates_reported={robust_candidate_count}")
    print(f"robust_preview_rows={len(robust_rows)}")
    print(f"robust_payload_truncated={robust_payload_truncated}")
    print(f"robust_coins_preview={len(robust_coins)}")
    print(f"positive_coins_conservative={len(positive_coins)}")
    print(f"profitability_protected_coins={len(profitability_protected_coins)}")
    print(f"alias_groups={len(aliases)}")
    print(f"ROBUST_ALIAS_SAFETY={'PASS' if robust_alias_safety_passed else 'FAIL'}")
    print(
        f"PROFITABILITY_PROTECTION_SAFETY={'PASS' if profitability_safety_passed else 'FAIL'}"
    )
    print(f"market_partitions={len(rows)}")
    for action in (
        "KEEP_RECENT_FULL_FIDELITY",
        "KEEP_ROBUST_FULL_FIDELITY",
        "COMPRESS_CANDIDATE",
        "DELETE_CANDIDATE",
    ):
        print(f"{action}_bytes={totals[action]} ({_human(totals[action])})")
    print(f"delete_candidate_budget={delete_budget_bytes} ({_human(delete_budget_bytes)})")
    print(f"postgresql_bytes={postgres_bytes} ({_human(postgres_bytes)})")
    print(f"manifest={output}")
    print("DELETION_PERFORMED=NO")


if __name__ == "__main__":
    main()
