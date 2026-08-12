from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.leverage_truth import DEFAULT_LEVERAGE_GRID, leverage_matrix

D = Decimal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.leverage_cli")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--leverages",
        nargs="*",
        type=Decimal,
        default=list(DEFAULT_LEVERAGE_GRID),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    summaries = payload.get("summaries", [])
    rows: list[dict[str, object]] = []
    for summary in summaries:
        if not isinstance(summary, dict) or int(summary.get("realized_actions", 0)) <= 0:
            continue
        rows.extend(leverage_matrix(summary, args.leverages))

    rows.sort(
        key=lambda row: (
            D(str(row.get("net_equity_return_bps", "0"))),
            int(row.get("realized_actions", 0)),
        ),
        reverse=True,
    )
    out = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_generated_at": payload.get("generated_at"),
        "model": "FIXED_NOTIONAL_CAPITAL_EFFICIENCY_V1",
        "warning": (
            "Leverage changes required follower equity, not underlying PnL. "
            "Leveraged rows are research-only until path-dependent maintenance "
            "margin, liquidation, funding, and open-position MTM are modeled."
        ),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"leverage rows={len(rows)}")
    for row in rows[:20]:
        print(
            f"{row['lane']:<6} {str(row['wallet_address'])[:12]:<12} "
            f"{row['scenario']:<11} notional=${row['notional_usd']:<7} "
            f"lev={row['follower_leverage']}x equity=${row['equity_required_usd']} "
            f"pnl=${row['net_pnl_usd']} roe={row['net_equity_return_pct']}% "
            f"actions={row['realized_actions']}"
        )


if __name__ == "__main__":
    main()
