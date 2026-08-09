from __future__ import annotations

from hlcopy.hyperliquid.http_client import ApiResponse
from hlcopy.profiling import _fill_history_cap_hit, _merge_fill_pages

WALLET = "0x1111111111111111111111111111111111111111"


def test_merge_fill_pages_deduplicates_and_keeps_perps_only():
    perp = {
        "time": 100,
        "tid": 1,
        "hash": "0xabc",
        "dir": "Open Long",
        "coin": "BTC",
    }
    spot = {
        "time": 101,
        "tid": 2,
        "hash": "0xdef",
        "dir": "Buy",
        "coin": "PURR/USDC",
    }
    pages = [
        ApiResponse("info", {"type": "userFillsByTime", "user": WALLET}, [perp, spot], 1),
        ApiResponse("info", {"type": "userFillsByTime", "user": WALLET}, [perp], 2),
    ]

    assert _merge_fill_pages(pages) == [perp]
    assert not _fill_history_cap_hit(pages)


def test_merge_fill_pages_preserves_source_order_within_same_timestamp():
    first = {
        "time": 100,
        "tid": 300,
        "hash": "0x300",
        "dir": "Open Long",
        "coin": "BTC",
    }
    second = {
        "time": 100,
        "tid": 100,
        "hash": "0x100",
        "dir": "Add Long",
        "coin": "BTC",
    }
    later = {
        "time": 101,
        "tid": 200,
        "hash": "0x200",
        "dir": "Close Long",
        "coin": "BTC",
    }
    pages = [
        ApiResponse(
            "info",
            {"type": "userFillsByTime", "user": WALLET},
            [first, second, later],
            1,
        )
    ]

    assert _merge_fill_pages(pages) == [first, second, later]
