from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.discovery.leaderboard import parse_leaderboard
from hlcopy.discovery.universe import UniverseRow, movement_signals, rank_universe
from hlcopy.hyperliquid.http_client import HyperliquidHttpClient
from hlcopy.shadow.registry import WalletRegistry, WalletSpec

SCOUT_MARKER = "universe-scout-v1"


def _load_previous(path: Path) -> tuple[dict[str, int], dict[str, dict[str, object]]]:
    if not path.exists():
        return {}, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("wallets", {})
    if not isinstance(rows, dict):
        return {}, {}
    ranks: dict[str, int] = {}
    clean: dict[str, dict[str, object]] = {}
    for address, raw in rows.items():
        if not isinstance(raw, dict):
            continue
        try:
            rank = int(raw.get("rank", 0))
        except (TypeError, ValueError):
            rank = 0
        if rank > 0:
            ranks[str(address).lower()] = rank
        clean[str(address).lower()] = raw
    return ranks, clean


def _registration_rows(
    rows: list[UniverseRow],
    signals: dict[str, tuple[str, ...]],
    *,
    top_n: int,
    movement_limit: int,
) -> list[UniverseRow]:
    chosen: dict[str, UniverseRow] = {row.address: row for row in rows[:top_n]}
    movers = [
        row
        for row in rows
        if any(
            tag in signals.get(row.address, ())
            for tag in ("ENTERED_TOP_100", "RANK_JUMP_25")
        )
    ]
    for row in movers[:movement_limit]:
        chosen[row.address] = row
    return sorted(chosen.values(), key=lambda row: row.rank)


def _register_research_wallets(
    registry: WalletRegistry,
    rows: list[UniverseRow],
    signals: dict[str, tuple[str, ...]],
    *,
    max_total_research: int,
) -> tuple[list[str], list[str]]:
    registry.init()
    existing = registry.load()
    existing_addresses = {
        wallet.source_ref.lower()
        for wallet in existing
        if wallet.source_type == "hyperliquid_wallet"
    }
    research_count = sum(
        wallet.source_type == "hyperliquid_wallet" and wallet.stage == "research"
        for wallet in existing
    )
    added: list[str] = []
    skipped_capacity: list[str] = []
    for row in rows:
        if row.address in existing_addresses:
            continue
        if research_count >= max_total_research:
            skipped_capacity.append(row.address)
            continue
        tags = ",".join(signals.get(row.address, ())) or "TOP_UNIVERSE"
        registry.add(
            WalletSpec(
                id=f"hl-{row.address[2:]}",
                label=row.display_name or row.address[:14],
                source_type="hyperliquid_wallet",
                source_ref=row.address,
                stage="research",
                enabled=True,
                notes=(
                    f"{SCOUT_MARKER}; leaderboard_rank={row.rank}; "
                    f"score={row.score}; signals={tags}"
                ),
            )
        )
        existing_addresses.add(row.address)
        research_count += 1
        added.append(row.address)
    return added, skipped_capacity


async def _run(args: argparse.Namespace) -> None:
    previous_ranks, previous_wallets = _load_previous(args.state)
    async with HyperliquidHttpClient(
        args.api_url,
        args.leaderboard_url,
        concurrency=1,
    ) as client:
        response = await client.leaderboard()

    candidates = parse_leaderboard(response.response_payload)
    universe = rank_universe(candidates, min_account_value=args.min_account_value)
    signals = movement_signals(universe, previous_ranks)
    registration_rows = _registration_rows(
        universe,
        signals,
        top_n=args.top_register,
        movement_limit=args.movement_register,
    )

    added: list[str] = []
    skipped_capacity: list[str] = []
    if args.registry is not None:
        added, skipped_capacity = _register_research_wallets(
            WalletRegistry(args.registry),
            registration_rows,
            signals,
            max_total_research=args.max_total_research,
        )

    now = datetime.now(UTC).isoformat()
    wallets: dict[str, dict[str, object]] = {}
    for row in universe:
        previous = previous_wallets.get(row.address, {})
        wallets[row.address] = row.to_dict() | {
            "signals": list(signals.get(row.address, ())),
            "first_seen_at": previous.get("first_seen_at", now),
            "last_seen_at": now,
            "previous_rank": previous_ranks.get(row.address),
        }

    payload = {
        "generated_at": now,
        "source_fetched_at_ms": response.fetched_at_ms,
        "source": "HYPERLIQUID_OFFICIAL_LEADERBOARD",
        "screened_wallets": len(candidates),
        "eligible_wallets": len(universe),
        "registered_this_run": added,
        "registration_capacity_skips": skipped_capacity,
        "wallets": wallets,
        "top": [
            row.to_dict() | {"signals": list(signals.get(row.address, ()))}
            for row in universe[:100]
        ],
    }
    args.state.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.state.with_suffix(args.state.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.state)

    movement_tags = (
        "NEW_TO_OBSERVED_LEADERBOARD",
        "ENTERED_TOP_100",
        "RANK_JUMP_25",
    )
    events = [
        row.to_dict() | {"signals": list(tags)}
        for row in universe
        if (tags := signals.get(row.address))
        and any(tag in tags for tag in movement_tags)
    ]
    args.events.parent.mkdir(parents=True, exist_ok=True)
    args.events.write_text(
        json.dumps({"generated_at": now, "events": events}, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        "universe_scout "
        f"screened={len(candidates)} eligible={len(universe)} "
        f"signals={len(events)} registered={len(added)} "
        f"capacity_skips={len(skipped_capacity)}",
        flush=True,
    )
    for row in universe[:20]:
        print(
            f"rank={row.rank:<3} wallet={row.address[:14]} score={row.score:<8} "
            f"day={row.day_roi:.4f} week={row.week_roi:.4f} month={row.month_roi:.4f} "
            f"signals={','.join(signals.get(row.address, ())) or '-'}",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hlcopy.discovery.universe_watch_cli"
    )
    parser.add_argument(
        "--leaderboard-url",
        default="https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
    )
    parser.add_argument("--api-url", default="https://api.hyperliquid.xyz")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--min-account-value", type=float, default=1_000.0)
    parser.add_argument("--top-register", type=int, default=300)
    parser.add_argument("--movement-register", type=int, default=100)
    parser.add_argument("--max-total-research", type=int, default=600)
    return parser


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
