#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC_TZ = timezone(timedelta(0))
EXPECTED_MARKET_ROOT = Path("/mnt/HC_Volume_106576526/hyperliquid/market-shadow")
EXPECTED_MOUNT = Path("/mnt/HC_Volume_106576526")
GIB = 1024**3


@dataclass(frozen=True)
class Candidate:
    path: Path
    day: str
    coin: str
    canonical_coin: str
    bytes_planned: int
    device: int
    inode: int


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _du_bytes(path: Path) -> int:
    out = subprocess.check_output(["du", "-sb", str(path)], text=True)
    return int(out.split()[0])


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("manifest generated_at must be timezone-aware")
    return parsed.astimezone(UTC_TZ)


def _used_pct(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return (usage.used / usage.total) * 100 if usage.total else 100.0


def _is_direct_partition(path: Path, market_root: Path) -> bool:
    try:
        rel = path.relative_to(market_root)
    except ValueError:
        return False
    if len(rel.parts) != 2:
        return False
    return rel.parts[0].startswith("date=") and rel.parts[1].startswith("coin=")


def _partition_day_from_path(path: Path, market_root: Path) -> str:
    rel = path.relative_to(market_root)
    date_part = rel.parts[0]
    if not date_part.startswith("date="):
        raise ValueError(f"candidate does not use date= partition layout: {path}")
    return date_part.split("=", 1)[1]


def _assert_no_symlink_components(raw: Path, market_root: Path) -> None:
    if market_root.is_symlink():
        raise ValueError(f"market root must not be a symlink: {market_root}")
    try:
        rel = raw.relative_to(market_root)
    except ValueError as exc:
        raise ValueError(f"candidate is not lexically below market root: {raw}") from exc
    cursor = market_root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"candidate path contains symlink component: {cursor}")


