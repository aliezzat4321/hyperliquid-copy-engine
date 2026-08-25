from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hlcopy.resolver.identifier import identify_wallet_from_csv


def test_identifier_fails_closed_when_any_row_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "mixed.csv"
    path.write_text(
        "id,symbol,position_side,avg_entry_price,avg_exit_price,start_time,end_time\n"
        "1,BTC,LONG,100,110,2026-08-01T10:00:00Z,2026-08-01T11:00:00Z\n"
        "2,ETH,SHORT,200,190,2026-08-02T10:00:00Z,2026-08-02T11:00:00Z\n"
        "3,SOL,LONG,150,155,2026-08-03T10:00:00Z,2026-08-03T11:00:00Z\n"
        "4,BTC,SIDEWAYS,100,101,2026-08-04T10:00:00Z,2026-08-04T11:00:00Z\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="malformed rows; fail closed"):
        asyncio.run(identify_wallet_from_csv(path))


def test_identifier_rejects_mixed_source_identities_before_discovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-identities.csv"
    path.write_text(
        "id,username,symbol,position_side,avg_entry_price,avg_exit_price,start_time,end_time\n"
        "1,carmine,BTC,LONG,100,110,2026-08-01T10:00:00Z,2026-08-01T11:00:00Z\n"
        "2,bones,ETH,SHORT,200,190,2026-08-02T10:00:00Z,2026-08-02T11:00:00Z\n"
        "3,carmine,SOL,LONG,150,155,2026-08-03T10:00:00Z,2026-08-03T11:00:00Z\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mixes multiple source identities"):
        asyncio.run(identify_wallet_from_csv(path))


def test_identifier_rejects_caller_identity_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "one-identity.csv"
    path.write_text(
        "id,portfolio_id,symbol,position_side,avg_entry_price,avg_exit_price,start_time,end_time\n"
        "1,carmine-id,BTC,LONG,100,110,2026-08-01T10:00:00Z,2026-08-01T11:00:00Z\n"
        "2,carmine-id,ETH,SHORT,200,190,2026-08-02T10:00:00Z,2026-08-02T11:00:00Z\n"
        "3,carmine-id,SOL,LONG,150,155,2026-08-03T10:00:00Z,2026-08-03T11:00:00Z\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match expected identity"):
        asyncio.run(
            identify_wallet_from_csv(
                path,
                expected_source_identity="bones-id",
            )
        )
