import asyncio

import pytest

from hlcopy.profitability import margin_snapshot_cli


def test_collector_refuses_real_trading(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "YES")
    with pytest.raises(SystemExit, match="refuses REAL_TRADING_ENABLED=YES"):
        asyncio.run(margin_snapshot_cli._run(tmp_path / "margin.jsonl", ""))
