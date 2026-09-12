#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from hlcopy.profitability.three_lane_diagnostics import write_diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="offline fail-closed three-lane diagnostics")
    parser.add_argument("--lane1-report", type=Path)
    parser.add_argument("--lane2-measurement", type=Path)
    parser.add_argument("--lane2-shadow", type=Path)
    parser.add_argument("--lane3-report", type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    args = parser.parse_args()
    write_diagnostics(lane1_path=args.lane1_report, lane2_path=args.lane2_measurement,
                      lane2_shadow_path=args.lane2_shadow, lane3_path=args.lane3_report,
                      json_output=args.json_output, markdown_output=args.markdown_output)


if __name__ == "__main__":
    main()
