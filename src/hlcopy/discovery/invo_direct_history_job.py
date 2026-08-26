from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hlcopy.discovery.invo_resolution_queue import (
    MIN_RESOLUTION_TRADES,
    materialize_resolution_queue_from_store,
)
from hlcopy.discovery.invo_source import InvoApiError, InvoReadOnlyClient
from hlcopy.discovery.invo_store import InvoRecordStore

DEFAULT_STATE_DIR = Path("/var/lib/hyperliquid-copy-engine/invo")
DEFAULT_PRIORITY_TRADERS = (
    "carmine",
    "bones",
    "tyron",
    "rps",
    "tony64dss",
    "profitales",
    "limpan96",
    "vortex_legion",
)
INVESTMENT_BUCKETS = (
    "investmentsTicker",
    "investmentsBusiness",
    "investmentsMaterial",
    "investmentsProperty",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill exact closed Invo investments for fast wallet resolution.",
    )
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--max-portfolios", type=int, default=80)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--refresh-minutes", type=int, default=60)
    parser.add_argument("--priority-trader", action="append", default=[])
    return parser.parse_args()


def _load_object(path: Path, default: Mapping[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _save_object(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalized_name(value: object) -> str:
    return str(value or "").removeprefix("@").strip().casefold()


def _priority_names(values: Sequence[str]) -> tuple[str, ...]:
    selected = values or DEFAULT_PRIORITY_TRADERS
    return tuple(dict.fromkeys(_normalized_name(value) for value in selected if value))


def _timestamp_ms(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        numeric = int(text)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _positive_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _page_investments(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in INVESTMENT_BUCKETS:
        values = payload.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        for value in values:
            if isinstance(value, Mapping):
                rows.append(dict(value))
    return rows


def normalize_direct_investment(
    investment: Mapping[str, Any],
    *,
    portfolio_id: str,
    username: str,
) -> dict[str, object] | None:
    """Convert one exact Invo closed investment into fail-closed resolver evidence."""
    if investment.get("verifiedTrade") is not True:
        return None
    if investment.get("isOpen") is not False:
        return None
    trade_id = str(investment.get("id") or "").strip()
    ticker = str(investment.get("ticker") or "").strip().upper()
    direction_long = investment.get("directionLong")
    if not trade_id or not ticker or not portfolio_id or not username:
        return None
    if not isinstance(direction_long, bool):
        return None

    leverage = _positive_number(investment.get("leverage"))
    entry_price = _positive_number(investment.get("entryPrice"))
    closing_price = _positive_number(investment.get("closingPrice"))
    # Invo entrySize is an allocation value, not trusted Hyperliquid absolute size.
    # The v3 size-agnostic resolver deliberately treats it as source-side sequence
    # evidence only; do not substitute positionSize without independent proof.
    entry_size = _positive_number(investment.get("entrySize"))
    opened_at = investment.get("createdAt")
    closed_at = investment.get("updatedAt")
    opened_ms = _timestamp_ms(opened_at)
    closed_ms = _timestamp_ms(closed_at)
    if None in (leverage, entry_price, closing_price, entry_size, opened_ms, closed_ms):
        return None
    assert opened_ms is not None and closed_ms is not None
    if closed_ms <= opened_ms:
        return None

    return {
        "trade_id": trade_id,
        "trade_alias_ids": [trade_id],
        "username": username,
        "ticker": ticker,
        "direction": "LONG" if direction_long else "SHORT",
        "leverage": leverage,
        "entry_price": entry_price,
        "closing_price": closing_price,
        "entry_size": entry_size,
        "opened_at": str(opened_at),
        "closed_at": str(closed_at),
        "portfolio_id": portfolio_id,
        "source_post_id": f"direct-investment:{portfolio_id}:{trade_id}",
    }


async def _fetch_closed_page(
    client: InvoReadOnlyClient,
    *,
    portfolio_id: str,
    page: int,
    size: int,
) -> dict[str, Any]:
    # This endpoint is read-only and was validated against the live Invo web client.
    return await client._post(  # noqa: SLF001 - internal client endpoint wrapper
        "/v1_0/investments/get_investments",
        {
            "portfolioId": portfolio_id,
            "isOpen": False,
            "params": {"page": page, "size": size},
        },
    )


async def collect_direct_history(
    client: InvoReadOnlyClient,
    *,
    portfolio_id: str,
    username: str,
    max_pages: int,
    page_size: int,
) -> tuple[list[dict[str, object]], int]:
    evidence: dict[str, dict[str, object]] = {}
    pages_fetched = 0
    for page in range(1, max(1, max_pages) + 1):
        payload = await _fetch_closed_page(
            client,
            portfolio_id=portfolio_id,
            page=page,
            size=max(1, page_size),
        )
        pages_fetched += 1
        raw_rows = _page_investments(payload)
        for investment in raw_rows:
            row = normalize_direct_investment(
                investment,
                portfolio_id=portfolio_id,
                username=username,
            )
            if row is not None:
                evidence[str(row["trade_id"])] = row
        if len(raw_rows) < max(1, page_size):
            break
    return list(evidence.values()), pages_fetched


def _verified_portfolios(payload: Mapping[str, Any]) -> set[str]:
    rows = payload.get("identities")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return set()
    return {
        str(row.get("portfolio_id") or "").strip()
        for row in rows
        if isinstance(row, Mapping) and str(row.get("portfolio_id") or "").strip()
    }


def select_candidates(
    universe: Mapping[str, Any],
    *,
    state: Mapping[str, Any],
    verified_portfolio_ids: set[str],
    priority_names: tuple[str, ...],
    max_portfolios: int,
    refresh_minutes: int,
    now_s: int,
) -> list[dict[str, Any]]:
    rows = universe.get("candidates")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    items = state.get("items") if isinstance(state.get("items"), Mapping) else {}
    priority = {name: index for index, name in enumerate(priority_names)}
    fallback = len(priority) + 1
    due: list[dict[str, Any]] = []
    refresh_s = max(1, refresh_minutes) * 60
    for source in rows:
        if not isinstance(source, Mapping):
            continue
        row = dict(source)
        portfolio_id = str(row.get("portfolio_id") or "").strip()
        username = _normalized_name(row.get("username"))
        if not portfolio_id or not username or portfolio_id in verified_portfolio_ids:
            continue
        if bool(row.get("liquidated", False)):
            continue
        try:
            closed_positions = int(row.get("closed_positions") or 0)
        except (TypeError, ValueError):
            closed_positions = 0
        if closed_positions < MIN_RESOLUTION_TRADES:
            continue
        previous = items.get(portfolio_id) if isinstance(items, Mapping) else None
        last_scan_s = (
            int(previous.get("last_scan_s") or 0)
            if isinstance(previous, Mapping)
            else 0
        )
        if last_scan_s and now_s - last_scan_s < refresh_s:
            continue
        row["_never_scanned"] = not bool(last_scan_s)
        due.append(row)

    due.sort(
        key=lambda row: (
            priority.get(_normalized_name(row.get("username")), fallback),
            0 if bool(row.get("_never_scanned")) else 1,
            -float(row.get("screen_score") or 0.0),
            -int(row.get("closed_positions") or 0),
            str(row.get("portfolio_id") or ""),
        )
    )
    limit = max(1, max_portfolios)
    return [
        {key: value for key, value in row.items() if key != "_never_scanned"}
        for row in due[:limit]
    ]


async def run_once(args: argparse.Namespace) -> dict[str, object]:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise RuntimeError("Invo direct-history miner refuses REAL_TRADING_ENABLED=YES")
    access_token = os.getenv("INVO_ACCESS_TOKEN")
    refresh_token = os.getenv("INVO_REFRESH_TOKEN")
    if not access_token and not refresh_token:
        raise RuntimeError("Invo authentication is missing")

    state_dir: Path = args.state_dir
    universe_path = state_dir / "universe_candidates.json"
    store_path = state_dir / "archive.sqlite3"
    queue_dir = state_dir / "resolution_queue"
    state_path = state_dir / "direct_history_state.json"
    identities_path = state_dir / "identified_wallets.json"

    universe = _load_object(universe_path, {"candidates": []})
    state = _load_object(state_path, {"version": 1, "items": {}})
    identities = _load_object(identities_path, {"identities": []})
    now_s = int(time.time())
    selected = select_candidates(
        universe,
        state=state,
        verified_portfolio_ids=_verified_portfolios(identities),
        priority_names=_priority_names(args.priority_trader),
        max_portfolios=max(1, int(args.max_portfolios)),
        refresh_minutes=max(1, int(args.refresh_minutes)),
        now_s=now_s,
    )

    semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))
    errors: list[str] = []

    async with InvoReadOnlyClient(
        access_token=access_token,
        refresh_token=refresh_token,
        timeout_seconds=12.0,
        retry_attempts=2,
    ) as client:
        async def collect(row: Mapping[str, Any]):
            portfolio_id = str(row.get("portfolio_id") or "").strip()
            username = str(row.get("username") or "").strip()
            async with semaphore:
                try:
                    evidence, pages = await collect_direct_history(
                        client,
                        portfolio_id=portfolio_id,
                        username=username,
                        max_pages=max(1, int(args.max_pages)),
                        page_size=max(1, int(args.page_size)),
                    )
                except InvoApiError as exc:
                    return row, [], 0, f"{type(exc).__name__}: {exc}"
                return row, evidence, pages, None

        results = await asyncio.gather(*(collect(row) for row in selected))

    all_evidence: list[dict[str, object]] = []
    items = state.get("items")
    if not isinstance(items, dict):
        items = {}
    successful = 0
    pages_fetched = 0
    for row, evidence, pages, error in results:
        portfolio_id = str(row.get("portfolio_id") or "").strip()
        username = str(row.get("username") or "").strip()
        if error is not None:
            errors.append(f"direct_history:{username}:{portfolio_id}:{error}")
            continue
        successful += 1
        pages_fetched += int(pages)
        all_evidence.extend(evidence)
        items[portfolio_id] = {
            "username": username,
            "last_scan_s": now_s,
            "last_scan_at": datetime.fromtimestamp(now_s, tz=UTC).isoformat(),
            "last_rows": len(evidence),
            "last_pages": int(pages),
        }

    portfolio_metadata = [
        dict(row)
        for row in universe.get("candidates", [])
        if isinstance(row, Mapping)
    ]
    with InvoRecordStore(store_path) as store:
        inserted = store.upsert(
            "evidence",
            all_evidence,
            key_field="source_post_id",
        ) if all_evidence else 0
        evidence_counts = {
            portfolio_id: len(rows) for portfolio_id, rows in store.evidence_groups()
        }
        resolution = materialize_resolution_queue_from_store(
            store=store,
            output_dir=queue_dir,
            portfolios=portfolio_metadata,
            min_trades=MIN_RESOLUTION_TRADES,
        )

    candidates = universe.get("candidates")
    if isinstance(candidates, list):
        for row in candidates:
            if not isinstance(row, dict):
                continue
            portfolio_id = str(row.get("portfolio_id") or "").strip()
            count = evidence_counts.get(portfolio_id, int(row.get("evidence_count") or 0))
            row["evidence_count"] = count
            if count >= MIN_RESOLUTION_TRADES:
                row["tracking_stage"] = "READY_FOR_WALLET_RESOLUTION"
            elif count > 0:
                row["tracking_stage"] = "ACCUMULATING_IDENTITY_EVIDENCE"
        universe["ready_for_wallet_resolution"] = sum(
            isinstance(row, Mapping)
            and row.get("tracking_stage") == "READY_FOR_WALLET_RESOLUTION"
            for row in candidates
        )
        universe["resolution_queue_count"] = resolution["ready_count"]
        universe["direct_history"] = {
            "last_run_at": datetime.fromtimestamp(now_s, tz=UTC).isoformat(),
            "selected_portfolios": len(selected),
            "successful_portfolios": successful,
            "pages_fetched": pages_fetched,
            "evidence_rows_seen": len(all_evidence),
            "new_evidence_rows": inserted,
            "error_count": len(errors),
        }
        surface_errors = universe.get("surface_errors")
        if not isinstance(surface_errors, list):
            surface_errors = []
        universe["surface_errors"] = [
            value for value in surface_errors
            if not str(value).startswith("direct_history:")
        ] + errors
        _save_object(universe_path, universe)

    _save_object(state_path, {"version": 1, "items": items})
    if selected and successful == 0:
        raise RuntimeError("all selected Invo direct-history requests failed")

    return {
        "candidate_portfolios": len(portfolio_metadata),
        "selected_portfolios": len(selected),
        "successful_portfolios": successful,
        "pages_fetched": pages_fetched,
        "evidence_rows_seen": len(all_evidence),
        "new_evidence_rows": inserted,
        "resolution_ready": resolution["ready_count"],
        "errors": errors,
        "real_trading": False,
    }


async def _main() -> int:
    result = await run_once(_parse_args())
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
