from __future__ import annotations

import argparse
from pathlib import Path

from hlcopy.profitability.forward_shadow_veto import write_forward_veto_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hlcopy.profitability.forward_shadow_veto_cli")
    parser.add_argument("--path-truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = write_forward_veto_store(
        path_truth_path=args.path_truth,
        output_path=args.output,
    )
    print(
        "forward_shadow_veto "
        f"active={result['active_veto_count']} "
        f"wallets={len(result['wallet_states'])} "
        f"intervals={len(result['veto_intervals'])} "
        f"output={args.output}",
        flush=True,
    )


if __name__ == "__main__":
    main()
