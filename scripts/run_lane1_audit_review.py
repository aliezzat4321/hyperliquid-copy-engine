#!/usr/bin/env python3
from __future__ import annotations

import os

from hlcopy.profitability import lane1_audit_bundle as bundle
from hlcopy.profitability.memory_bounded_wide import WideEventStream


def main() -> None:
    if os.getenv("REAL_TRADING_ENABLED", "NO").strip().upper() == "YES":
        raise SystemExit("Lane 1 audit review refuses REAL_TRADING_ENABLED=YES")

    args = bundle.build_parser().parse_args()
    screening_path = args.funnel_report.parent / "screening.jsonl"
    confirmation_path = args.funnel_report.parent / "confirmation.jsonl"
    realized_slices_path = args.funnel_report.parent / "realized_slices.jsonl"

    challenger_queue = bundle._read_json(args.challenger_queue)
    funnel_report = bundle._read_json(args.funnel_report)
    prospective_report = bundle._read_json(args.prospective_report)
    screening_rows = bundle._read_jsonl(screening_path)
    confirmation_rows = bundle._read_jsonl(confirmation_path)
    targets = bundle.collect_audit_targets(
        challenger_queue,
        funnel_report,
        prospective_report,
        screening_rows=screening_rows,
        confirmation_rows=confirmation_rows,
    )
    target_keys = {target.key for target in targets}

    def target_filtered_loader(enriched_dir, *, cutoff_ns):
        return WideEventStream(
            enriched_dir,
            cutoff_ns=cutoff_ns,
            target_keys=target_keys,
        )

    bundle.load_wide_events = target_filtered_loader
    cutoff_ns = int(args.wide_cutoff_ns_file.read_text(encoding="utf-8").strip())
    manifest = bundle.build_lane1_audit_bundle(
        challenger_queue_path=args.challenger_queue,
        funnel_report_path=args.funnel_report,
        prospective_report_path=args.prospective_report,
        wide_enriched_dir=args.wide_enriched_dir,
        cutoff_ns=cutoff_ns,
        market_dir=args.market_dir,
        output_dir=args.output_dir,
        git_sha=str(args.git_sha),
        screening_path=screening_path,
        confirmation_path=confirmation_path,
        realized_slices_path=realized_slices_path,
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
