#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC_TZ = timezone(timedelta(0))


@dataclass(frozen=True)
class Candidate:
    path: Path
    day: str
    coin: str
    canonical_coin: str
    bytes_planned: int


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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
    if safety.get("deletion_performed") is not False:
        raise ValueError("manifest already records deletion")
    if safety.get("postgres_filesystem_deletion_allowed") is not False:
        raise ValueError("PostgreSQL filesystem deletion must remain forbidden")
    if safety.get("robust_alias_safety_passed") is not True:
        raise ValueError("robust alias safety did not pass")
    if normalization.get("robust_alias_safety_passed") is not True:
        raise ValueError("normalization robust alias safety did not pass")

    recent_days = int(manifest.get("recent_days_kept_full_fidelity") or 0)
    if recent_days < 1:
        raise ValueError("invalid recent-days protection")

    robust = {
        str(value).upper()
        for value in ((manifest.get("funnel") or {}).get("robust_coins") or [])
    }
    rows = ((manifest.get("market_shadow") or {}).get("partitions") or [])
    if not isinstance(rows, list):
        raise ValueError("market_shadow.partitions must be a list")

    resolved_market = market_root.resolve()
    candidates: list[Candidate] = []
    for row in rows:
        if row.get("action") != "DELETE_CANDIDATE":
            continue
        raw = Path(str(row.get("path") or ""))
        if raw.is_symlink():
            raise ValueError(f"candidate is a symlink: {raw}")
        resolved = raw.resolve(strict=False)
        if not _is_direct_partition(resolved, resolved_market):
            raise ValueError(f"candidate escapes direct market partition layout: {raw}")
        canonical = str(row.get("canonical_coin") or "").upper()
        if not canonical or canonical in robust:
            raise ValueError(f"candidate is missing canonical coin or is robust: {raw}")
        day = str(row.get("date") or "")
        try:
            partition_day = datetime.fromisoformat(day).date()
        except ValueError as exc:
            raise ValueError(f"invalid candidate date: {day}") from exc
        protected_cutoff = now.date() - timedelta(days=recent_days - 1)
        if partition_day >= protected_cutoff:
            raise ValueError(f"candidate falls inside recent protection window: {raw}")
        candidates.append(
            Candidate(
                path=resolved,
                day=day,
                coin=str(row.get("coin_dir") or ""),
                canonical_coin=canonical,
                bytes_planned=int(row.get("bytes") or 0),
            )
        )

    if not candidates:
        raise ValueError(f"no DELETE_CANDIDATE rows in fresh manifest {manifest_path}")
    candidates.sort(key=lambda item: (item.day, -item.bytes_planned, str(item.path)))
    return candidates


def apply_candidates(
    candidates: list[Candidate],
    *,
    mount: Path,
    target_used_pct: float,
    apply: bool,
) -> dict:
    before_pct = _used_pct(mount)
    deleted: list[dict] = []
    skipped_missing: list[str] = []
    for candidate in candidates:
        current_pct = _used_pct(mount)
        if current_pct <= target_used_pct:
            break
        if not candidate.path.exists():
            skipped_missing.append(str(candidate.path))
            continue
        if not candidate.path.is_dir() or candidate.path.is_symlink():
            raise ValueError(f"candidate changed type since audit: {candidate.path}")
        actual_bytes = sum(p.stat().st_size for p in candidate.path.rglob("*") if p.is_file())
        if apply:
            shutil.rmtree(candidate.path)
        deleted.append(
            {
                "path": str(candidate.path),
                "date": candidate.day,
                "coin": candidate.coin,
                "canonical_coin": candidate.canonical_coin,
                "planned_bytes": candidate.bytes_planned,
                "observed_file_bytes": actual_bytes,
                "applied": apply,
            }
        )
    after_pct = _used_pct(mount)
    return {
        "before_used_pct": round(before_pct, 3),
        "after_used_pct": round(after_pct, 3),
        "target_used_pct": target_used_pct,
        "candidate_count_considered": len(candidates),
        "partitions_processed": len(deleted),
        "planned_bytes_processed": sum(row["planned_bytes"] for row in deleted),
        "skipped_missing": skipped_missing,
        "target_reached": after_pct <= target_used_pct,
        "apply": apply,
        "deleted": deleted,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/root/hyperliquid-audit/storage-retention/storage_retention_manifest.json"),
    )
    parser.add_argument(
        "--market-root",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526/hyperliquid/market-shadow"),
    )
    parser.add_argument(
        "--mount",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526"),
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path("/root/hyperliquid-audit/storage-retention/storage_retention_apply.json"),
    )
    parser.add_argument("--target-used-pct", type=float, default=88.0)
    parser.add_argument("--max-manifest-age-minutes", type=int, default=15)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not (70.0 <= args.target_used_pct <= 92.0):
        raise SystemExit("target-used-pct must be between 70 and 92")
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
        target_used_pct=args.target_used_pct,
        apply=args.apply,
    )
    result.update(
        {
            "generated_at": datetime.now(UTC_TZ).isoformat(),
            "manifest": str(args.manifest),
            "mode": "APPLY_REVIEWED_DELETE_CANDIDATES" if args.apply else "DRY_RUN",
            "real_trading_changed": False,
            "postgresql_filesystem_deletion": False,
        }
    )
    args.audit_log.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.audit_log.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.audit_log)

    print("========== STORAGE RETENTION APPLY ==========")
    print(f"mode={result['mode']}")
    print(f"before_used_pct={result['before_used_pct']}")
    print(f"after_used_pct={result['after_used_pct']}")
    print(f"target_used_pct={result['target_used_pct']}")
    print(f"partitions_processed={result['partitions_processed']}")
    print(f"planned_bytes_processed={result['planned_bytes_processed']}")
    print(f"target_reached={result['target_reached']}")
    print(f"audit_log={args.audit_log}")
    print(f"DELETION_PERFORMED={'YES' if args.apply else 'NO'}")
    if args.apply and not result["target_reached"]:
        raise SystemExit("reviewed delete-candidate pool exhausted before target headroom was reached")


if __name__ == "__main__":
    main()
