import json
from pathlib import Path

import pytest

from hlcopy.profitability.memory_bounded_wide import WideEventStream
from hlcopy.profitability.position_copy import load_wide_events


WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _fill(*, tid: int, time_ms: int, coin: str) -> dict[str, object]:
    return {
        "tid": tid,
        "time": time_ms,
        "coin": coin,
        "side": "B",
        "px": "100",
        "sz": "1",
        "startPosition": "0",
        "closedPnl": "0",
        "fee": "0",
    }


def _row(
    *,
    wallet: str,
    tid: int,
    time_ms: int,
    received_ns: int,
    coin: str,
) -> dict[str, object]:
    return {
        "kind": "wide_official_fill",
        "wallet_id": wallet,
        "wallet_address": wallet,
        "public_received_at_ns": received_ns,
        "official_fill": _fill(tid=tid, time_ms=time_ms, coin=coin),
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _sort_key(event):
    return (event.exchange_ts_ms, event.wallet_address, event.coin, event.tid)


def test_memory_bounded_stream_matches_legacy_loader_on_fixture(tmp_path: Path) -> None:
    rows_a = [
        _row(wallet=WALLET_B, tid=3, time_ms=3000, received_ns=300, coin="ETH"),
        _row(wallet=WALLET_A, tid=1, time_ms=1000, received_ns=100, coin="BTC"),
    ]
    rows_b = [
        _row(wallet=WALLET_A, tid=2, time_ms=2000, received_ns=200, coin="BTC"),
        _row(wallet=WALLET_A, tid=2, time_ms=2000, received_ns=201, coin="BTC"),
        {"kind": "wide_official_fill", "public_received_at_ns": 250, "official_fill": "bad"},
        {"kind": "other"},
    ]
    _write_jsonl(tmp_path / "b.jsonl", rows_b)
    _write_jsonl(tmp_path / "a.jsonl", rows_a)

    expected = load_wide_events(tmp_path, cutoff_ns=100)
    stream = WideEventStream(tmp_path, cutoff_ns=100)
    actual = list(stream)

    assert sorted(actual, key=_sort_key) == list(expected)
    assert len(stream) == len(expected)
    with pytest.raises(RuntimeError, match="single-pass"):
        list(stream)


def test_target_filtered_stream_is_line_streaming_and_excludes_unrelated_rows(
    tmp_path: Path, monkeypatch
) -> None:
    _write_jsonl(
        tmp_path / "events.jsonl",
        [
            _row(wallet=WALLET_A, tid=1, time_ms=1000, received_ns=99, coin="BTC"),
            _row(wallet=WALLET_A, tid=2, time_ms=2000, received_ns=200, coin="BTC"),
            _row(wallet=WALLET_B, tid=3, time_ms=3000, received_ns=300, coin="BTC"),
            _row(wallet=WALLET_A, tid=4, time_ms=4000, received_ns=400, coin="ETH"),
        ],
    )

    original_read_text = Path.read_text

    def fail_jsonl_read_text(self, *args, **kwargs):
        if self.suffix == ".jsonl":
            raise AssertionError("wide JSONL must be streamed, not read_text().splitlines()")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_jsonl_read_text)
    stream = WideEventStream(
        tmp_path,
        cutoff_ns=100,
        target_keys={(WALLET_A, "BTC")},
    )
    events = list(stream)

    assert [(event.wallet_address, event.coin, event.tid) for event in events] == [
        (WALLET_A, "BTC", 2)
    ]
    assert len(stream) == 1
