import json
from decimal import Decimal
from pathlib import Path

from hlcopy.market.historical_archive import LocalArchiveBookProvider, parse_l2_snapshot


def _payload(time_ms: int):
    return {
        "channel": "l2Book",
        "data": {
            "coin": "BTC",
            "time": time_ms,
            "levels": [
                [{"px": "100", "sz": "2"}, {"px": "99", "sz": "3"}],
                [{"px": "101", "sz": "2"}, {"px": "102", "sz": "3"}],
            ],
        },
    }


def test_parse_archive_snapshot():
    snapshot = parse_l2_snapshot(_payload(1_000))
    assert snapshot is not None
    assert snapshot.bids[0].price == Decimal("100")
    assert snapshot.asks[0].price == Decimal("101")
    assert snapshot.mid == Decimal("100.5")


def test_provider_uses_latest_nonfuture_snapshot(tmp_path: Path):
    target = tmp_path / "20260807" / "14" / "l2Book"
    target.mkdir(parents=True)
    (target / "BTC").write_text(
        "\n".join(json.dumps(_payload(t)) for t in [1_786_112_999_000, 1_786_112_999_500])
        + "\n",
        encoding="utf-8",
    )
    provider = LocalArchiveBookProvider(tmp_path, max_book_age_ms=1_000)
    snapshot = provider.snapshot_at_or_before("BTC", 1_786_112_999_800)
    assert snapshot is not None
    assert snapshot.timestamp_ms == 1_786_112_999_500
