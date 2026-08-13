from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hlcopy.profitability.attribution import build_trade_attribution

ROUND_TRIP_PNL_MODEL = "PROPORTIONAL_POSITION_CHANGE_V2_ROUND_TRIP_FEES"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.attribution_cli")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _assert_round_trip_fee_truth(payload: dict[str, object]) -> None:
    if payload.get("pnl_model") != ROUND_TRIP_PNL_MODEL:
        raise SystemExit(
            "trade attribution refuses legacy PnL input: regenerate master_profitability "
            "with round-trip entry+exit fee accounting before ranking cohorts"
        )
    slices = payload.get("realized_slices")
    if not isinstance(slices, list):
        raise SystemExit("trade attribution requires realized_slices list")
    missing = sum(
        isinstance(row, dict)
        and ("entry_fee_usd_allocated" not in row or row.get("entry_fee_usd_allocated") is None)
        for row in slices
    )
    if missing:
        raise SystemExit(
            f"trade attribution refuses {missing} fee-incomplete realized slices; "
            "regenerate source profitability first"
        )


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("trade attribution refuses REAL_TRADING_ENABLED=YES")
    args = build_parser().parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    _assert_round_trip_fee_truth(payload)
    result = build_trade_attribution(payload)
    result["round_trip_fee_truth"] = True
    result["fee_accounting_mode"] = "ALLOCATED_ENTRY_PLUS_EXIT_FEES_V1"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    ranked = result["ranked_complete_cohorts"]
    print(
        "trade_attribution "
        f"slices={result['slice_count']} cohorts={result['cohort_count']} "
        f"latency_complete={result['latency_complete_cohort_count']} "
        "round_trip_fees=YES"
    )
    for row in ranked[:20]:
        print(
            f"{row['lane']:<6} {str(row['wallet_address'])[:12]:<12} "
            f"{row['coin']:<12} {row['direction']:<5} {row['action']:<10} "
            f"${row['notional_usd']:<7} actions={row['robust_actions_floor']:<3} "
            f"worst_bps={row['robust_return_bps']}"
        )


if __name__ == "__main__":
    main()