def validate_manifest(
    manifest: dict,
    *,
    manifest_path: Path,
    market_root: Path,
    max_age_minutes: int,
    now: datetime | None = None,
) -> list[Candidate]:
    now = now or datetime.now(UTC_TZ)
    if manifest.get("mode") != "DRY_RUN_ONLY_NO_DELETION":
        raise ValueError("manifest mode is not the audited dry-run mode")
    if manifest.get("real_trading") is not False:
        raise ValueError("manifest must explicitly record real_trading=false")

    generated_at = _parse_time(str(manifest.get("generated_at") or ""))
    age = now - generated_at
    if age < timedelta(0) or age > timedelta(minutes=max_age_minutes):
        raise ValueError(f"manifest is not fresh: age={age}")

    safety = manifest.get("safety") or {}
    normalization = manifest.get("normalization") or {}
    source_evidence = manifest.get("source_evidence") or {}
    if safety.get("deletion_performed") is not False:
        raise ValueError("manifest already records deletion")
    if safety.get("postgres_filesystem_deletion_allowed") is not False:
        raise ValueError("PostgreSQL filesystem deletion must remain forbidden")
    if safety.get("apply_requires_separate_explicit_reviewed_manifest") is not True:
        raise ValueError("manifest does not require a separately reviewed apply step")
    if safety.get("source_evidence_complete") is not True:
        raise ValueError("manifest source evidence is not complete")
    if source_evidence.get("complete") is not True:
        raise ValueError("manifest source_evidence.complete is not true")
    if safety.get("robust_set_nonempty") is not True:
        raise ValueError("manifest robust set is not explicitly non-empty")
    if safety.get("profitability_protection_set_nonempty") is not True:
        raise ValueError("manifest profitability protection set is not explicitly non-empty")
    if safety.get("robust_alias_safety_passed") is not True:
        raise ValueError("robust alias safety did not pass")
    if safety.get("profitability_protection_safety_passed") is not True:
        raise ValueError("profitability protection safety did not pass")
    if normalization.get("robust_alias_safety_passed") is not True:
        raise ValueError("normalization robust alias safety did not pass")
    if normalization.get("profitability_protection_safety_passed") is not True:
        raise ValueError("normalization profitability protection safety did not pass")

    recent_days = int(manifest.get("recent_days_kept_full_fidelity") or 0)
    if recent_days < 3:
        raise ValueError("emergency reclaim requires at least 3 recent UTC days protected")

    funnel = manifest.get("funnel") or {}
    robust_raw = funnel.get("robust_coins")
    if not isinstance(robust_raw, list) or not robust_raw:
        raise ValueError("robust_coins must be a non-empty reviewed list")
    robust = {str(value).upper() for value in robust_raw if str(value).strip()}
    if not robust:
        raise ValueError("robust_coins canonical set is empty")

    protected_raw = funnel.get("profitability_protected_coins")
    if not isinstance(protected_raw, list) or not protected_raw:
        raise ValueError("profitability_protected_coins must be a non-empty reviewed list")
    profitability_protected = {
        str(value).upper() for value in protected_raw if str(value).strip()
    }
    if not profitability_protected or not robust.issubset(profitability_protected):
        raise ValueError("profitability protection set must be non-empty and include robust coins")

    market_shadow = manifest.get("market_shadow") or {}
    rows = market_shadow.get("partitions") or []
    if not isinstance(rows, list):
        raise ValueError("market_shadow.partitions must be a list")

    delete_budget_bytes = int(manifest.get("deletion_budget_bytes") or 0)
    if not (0 < delete_budget_bytes <= 6 * GIB):
        raise ValueError("manifest deletion budget must be >0 and <=6 GiB")

    resolved_market = market_root.resolve(strict=True)
    candidates: list[Candidate] = []
    for row in rows:
        if row.get("action") != "DELETE_CANDIDATE":
            continue
        raw = Path(str(row.get("path") or ""))
        if not raw.is_absolute():
            raise ValueError(f"candidate path must be absolute: {raw}")
        _assert_no_symlink_components(raw, market_root)
        resolved = raw.resolve(strict=True)
        if not _is_direct_partition(resolved, resolved_market):
            raise ValueError(f"candidate escapes direct market partition layout: {raw}")
        canonical = str(row.get("canonical_coin") or "").upper()
        if not canonical:
            raise ValueError(f"candidate is missing canonical coin: {raw}")
        if canonical in robust:
            raise ValueError(f"candidate is robust: {raw}")
        if canonical in profitability_protected:
            raise ValueError(f"candidate has profitability evidence and is protected: {raw}")
        day = str(row.get("date") or "")
        try:
            partition_day = datetime.fromisoformat(day).date()
        except ValueError as exc:
            raise ValueError(f"invalid candidate date: {day}") from exc
        on_disk_day = _partition_day_from_path(resolved, resolved_market)
        if on_disk_day != day:
            raise ValueError(
                f"candidate manifest date does not match partition path: {day} != {on_disk_day}"
            )
        protected_cutoff = now.date() - timedelta(days=recent_days - 1)
        if partition_day >= protected_cutoff:
            raise ValueError(f"candidate falls inside recent protection window: {raw}")
        planned = int(row.get("bytes") or 0)
        if planned <= 0:
            raise ValueError(f"candidate has non-positive planned bytes: {raw}")
        st = raw.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"candidate is not a directory: {raw}")
        candidates.append(
            Candidate(
                path=resolved,
                day=day,
                coin=str(row.get("coin_dir") or ""),
                canonical_coin=canonical,
                bytes_planned=planned,
                device=st.st_dev,
                inode=st.st_ino,
            )
        )

    if not candidates:
        raise ValueError(f"no DELETE_CANDIDATE rows in fresh manifest {manifest_path}")

    candidate_count = len(candidates)
    candidate_total = sum(item.bytes_planned for item in candidates)
    expected_count = int(market_shadow.get("delete_candidate_count") or -1)
    expected_total = int(market_shadow.get("recoverable_delete_candidate_bytes") or -1)
    if candidate_count != expected_count:
        raise ValueError(
            f"delete-candidate count mismatch: rows={candidate_count} manifest={expected_count}"
        )
    if candidate_total != expected_total:
        raise ValueError(
            "delete-candidate byte total mismatch: "
            f"rows={candidate_total} manifest={expected_total}"
        )
    if candidate_total > delete_budget_bytes:
        raise ValueError(
            "delete-candidate pool exceeds reviewed budget: "
            f"{candidate_total} > {delete_budget_bytes}"
        )

    candidates.sort(key=lambda item: (item.day, -item.bytes_planned, str(item.path)))
    return candidates


def _revalidate_identity(candidate: Candidate, market_root: Path) -> None:
    _assert_no_symlink_components(candidate.path, market_root)
    st = candidate.path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"candidate changed type since audit: {candidate.path}")
    if st.st_dev != candidate.device or st.st_ino != candidate.inode:
        raise ValueError(f"candidate identity changed since audit: {candidate.path}")
    observed_bytes = _du_bytes(candidate.path)
    if observed_bytes != candidate.bytes_planned:
        raise ValueError(
            "candidate bytes changed since reviewed manifest: "
            f"{candidate.path} planned={candidate.bytes_planned} observed={observed_bytes}"
        )


