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
