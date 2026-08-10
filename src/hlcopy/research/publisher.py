from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from hlcopy.research.ledger import (
    CandidateObservation,
    append_observation,
    artifact_fingerprint,
    now_ns,
)
from hlcopy.shadow.registry import WalletRegistry, WalletSpec


@dataclass(frozen=True, slots=True)
class PublishResult:
    observed: int
    newly_registered: int
    skipped_existing: int
    artifact_fingerprint: str


def _wallet_id(address: str) -> str:
    return f"hl-{address.lower().removeprefix('0x')}"


def publish_ranked_candidates(
    *,
    parquet_path: Path,
    registry: WalletRegistry,
    ledger_path: Path,
    max_candidates: int = 25,
    min_composite_score: float = 0.0,
) -> PublishResult:
    registry.init()
    frame = pl.read_parquet(parquet_path)
    if frame.is_empty():
        return PublishResult(0, 0, 0, artifact_fingerprint([]))
    rows = frame.sort("rank").to_dicts()
    fingerprint = artifact_fingerprint(rows)
    selected = [
        row
        for row in rows
        if float(row.get("composite_score") or 0.0) >= min_composite_score
    ][: max(0, max_candidates)]
    existing_addresses = {
        wallet.source_ref.lower()
        for wallet in registry.load()
        if wallet.source_type == "hyperliquid_wallet"
    }
    observed_at_ns = now_ns()
    newly_registered = 0
    skipped_existing = 0

    for row in selected:
        address = str(row["address"]).lower()
        append_observation(
            ledger_path,
            CandidateObservation(
                observed_at_ns=observed_at_ns,
                candidate_address=address,
                rank=int(row["rank"]),
                composite_score=float(row.get("composite_score") or 0.0),
                style=str(row.get("style") or "UNKNOWN"),
                warning_flags=str(row.get("warning_flags") or ""),
                source_snapshot_ms=(
                    int(row["source_snapshot_ms"])
                    if row.get("source_snapshot_ms") is not None
                    else None
                ),
                screened_count=(
                    int(row["screened_count"])
                    if row.get("screened_count") is not None
                    else None
                ),
                shortlisted_count=(
                    int(row["shortlisted_count"])
                    if row.get("shortlisted_count") is not None
                    else None
                ),
                ranked_count=(
                    int(row["ranked_count"])
                    if row.get("ranked_count") is not None
                    else None
                ),
                source_artifact=str(parquet_path),
                artifact_fingerprint=fingerprint,
                raw_metrics=row,
            ),
        )
        if address in existing_addresses:
            skipped_existing += 1
            continue
        display_name = str(row.get("display_name") or "").strip()
        label = display_name or f"HL {address[:8]}…{address[-6:]}"
        registry.add(
            WalletSpec(
                id=_wallet_id(address),
                label=label,
                source_type="hyperliquid_wallet",
                source_ref=address,
                stage="research",
                coins=(),
                notes=(
                    "Point-in-time research candidate; "
                    f"rank={int(row['rank'])} composite={float(row.get('composite_score') or 0.0):.2f} "
                    f"artifact={parquet_path.name} fingerprint={fingerprint[:16]}"
                ),
            )
        )
        existing_addresses.add(address)
        newly_registered += 1

    return PublishResult(
        observed=len(selected),
        newly_registered=newly_registered,
        skipped_existing=skipped_existing,
        artifact_fingerprint=fingerprint,
    )
