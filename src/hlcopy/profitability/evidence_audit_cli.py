"""CLI for the reusable profitability evidence audit contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hlcopy.profitability.evidence_auditor import audit_evidence, lane3_bundle, read_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.evidence_audit_cli")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--format", choices=("normalized", "lane3-jsonl"), default="normalized")
    parser.add_argument("--manifest", type=Path, help="required metadata/economics for lane3-jsonl")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.format == "lane3-jsonl":
        if args.manifest is None:
            raise SystemExit("--manifest is required for lane3-jsonl")
        bundle = lane3_bundle(read_jsonl(args.input), json.loads(args.manifest.read_text()))
    else:
        bundle = json.loads(args.input.read_text())
    report = audit_evidence(bundle)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "blockers": len(report["blockers"])}))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
