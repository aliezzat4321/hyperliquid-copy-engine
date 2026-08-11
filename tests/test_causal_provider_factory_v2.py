from pathlib import Path

from hlcopy.profitability import causal_position_live_cli


def test_shared_provider_is_reused(tmp_path: Path) -> None:
    causal_position_live_cli._PROVIDER_CACHE.clear()
    first = causal_position_live_cli._shared_causal_provider(tmp_path)
    second = causal_position_live_cli._shared_causal_provider(tmp_path)
    assert first is second
