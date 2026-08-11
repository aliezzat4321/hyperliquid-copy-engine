from __future__ import annotations

from pathlib import Path

from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


def test_causal_provider_bounds_coin_cache(monkeypatch, tmp_path: Path) -> None:
    provider = CausalParquetL2BookProvider(tmp_path, max_cached_coins=2)

    def fake_parent_load(self, coin: str):
        # Mirror the base provider's cache contract without requiring parquet fixtures.
        books = []
        self._cache[coin] = books
        return books

    from hlcopy.shadow import evaluator

    monkeypatch.setattr(evaluator.ParquetL2BookProvider, "_load_coin", fake_parent_load)

    provider._load_coin("BTC")
    provider._received_ms_cache["BTC"] = ()
    provider._load_coin("ETH")
    provider._received_ms_cache["ETH"] = ()
    provider._load_coin("SOL")

    assert list(provider._cache) == ["ETH", "SOL"]
    assert "BTC" not in provider._received_ms_cache


def test_causal_provider_lru_refresh(monkeypatch, tmp_path: Path) -> None:
    provider = CausalParquetL2BookProvider(tmp_path, max_cached_coins=2)

    def fake_parent_load(self, coin: str):
        books = []
        self._cache[coin] = books
        return books

    from hlcopy.shadow import evaluator

    monkeypatch.setattr(evaluator.ParquetL2BookProvider, "_load_coin", fake_parent_load)

    provider._load_coin("BTC")
    provider._load_coin("ETH")
    provider._load_coin("BTC")
    provider._load_coin("SOL")

    assert list(provider._cache) == ["BTC", "SOL"]
