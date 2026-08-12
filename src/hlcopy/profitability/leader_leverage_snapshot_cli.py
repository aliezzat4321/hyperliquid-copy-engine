from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from hlcopy.hyperliquid.http_client import HyperliquidHttpClient

D = Decimal
ZERO = D("0")


def _d(value: object) -> Decimal:
    try:
        return D(str(value))
    except Exception:
        return ZERO


def parse_clearinghouse_state(address: str, state: dict[str, Any]) -> dict[str, object]:
    summary = state.get("marginSummary") or {}
    account_value = _d(summary.get("accountValue"))
    total_notional = _d(summary.get("totalNtlPos"))
    margin_used = _d(summary.get("totalMarginUsed"))
    effective_leverage = total_notional / account_value if account_value > ZERO else None

    positions: list[dict[str, object]] = []
    for item in state.get("assetPositions") or []:
        if not isinstance(item, dict):
            continue
        pos = item.get("position") or {}
        if not isinstance(pos, dict):
            continue
        leverage = pos.get("leverage") or {}
        if not isinstance(leverage, dict):
            leverage = {}
        positions.append(
            {
                "coin": str(pos.get("coin") or ""),
                "szi": str(pos.get("szi") or "0"),
                "position_value_usd": str(pos.get("positionValue") or "0"),
                "margin_used_usd": str(pos.get("marginUsed") or "0"),
                "entry_px": pos.get("entryPx"),
                "liquidation_px": pos.get("liquidationPx"),
                "unrealized_pnl_usd": str(pos.get("unrealizedPnl") or "0"),
                "leverage_type": leverage.get("type"),
                "leverage_value": str(leverage.get("value")) if leverage.get("value") is not None else None,
            }
        )

    return {
        "wallet_address": address.lower(),
        "account_value_usd": str(account_value),
        "gross_notional_usd": str(total_notional),
        "margin_used_usd": str(margin_used),
        "effective_gross_leverage": str(effective_leverage) if effective_leverage is not None else None,
        "cross_maintenance_margin_used_usd": str(state.get("crossMaintenanceMarginUsed") or "0"),
        "withdrawable_usd": str(state.get("withdrawable") or "0"),
        "positions": positions,
    }


def _wallets_from_ranked(path: Path, limit: int) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    seen: set[str] = set()
    out: list[str] = []
    for row in payload.get("ranked", []):
        address = str(row.get("wallet_address") or "").lower()
        if not address.startswith("0x") or address in seen:
            continue
        seen.add(address)
        out.append(address)
        if len(out) >= limit:
            break
    return out


async def _run(args: argparse.Namespace) -> None:
    wallets = _wallets_from_ranked(args.ranked, args.limit)
    rows: list[dict[str, object]] = []
    async with HyperliquidHttpClient(
        "https://api.hyperliquid.xyz",
        "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
        concurrency=2,
    ) as client:
        for index, address in enumerate(wallets, 1):
            response = await client.clearinghouse_state(address)
            state = response.response_payload
            if isinstance(state, dict):
                row = parse_clearinghouse_state(address, state)
                row["fetched_at_ms"] = response.fetched_at_ms
                rows.append(row)
            print(f"leader_leverage_snapshot {index}/{len(wallets)} wallet={address[:14]}", flush=True)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "CURRENT_STATE_ONLY_NOT_HISTORICAL",
        "warning": (
            "Current clearinghouseState cannot be backfilled as historical leverage for earlier fills. "
            "Use these snapshots for current diagnostics and prospectively capture state near future signals."
        ),
        "wallets": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.profitability.leader_leverage_snapshot_cli"
    )
    parser.add_argument("--ranked", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=20)
    return parser


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
