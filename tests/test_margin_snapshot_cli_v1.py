from __future__ import annotations

import asyncio

import pytest

from hlcopy.profitability import margin_snapshot_cli


def test_collector_refuses_real_trading(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "YES")
    with pytest.raises(SystemExit, match="refuses REAL_TRADING_ENABLED=YES"):
        asyncio.run(margin_snapshot_cli._run(tmp_path / "margin.jsonl", ""))


def test_collector_refuses_dex_plus_all_dexes_before_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("REAL_TRADING_ENABLED", "NO")

    class NetworkMustNotBeCreated:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("invalid CLI combination must fail before HTTP client creation")

    monkeypatch.setattr(margin_snapshot_cli, "HyperliquidHttpClient", NetworkMustNotBeCreated)
    with pytest.raises(SystemExit, match="mutually exclusive"):
        asyncio.run(
            margin_snapshot_cli._run(
                tmp_path / "margin.jsonl",
                "hip3-test",
                all_dexes=True,
            )
        )
    assert not (tmp_path / "margin.jsonl").exists()


def test_dex_name_discovery_includes_default_and_hip3() -> None:
    payload = [None, {"name": "xyz"}, {"name": "para"}, {"name": "xyz"}]
    assert margin_snapshot_cli._dex_names(payload) == ("", "xyz", "para")


def test_parser_default_output() -> None:
    args = margin_snapshot_cli.build_parser().parse_args([])
    assert str(args.output) == "data/research/margin_metadata.jsonl"
    assert args.dex == ""
    assert args.all_dexes is False