def apply_candidates(
    candidates: list[Candidate],
    *,
    mount: Path,
    market_root: Path,
    target_used_pct: float,
    apply: bool,
) -> dict:
    before_pct = _used_pct(mount)
    processed: list[dict] = []
    skipped_missing: list[str] = []
    if apply and not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise ValueError("platform does not provide symlink-attack-resistant shutil.rmtree")

    for candidate in candidates:
        current_pct = _used_pct(mount)
        if current_pct <= target_used_pct:
            break
        if not candidate.path.exists():
            skipped_missing.append(str(candidate.path))
            continue
        _revalidate_identity(candidate, market_root)
        if apply:
            shutil.rmtree(candidate.path)
        processed.append(
            {
                "path": str(candidate.path),
                "date": candidate.day,
                "coin": candidate.coin,
                "canonical_coin": candidate.canonical_coin,
                "planned_bytes": candidate.bytes_planned,
                "observed_file_bytes": candidate.bytes_planned,
                "applied": apply,
            }
        )

    after_pct = _used_pct(mount)
    return {
        "before_used_pct": round(before_pct, 3),
        "after_used_pct": round(after_pct, 3),
        "target_used_pct": target_used_pct,
        "candidate_count_considered": len(candidates),
        "partitions_processed": len(processed),
        "planned_bytes_processed": sum(row["planned_bytes"] for row in processed),
        "skipped_missing": skipped_missing,
        "target_reached": after_pct <= target_used_pct,
        "apply": apply,
        "processed": processed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            "/root/hyperliquid-audit/storage-retention/storage_retention_manifest.json"
        ),
    )
    parser.add_argument("--market-root", type=Path, default=EXPECTED_MARKET_ROOT)
    parser.add_argument("--mount", type=Path, default=EXPECTED_MOUNT)
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path(
            "/root/hyperliquid-audit/storage-retention/storage_retention_apply.json"
        ),
    )
    parser.add_argument("--target-used-pct", type=float, default=75.0)
    parser.add_argument("--max-manifest-age-minutes", type=int, default=15)
    parser.add_argument("--expected-manifest-sha256", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.market_root.resolve(strict=True) != EXPECTED_MARKET_ROOT.resolve(strict=True):
        raise SystemExit("market-root must be the exact Hyperliquid market-shadow directory")
    if args.mount.resolve(strict=True) != EXPECTED_MOUNT.resolve(strict=True):
        raise SystemExit("mount must be the exact Hyperliquid data volume")
    if not (70.0 <= args.target_used_pct < 80.0):
        raise SystemExit("exit-gate target-used-pct must be at least 70 and below 80")

    actual_manifest_sha256 = _sha256(args.manifest)
    expected_sha = str(args.expected_manifest_sha256 or "").strip().lower()
    if expected_sha and actual_manifest_sha256 != expected_sha:
        raise SystemExit(
            "manifest SHA-256 does not match explicitly reviewed manifest: "
            f"actual={actual_manifest_sha256} expected={expected_sha}"
        )
    if args.apply and not expected_sha:
        raise SystemExit("--apply requires --expected-manifest-sha256")

    manifest = _read_json(args.manifest)
    candidates = validate_manifest(
        manifest,
        manifest_path=args.manifest,
        market_root=args.market_root,
        max_age_minutes=args.max_manifest_age_minutes,
    )
    result = apply_candidates(
        candidates,
        mount=args.mount,
        market_root=args.market_root,
        target_used_pct=args.target_used_pct,
        apply=args.apply,
    )
    result.update(
        {
            "generated_at": datetime.now(UTC_TZ).isoformat(),
            "manifest": str(args.manifest),
            "manifest_sha256": actual_manifest_sha256,
            "mode": "APPLY_REVIEWED_DELETE_CANDIDATES" if args.apply else "DRY_RUN",
            "real_trading_changed": False,
            "postgresql_filesystem_deletion": False,
            "polymarket_mutation": False,
            "market_capture_resumed": False,
        }
    )
    args.audit_log.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.audit_log.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.audit_log)

    print("========== HYPERLIQUID EMERGENCY STORAGE RETENTION ==========")
    print(f"mode={result['mode']}")
    print(f"manifest_sha256={actual_manifest_sha256}")
    print(f"before_used_pct={result['before_used_pct']}")
    print(f"after_used_pct={result['after_used_pct']}")
    print(f"target_used_pct={result['target_used_pct']}")
    print(f"partitions_processed={result['partitions_processed']}")
    print(f"planned_bytes_processed={result['planned_bytes_processed']}")
    print(f"target_reached={result['target_reached']}")
    print(f"audit_log={args.audit_log}")
    print("POSTGRESQL_FILESYSTEM_DELETION=NO")
    print("POLYMARKET_MUTATION=NO")
    print("MARKET_CAPTURE_RESUMED=NO")
    print(f"DELETION_PERFORMED={'YES' if args.apply else 'NO'}")
    if args.apply and not result["target_reached"]:
        raise SystemExit(
            "reviewed delete-candidate pool exhausted before target headroom was reached"
        )


if __name__ == "__main__":
    main()
