from __future__ import annotations

import argparse
import csv
import json
from decimal import Decimal
from pathlib import Path

D = Decimal
ZERO = D("0")


def _score(row: dict[str, object]) -> Decimal:
    realized = D(str(row.get("realized_actions", row.get("executed", 0))))
    if realized <= ZERO:
        return D("-Infinity")
    if row.get("net_return_bps") is not None:
        edge = D(str(row["net_return_bps"]))
        evidence_weight = min(D("1"), realized / D("20"))
    elif row.get("avg_net_bps") is not None:
        edge = D(str(row["avg_net_bps"]))
        evidence_weight = min(D("1"), realized / D("30"))
    else:
        return D("-Infinity")
    execution = D(str(row.get("execution_pct", 0))) / D("100")
    return edge * execution * evidence_weight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.ranking_cli")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    all_summaries = list(payload.get("summaries", []))
    summaries = [
        row
        for row in all_summaries
        if int(row.get("realized_actions", row.get("executed", 0))) > 0
    ]
    for row in summaries:
        row["copy_edge_score"] = str(_score(row))
        if row.get("net_return_bps") is not None:
            edge = D(str(row["net_return_bps"]))
        else:
            edge = D(str(row["avg_net_bps"])) if row.get("avg_net_bps") is not None else ZERO
        row["positive_edge"] = edge > ZERO
    summaries.sort(
        key=lambda row: (
            bool(row["positive_edge"]),
            D(str(row["copy_edge_score"])),
            D(str(row["closed_net_pnl_usd"])),
            int(row.get("realized_actions", row.get("executed", 0))),
        ),
        reverse=True,
    )
    ranked_payload = {
        "generated_at": payload.get("generated_at"),
        "pnl_model": payload.get("pnl_model"),
        "ranking_method": (
            "net_return_bps * execution_fraction * min(1, realized_actions/20); "
            "zero-evidence rows excluded; positive edge first"
        ),
        "real_trading": False,
        "funding_mode": payload.get("funding_mode"),
        "liquidation_path_mode": payload.get("liquidation_path_mode"),
        "open_position_mark_to_market": payload.get("open_position_mark_to_market"),
        "excluded_zero_evidence_rows": len(all_summaries) - len(summaries),
        "ranked": summaries,
        "realized_slices": payload.get("realized_slices", []),
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
    print(
        f"ranked profitability rows={len(summaries)} "
        f"excluded_zero_evidence={len(all_summaries) - len(summaries)}"
    )
    for row in summaries[:20]:
        edge = row.get("net_return_bps", row.get("avg_net_bps"))
        print(
            f"{row['lane']:<6} {str(row['wallet_address'])[:12]:<12} {row['scenario']:<11} "
            f"${row['notional_usd']:<7} n={row.get('realized_actions', row.get('executed', 0)):<3} "
            f"score={row['copy_edge_score']} pnl=${row['closed_net_pnl_usd']} edge_bps={edge}"
        )


if __name__ == "__main__":
    main()
