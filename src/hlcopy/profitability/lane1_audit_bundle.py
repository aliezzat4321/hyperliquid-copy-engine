from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from hlcopy.market.symbols import wire_coin
from hlcopy.profitability.lane1_funding_source import (
    fetch_official_funding_history,
    funding_ranges_for_event_groups,
)
from hlcopy.profitability.lane1_metrics import LANE1_RETURN_BASIS_FUNDING_V2
from hlcopy.profitability.position_copy import CopyFillEvent, load_wide_events

HOUR_MS = 3_600_000
BUNDLE_VERSION = "LANE1_REPLAY_EVIDENCE_V2"


class Lane1AuditBundleError(RuntimeError):
    """Raised when a replay-complete Lane 1 evidence bundle cannot be built."""


@dataclass(frozen=True, slots=True)
class AuditTarget:
    wallet_address: str
    coin: str
    roles: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return self.wallet_address.lower(), self.coin


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Lane1AuditBundleError(f"invalid JSON evidence source: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise Lane1AuditBundleError(f"JSON evidence source must be an object: {path}")
    return payload


def collect_audit_targets(
    challenger_queue: dict[str, Any],
    funnel_report: dict[str, Any],
    prospective_report: dict[str, Any],
) -> tuple[AuditTarget, ...]:
    """Union current challengers, robust OOS candidates and prospective targets."""
    roles: dict[tuple[str, str], set[str]] = defaultdict(set)

    for row in challenger_queue.get("candidates", []):
        if not isinstance(row, dict) or row.get("status") != "challenger":
            continue
        wallet = str(row.get("wallet_address", "")).lower().strip()
        coin = str(row.get("coin", "")).strip()
        if wallet and coin:
            roles[(wallet, coin)].add("challenger")

    for row in funnel_report.get("robust_candidates", []):
        if not isinstance(row, dict):
            continue
        wallet = str(row.get("wallet_address", "")).lower().strip()
        coin = str(row.get("coin", "")).strip()
        if wallet and coin:
            roles[(wallet, coin)].add("robust_oos")

    for row in prospective_report.get("targets", []):
        if not isinstance(row, dict):
            continue
        wallet = str(row.get("wallet_address", row.get("wallet", ""))).lower().strip()
        coin = str(row.get("coin", "")).strip()
        if wallet and coin:
            roles[(wallet, coin)].add("prospective")

    return tuple(
        AuditTarget(wallet, coin, tuple(sorted(target_roles)))
        for (wallet, coin), target_roles in sorted(roles.items())
    )


def _event_dict(event: CopyFillEvent, roles: tuple[str, ...]) -> dict[str, object]:
    return {
        "lane": event.lane,
        "wallet_id": event.wallet_id,
        "wallet_address": event.wallet_address,
        "coin": event.coin,
        "exchange_ts_ms": event.exchange_ts_ms,
        "received_at_ns": event.received_at_ns,
        "tid": event.tid,
        "leader_start": str(event.leader_start),
        "leader_after": str(event.leader_after),
        "leader_delta": str(event.leader_delta),
        "source_price": str(event.source_price),
        "audit_roles": list(roles),
    }


def _utc_dates(start_ms: int, end_ms: int) -> tuple[str, ...]:
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(days)


def _crosses_hourly_boundary(start_ms: int, end_ms: int) -> bool:
    if end_ms < start_ms:
        return False
    first = ((start_ms // HOUR_MS) + 1) * HOUR_MS
    return first <= end_ms


def _required_hourly_boundaries(start_ms: int, end_ms: int) -> tuple[int, ...]:
    first = ((start_ms // HOUR_MS) + 1) * HOUR_MS
    if first > end_ms:
        return ()
    return tuple(range(first, end_ms + 1, HOUR_MS))


def _copy_partition_files(
    market_dir: Path,
    destination: Path,
    *,
    coin: str,
    channel: str,
    dates: tuple[str, ...],
) -> list[Path]:
    copied: list[Path] = []
    wire = wire_coin(coin)
    for day in dates:
        source_dir = market_dir / f"date={day}" / f"coin={wire}" / f"channel={channel}"
        if not source_dir.exists():
            continue
        target_dir = destination / "market" / f"date={day}" / f"coin={wire}" / f"channel={channel}"
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_dir.glob("*.parquet")):
            target = target_dir / source.name
            shutil.copy2(source, target)
            copied.append(target)
    return copied


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_files(root: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "manifest.json":
            continue
        files.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return files


def _copy_optional_report(source: Path | None, destination: Path, name: str) -> None:
    if source is None or not source.exists():
        return
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination / name)


def _funding_rows_cover(
    rows: list[dict[str, object]],
    *,
    coin: str,
    required: tuple[int, ...],
) -> bool:
    observed: set[int] = set()
    target_wire = wire_coin(coin)
    for row in rows:
        if wire_coin(str(row.get("coin", coin))) != target_wire:
            continue
        try:
            observed.add(int(row["time"]))
        except (KeyError, TypeError, ValueError):
            continue
    return set(required).issubset(observed)


def _promote_atomic(staging: Path, output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = output_dir.with_name(f".{output_dir.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    had_previous = output_dir.exists()
    if had_previous:
        os.replace(output_dir, backup)
    try:
        os.replace(staging, output_dir)
    except Exception:
        if had_previous and backup.exists() and not output_dir.exists():
            os.replace(backup, output_dir)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def build_lane1_audit_bundle(
    *,
    challenger_queue_path: Path,
    funnel_report_path: Path,
    prospective_report_path: Path,
    wide_enriched_dir: Path,
    cutoff_ns: int,
    market_dir: Path,
    output_dir: Path,
    git_sha: str,
    screening_path: Path | None = None,
    confirmation_path: Path | None = None,
    realized_slices_path: Path | None = None,
) -> dict[str, object]:
    challenger_queue = _read_json(challenger_queue_path)
    funnel_report = _read_json(funnel_report_path)
    prospective_report = _read_json(prospective_report_path)
    targets = collect_audit_targets(challenger_queue, funnel_report, prospective_report)
    if not targets:
        raise Lane1AuditBundleError(
            "NO_REPLAY_TARGETS: no challenger, robust OOS, or prospective target exists"
        )

    all_events = load_wide_events(wide_enriched_dir, cutoff_ns=cutoff_ns)
    grouped: dict[tuple[str, str], list[CopyFillEvent]] = defaultdict(list)
    for event in all_events:
        grouped[(event.wallet_address.lower(), event.coin)].append(event)

    target_events: dict[tuple[str, str], tuple[CopyFillEvent, ...]] = {}
    for target in targets:
        rows = tuple(
            sorted(
                grouped.get(target.key, ()),
                key=lambda item: (item.exchange_ts_ms, item.received_at_ns, item.tid),
            )
        )
        if not rows:
            raise Lane1AuditBundleError(
                f"MISSING_TARGET_EVENTS: wallet={target.wallet_address} coin={target.coin}"
            )
        target_events[target.key] = rows

    funding_ranges = funding_ranges_for_event_groups(target_events.values())
    funding_rows, funding_errors = asyncio.run(fetch_official_funding_history(funding_ranges))

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.build-", dir=output_dir.parent))
    try:
        report_dir = staging / "reports"
        _copy_optional_report(challenger_queue_path, report_dir, "challenger_queue.json")
        _copy_optional_report(funnel_report_path, report_dir, "funnel_report.json")
        _copy_optional_report(prospective_report_path, report_dir, "prospective_report.json")
        _copy_optional_report(screening_path, report_dir, "screening.jsonl")
        _copy_optional_report(confirmation_path, report_dir, "confirmation.jsonl")
        _copy_optional_report(realized_slices_path, report_dir, "realized_slices.jsonl")

        event_rows: list[dict[str, object]] = []
        target_manifest: list[dict[str, object]] = []
        for target in targets:
            rows = target_events[target.key]
            start_ms = min(event.exchange_ts_ms for event in rows)
            end_ms = max(event.exchange_ts_ms for event in rows)
            event_rows.extend(_event_dict(event, target.roles) for event in rows)

            # Include a small next-date allowance for causal delayed books crossing UTC.
            l2_dates = _utc_dates(start_ms, end_ms + 2_000)
            l2_files = _copy_partition_files(
                market_dir,
                staging,
                coin=target.coin,
                channel="l2Book",
                dates=l2_dates,
            )
            if not l2_files:
                raise Lane1AuditBundleError(
                    f"MISSING_L2_EVIDENCE: wallet={target.wallet_address} coin={target.coin}"
                )

            funding_required = _required_hourly_boundaries(start_ms, end_ms)
            # Copy oracle context even for short spans. It proves the recorded market
            # context and makes a no-funding interval independently inspectable.
            ctx_dates = _utc_dates(max(0, start_ms - 60_000), end_ms + 2_000)
            ctx_files = _copy_partition_files(
                market_dir,
                staging,
                coin=target.coin,
                channel="activeAssetCtx",
                dates=ctx_dates,
            )
            if not ctx_files:
                raise Lane1AuditBundleError(
                    f"MISSING_ORACLE_EVIDENCE: wallet={target.wallet_address} coin={target.coin}"
                )

            if target.coin in funding_errors:
                raise Lane1AuditBundleError(
                    f"FUNDING_FETCH_FAILED: coin={target.coin}: {funding_errors[target.coin]}"
                )
            rows_for_coin = funding_rows.get(target.coin, [])
            if funding_required and not _funding_rows_cover(
                rows_for_coin,
                coin=target.coin,
                required=funding_required,
            ):
                raise Lane1AuditBundleError(
                    f"MISSING_FUNDING_EVIDENCE: coin={target.coin} "
                    f"required={','.join(str(value) for value in funding_required)}"
                )
            funding_name = hashlib.sha256(target.coin.encode("utf-8")).hexdigest()[:16]
            funding_path = staging / "funding_history" / f"{funding_name}.json"
            _write_json(
                funding_path,
                {
                    "coin": target.coin,
                    "wire_coin": wire_coin(target.coin),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "required_hourly_boundaries": list(funding_required),
                    "rows": rows_for_coin,
                },
            )

            target_manifest.append(
                {
                    "wallet_address": target.wallet_address,
                    "coin": target.coin,
                    "wire_coin": wire_coin(target.coin),
                    "roles": list(target.roles),
                    "event_count": len(rows),
                    "start_exchange_ts_ms": start_ms,
                    "end_exchange_ts_ms": end_ms,
                    "crosses_funding_boundary": _crosses_hourly_boundary(start_ms, end_ms),
                    "required_hourly_funding_boundaries": list(funding_required),
                    "l2_file_count": len(l2_files),
                    "active_asset_ctx_file_count": len(ctx_files),
                    "funding_history_row_count": len(rows_for_coin),
                }
            )

        _write_jsonl(staging / "events" / "lane1_target_events.jsonl", event_rows)
        manifest: dict[str, object] = {
            "bundle_version": BUNDLE_VERSION,
            "built_at": datetime.now(UTC).isoformat(),
            "git_sha": git_sha,
            "return_basis": LANE1_RETURN_BASIS_FUNDING_V2,
            "real_trading": False,
            "wide_cutoff_ns": cutoff_ns,
            "target_count": len(targets),
            "targets": target_manifest,
        }
        manifest["files"] = _manifest_files(staging)
        _write_json(staging / "manifest.json", manifest)
        _promote_atomic(staging, output_dir)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.lane1_audit_bundle")
    parser.add_argument(
        "--challenger-queue",
        type=Path,
        default=Path("/root/hyperliquid-audit/funnel/challenger_queue.json"),
    )
    parser.add_argument(
        "--funnel-report",
        type=Path,
        default=Path("/root/hyperliquid-audit/funnel/funnel_report.json"),
    )
    parser.add_argument(
        "--prospective-report",
        type=Path,
        default=Path("/root/hyperliquid-audit/prospective-champions/report.json"),
    )
    parser.add_argument(
        "--wide-enriched-dir",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-enriched-live"),
    )
    parser.add_argument(
        "--wide-cutoff-ns-file",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526/hyperliquid/shadow/wide-finance-cutoff-ns.txt"),
    )
    parser.add_argument(
        "--market-dir",
        type=Path,
        default=Path("/mnt/HC_Volume_106576526/hyperliquid/market-shadow"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/root/hyperliquid-audit/evidence"),
    )
    parser.add_argument("--git-sha", default=os.getenv("HLCOPY_DEPLOYED_GIT_SHA", "UNKNOWN"))
    return parser


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("Lane 1 audit bundle refuses REAL_TRADING_ENABLED=YES")
    args = build_parser().parse_args()
    cutoff_ns = int(args.wide_cutoff_ns_file.read_text(encoding="utf-8").strip())
    manifest = build_lane1_audit_bundle(
        challenger_queue_path=args.challenger_queue,
        funnel_report_path=args.funnel_report,
        prospective_report_path=args.prospective_report,
        wide_enriched_dir=args.wide_enriched_dir,
        cutoff_ns=cutoff_ns,
        market_dir=args.market_dir,
        output_dir=args.output_dir,
        git_sha=str(args.git_sha),
        screening_path=args.funnel_report.parent / "screening.jsonl",
        confirmation_path=args.funnel_report.parent / "confirmation.jsonl",
        realized_slices_path=args.funnel_report.parent / "realized_slices.jsonl",
    )
    print(
        "lane1_audit_bundle_done",
        f"targets={manifest['target_count']}",
        f"git_sha={manifest['git_sha']}",
        f"output={args.output_dir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
