#!/usr/bin/env python3
"""Issue #90 exact-plan bootstrap storage apply.

This wrapper makes the destructive bootstrap stage consume only the exact
DELETE_CANDIDATE prefix frozen by the reviewed dry-run bundle. It deliberately
reuses the generic retention validator/applier and adds immutable-plan binding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import storage_retention_apply as retention


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _fail(message: str) -> None:
    raise ValueError(message)


def _bind_exact_plan(
    candidates: list[retention.Candidate],
    *,
    plan: dict,
    manifest_sha256: str,
    code_sha: str,
    bootstrap_free_bytes: int,
) -> list[retention.Candidate]:
    if plan.get("mode") != "DRY_RUN_BOOTSTRAP_HEADROOM":
        _fail("bootstrap plan is not a dry-run headroom plan")
    if plan.get("apply") is not False or plan.get("final_exit_gate") is not False:
        _fail("bootstrap plan apply/final-exit flags are invalid")
    if plan.get("target_reached") is not True:
        _fail("bootstrap plan did not prove its objective reachable")
    if int(plan.get("bootstrap_free_bytes") or 0) != bootstrap_free_bytes:
        _fail("bootstrap free-byte objective differs from reviewed plan")
    if str(plan.get("manifest_sha256") or "").lower() != manifest_sha256.lower():
        _fail("bootstrap plan retention-manifest SHA mismatch")
    if str(plan.get("code_sha") or "").lower() != code_sha.lower():
        _fail("bootstrap plan code SHA mismatch")
    if plan.get("postgresql_filesystem_deletion") is not False:
        _fail("bootstrap plan does not prohibit PostgreSQL filesystem deletion")
    if plan.get("polymarket_mutation") is not False:
        _fail("bootstrap plan does not prohibit Polymarket mutation")
    if plan.get("real_trading_changed") is not False:
        _fail("bootstrap plan indicates a trading-state change")

    rows = plan.get("processed")
    if not isinstance(rows, list) or not rows:
        _fail("bootstrap plan contains no processed candidate rows")
    if int(plan.get("partitions_processed") or 0) != len(rows):
        _fail("bootstrap plan processed-count mismatch")

    if len(rows) > len(candidates):
        _fail("bootstrap plan contains more rows than validated manifest")

    selected: list[retention.Candidate] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            _fail(f"bootstrap plan row {index} is not an object")
        candidate = candidates[index]
        planned_path = str(row.get("path") or "")
        if planned_path != str(candidate.path):
            _fail(
                f"bootstrap plan is not the validated deterministic prefix at row {index}: "
                f"{planned_path} != {candidate.path}"
            )
        if int(row.get("planned_bytes") or 0) != candidate.bytes_planned:
            _fail(f"bootstrap planned bytes mismatch at row {index}")
        if int(row.get("observed_file_bytes") or 0) != candidate.bytes_planned:
            _fail(f"bootstrap observed bytes mismatch at row {index}")
        if str(row.get("date") or "") != candidate.day:
            _fail(f"bootstrap date mismatch at row {index}")
        if str(row.get("canonical_coin") or "").upper() != candidate.canonical_coin:
            _fail(f"bootstrap canonical coin mismatch at row {index}")

        expected_device = int(row.get("device") or 0)
        expected_inode = int(row.get("inode") or 0)
        if expected_device <= 0 or expected_inode <= 0:
            _fail(f"bootstrap plan missing filesystem identity at row {index}")
        current = candidate.path.lstat()
        if current.st_dev != expected_device or current.st_ino != expected_inode:
            _fail(f"bootstrap filesystem identity changed at row {index}: {candidate.path}")
        selected.append(candidate)

    reviewed_bytes = sum(item.bytes_planned for item in selected)
    if reviewed_bytes != int(plan.get("planned_bytes_processed") or -1):
        _fail("bootstrap plan byte-total mismatch")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--bootstrap-plan", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-bootstrap-plan-sha256", required=True)
    parser.add_argument("--expected-code-sha", required=True)
    parser.add_argument("--bootstrap-free-bytes", type=int, required=True)
    parser.add_argument("--max-manifest-age-minutes", type=int, default=30)
    parser.add_argument("--market-root", type=Path, default=retention.EXPECTED_MARKET_ROOT)
    parser.add_argument("--mount", type=Path, default=retention.EXPECTED_MOUNT)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not args.apply:
        raise SystemExit("exact-plan wrapper is destructive-only; --apply is required")
    if retention._git_head() != args.expected_code_sha.lower():
        raise SystemExit("exact code SHA mismatch")

    actual_manifest_sha = _sha256(args.manifest)
    if actual_manifest_sha != args.expected_manifest_sha256.lower():
        raise SystemExit("exact retention-manifest SHA mismatch")
    actual_plan_sha = _sha256(args.bootstrap_plan)
    if actual_plan_sha != args.expected_bootstrap_plan_sha256.lower():
        raise SystemExit("exact bootstrap-plan SHA mismatch")

    if args.market_root.resolve(strict=True) != retention.EXPECTED_MARKET_ROOT.resolve(strict=True):
        raise SystemExit("market-root is not the exact Hyperliquid market-shadow directory")
    if args.mount.resolve(strict=True) != retention.EXPECTED_MOUNT.resolve(strict=True):
        raise SystemExit("mount is not the exact Hyperliquid data volume")

    manifest = _read_json(args.manifest)
    candidates = retention.validate_manifest(
        manifest,
        manifest_path=args.manifest,
        market_root=args.market_root,
        max_age_minutes=args.max_manifest_age_minutes,
    )
    plan = _read_json(args.bootstrap_plan)
    selected = _bind_exact_plan(
        candidates,
        plan=plan,
        manifest_sha256=actual_manifest_sha,
        code_sha=args.expected_code_sha,
        bootstrap_free_bytes=args.bootstrap_free_bytes,
    )

    result = retention.apply_bootstrap_candidates(
        selected,
        mount=args.mount,
        market_root=args.market_root,
        bootstrap_free_bytes=args.bootstrap_free_bytes,
        apply=True,
    )
    result.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "APPLY_EXACT_REVIEWED_BOOTSTRAP_PLAN",
            "code_sha": args.expected_code_sha.lower(),
            "manifest_sha256": actual_manifest_sha,
            "bootstrap_plan_sha256": actual_plan_sha,
            "exact_plan_rows": len(selected),
            "postgresql_filesystem_deletion": False,
            "polymarket_mutation": False,
            "real_trading_changed": False,
        }
    )
    args.audit_log.parent.mkdir(parents=True, exist_ok=True)
    temp = args.audit_log.with_suffix(args.audit_log.suffix + ".tmp")
    temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(args.audit_log)

    print(json.dumps(result, indent=2, sort_keys=True))
    print("POSTGRESQL_FILESYSTEM_DELETION=NO")
    print("POLYMARKET_MUTATION=NO")
    print("REAL_TRADING_CHANGE=NO")
    print("DELETION_PERFORMED=YES")
    if not result.get("target_reached"):
        raise SystemExit("exact reviewed bootstrap plan did not reach its free-space objective")


if __name__ == "__main__":
    main()
