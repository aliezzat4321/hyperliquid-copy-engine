from __future__ import annotations

import pytest

from hlcopy.profitability import margin_snapshot_cli


def test_collector_refuses_real_trading(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "YES")
    with pytest.raises(SystemExit, match="refuses REAL_TRADING_ENABLED=YES"):
        import asyncio

        asyncio.run(margin_snapshot_cli._run(tmp_path / "margin.jsonl", ""))


def test_parser_default_output() -> None:
    args = margin_snapshot_cli.build_parser().parse_args([])
    assert str(args.output) == "data/research/margin_metadata.jsonl"
    assert args.dex == ""
