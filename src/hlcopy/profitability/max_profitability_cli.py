from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability.max_profitability import build_tournament


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.profitability.max_profitability_cli"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-actions", type=int, default=10)
    parser.add_argument("--min-execution-pct", type=Decimal, default=Decimal("10"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    rows = source.get("rows", [])
    tournament = build_tournament(
        [row for row in rows if isinstance(row, dict)],
        min_realized_actions=args.min_actions,
        min_execution_pct=args.min_execution_pct,
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_generated_at": source.get("generated_at"),
        "source_model": source.get("model"),
        **tournament,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    ranked = payload["ranked"]
    print(
        "max_profitability_tournament "
        f"candidates={payload['candidate_count']} "
        f"wallets={len(payload['best_by_wallet'])}"
    )
    for index, row in enumerate(ranked[:30], 1):
        print(
            f"{index:>2}. {row['lane']:<6} {str(row['wallet_address'])[:14]} "
            f"${row['notional_usd']:<7} lev={row['follower_leverage']}x "
            f"worst_roe={row['worst_latency_roe_pct']}% "
            f"median_roe={row['median_latency_roe_pct']}% "
            f"actions={row['realized_actions_floor']} "
            f"exec_floor={row['execution_pct_floor']}% "
            f"score={row['robust_profitability_score']}"
        )


if __name__ == "__main__":
    main()
