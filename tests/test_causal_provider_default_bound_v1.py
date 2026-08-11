from hlcopy.profitability.causal_book import CausalParquetL2BookProvider


def test_default_coin_cache_bound(tmp_path) -> None:
    provider = CausalParquetL2BookProvider(tmp_path)
    assert provider.max_cached_coins == 4
