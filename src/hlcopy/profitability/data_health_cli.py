from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from hlcopy.models import Fill
from hlcopy.positions.state_machine import POSITION_EPSILON, normalize_position
from hlcopy.shadow.evaluator import load_prospective_episodes
from hlcopy.shadow.registry import WalletRegistry
from hlcopy.shadow.wide_score import build_wide_episodes, load_wide_signals


def _jsonl_rows(folder: Path):
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hlcopy.profitability.data_health_cli")
    p.add_argument("--registry", required=True, type=Path)
    p.add_argument("--shadow-dir", required=True, type=Path)
    p.add_argument("--wide-enriched-dir", required=True, type=Path)
    p.add_argument("--wide-cutoff-ns-file", required=True, type=Path)
    return p


def main() -> None:
    args = build_parser().parse_args()
    registry = WalletRegistry(args.registry)
    wallets = [
        w for w in registry.load()
        if w.enabled and w.source_type == "hyperliquid_wallet" and w.stage in {"validation", "approved"}
    ]

    counts = defaultdict(Counter)
    fill_dir = args.shadow_dir / "fills"
    for row in _jsonl_rows(fill_dir):
        if row.get("kind") != "wallet_fill":
            continue
        wid = str(row.get("wallet_id") or "UNKNOWN")
        counts[wid]["raw_wallet_fill_rows"] += 1
        if row.get("is_snapshot"):
            counts[wid]["snapshot_rows"] += 1
            continue
        counts[wid]["prospective_rows"] += 1
        raw = row.get("fill")
        if not isinstance(raw, dict):
            counts[wid]["malformed_rows"] += 1
            continue
        try:
            fill = Fill.from_raw(str(row.get("wallet_address") or ""), raw)
            start = normalize_position(fill.start_position)
            after = normalize_position(start + fill.signed_size)
            if abs(start) <= POSITION_EPSILON:
                start = 0
            if abs(after) <= POSITION_EPSILON:
                after = 0
            if start == 0 and after != 0:
                counts[wid]["opens"] += 1
            elif start != 0 and after == 0:
                counts[wid]["closes"] += 1
            elif start != 0 and start * after < 0:
                counts[wid]["flips"] += 1
            elif abs(after) > abs(start):
                counts[wid]["increases"] += 1
            elif abs(after) < abs(start):
                counts[wid]["reductions"] += 1
            else:
                counts[wid]["other"] += 1
        except Exception:
            counts[wid]["parse_errors"] += 1

    print("DIRECT LANE")
    for wallet in wallets:
        episodes = load_prospective_episodes(args.shadow_dir, wallet.id)
        c = counts[wallet.id]
        print(
            f"{wallet.id} {wallet.source_ref[:12]} "
            f"raw={c['raw_wallet_fill_rows']} snapshot={c['snapshot_rows']} "
            f"prospective={c['prospective_rows']} opens={c['opens']} increases={c['increases']} "
            f"reductions={c['reductions']} closes={c['closes']} flips={c['flips']} "
            f"episodes={len(episodes)} parse_errors={c['parse_errors']}"
        )

    cutoff_ns = int(args.wide_cutoff_ns_file.read_text(encoding="utf-8").strip())
    signals = load_wide_signals(args.wide_enriched_dir, cutoff_ns=cutoff_ns)
    episodes = build_wide_episodes(signals)
    by_wallet = Counter(s.wallet_address for s in signals)
    closed_by_wallet = Counter(e.wallet_address for e in episodes if e.exit is not None)
    print("\nWIDE LANE")
    print(f"signals={len(signals)} episodes={len(episodes)} completed={sum(e.exit is not None for e in episodes)} wallets={len(by_wallet)}")
    for wallet, n in by_wallet.most_common(25):
        print(f"{wallet[:12]} signals={n} completed_episodes={closed_by_wallet[wallet]}")


if __name__ == "__main__":
    main()
