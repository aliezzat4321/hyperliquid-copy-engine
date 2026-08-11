from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path

D = Decimal
ZERO = D("0")


def _score(row: dict[str, object]) -> Decimal:
    if row.get("avg_net_bps") is None:
        return D("-Infinity")
    avg_bps = D(str(row["avg_net_bps"]))
    execution = D(str(row.get("execution_pct", 0))) / D("100")
    executed = D(str(row.get("executed", 0)))
    evidence_weight = min(D("1"), executed / D("30"))
    return avg_bps * execution * evidence_weight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.ranking_cli")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    summaries = list(payload.get("summaries", []))
    for row in summaries:
        row["copy_edge_score"] = str(_score(row))
        avg = D(str(row["avg_net_bps"])) if row.get("avg_net_bps") is not None else ZERO
        row["positive_edge"] = avg > ZERO
    summaries.sort(
        key=lambda row: (
            bool(row["positive_edge"]),
            D(str(row["copy_edge_score"])),
            D(str(row["closed_net_pnl_usd"])),
            int(row["executed"]),
        ),
        reverse=True,
    )
    ranked_payload = {
        "generated_at": payload.get("generated_at"),
        "ranking_method": (
            "avg_net_bps * execution_fraction * min(1, executed/30); positive edge first"
        ),
        "real_trading": False,
        "funding_mode": payload.get("funding_mode"),
        "liquidation_path_mode": payload.get("liquidation_path_mode"),
        "ranked": summaries,
        "substrategies": payload.get("substrategies", []),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "ranked_profitability.json"
    csv_path = args.output_dir / "ranked_profitability.csv"
    json_path.write_text(json.dumps(ranked_payload, indent=2) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(summaries[0]) if summaries else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(summaries)
    print(f"ranked profitability rows={len(summaries)}")
    for row in summaries[:20]:
        print(
            f"{row['lane']:<6} {str(row['wallet_address'])[:12]:<12} {row['scenario']:<11} "
            f"${row['notional_usd']:<7} n={row['executed']:<3} score={row['copy_edge_score']} "
            f"pnl=${row['closed_net_pnl_usd']} avg_bps={row['avg_net_bps']}"
        )


if __name__ == "__main__":
    main()
