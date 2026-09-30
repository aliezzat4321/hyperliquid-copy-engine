import json
from decimal import Decimal
from pathlib import Path

from hlcopy.profitability import lane1_audit_bundle as bundle
from hlcopy.profitability.position_copy import CopyFillEvent

D = Decimal
WALLET_A = "0x" + "a" * 40
WALLET_B = "0x" + "b" * 40


def _event(wallet: str, ts: int, tid: int) -> CopyFillEvent:
    return CopyFillEvent(
        lane="WIDE",
        wallet_id="wide",
        wallet_address=wallet,
        coin="BTC",
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        tid=tid,
        leader_start=D("0"),
        leader_after=D("1"),
        leader_delta=D("1"),
        source_price=D("100"),
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_same_coin_targets_get_distinct_funding_evidence(tmp_path: Path, monkeypatch) -> None:
    start_a = 1_800_000_000_000
    boundary_a = ((start_a // bundle.HOUR_MS) + 1) * bundle.HOUR_MS
    end_a = boundary_a + 1
    start_b = boundary_a + 1_000
    boundary_b = ((start_b // bundle.HOUR_MS) + 1) * bundle.HOUR_MS
    end_b = boundary_b + 1

    events = [
        _event(WALLET_A, start_a, 1),
        _event(WALLET_A, end_a, 2),
        _event(WALLET_B, start_b, 3),
        _event(WALLET_B, end_b, 4),
    ]

    queue = tmp_path / "queue.json"
    funnel = tmp_path / "funnel.json"
    prospective = tmp_path / "prospective.json"
    _write_json(
        queue,
        {
            "candidates": [
                {"status": "challenger", "wallet_address": WALLET_A, "coin": "BTC"},
                {"status": "challenger", "wallet_address": WALLET_B, "coin": "BTC"},
            ]
        },
    )
    _write_json(funnel, {"robust_candidates": []})
    _write_json(prospective, {"targets": []})

    monkeypatch.setattr(bundle, "load_wide_events", lambda *_args, **_kwargs: events)

    async def fake_funding(_ranges):
        return {
            "BTC": [
                {"coin": "BTC", "time": boundary_a, "fundingRate": "0.0001"},
                {"coin": "BTC", "time": boundary_b, "fundingRate": "0.0002"},
            ]
        }, {}

    monkeypatch.setattr(bundle, "fetch_official_funding_history", fake_funding)

    def fake_extract(_market_dir, destination, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audit")
        return destination, 1

    monkeypatch.setattr(bundle, "extract_market_window_rows", fake_extract)

    output = tmp_path / "evidence"
    manifest = bundle.build_lane1_audit_bundle(
        challenger_queue_path=queue,
        funnel_report_path=funnel,
        prospective_report_path=prospective,
        wide_enriched_dir=tmp_path / "wide",
        cutoff_ns=0,
        market_dir=tmp_path / "market",
        output_dir=output,
        git_sha="deadbeef",
    )

    assert manifest["bundle_version"] == "LANE1_REPLAY_EVIDENCE_V3_TARGET_SCOPED_FUNDING"
    assert manifest["target_count"] == 2

    targets = manifest["targets"]
    funding_paths = [target["funding_history_path"] for target in targets]
    assert len(set(funding_paths)) == 2
    assert all(path.startswith("funding_history/target=") for path in funding_paths)

    for target in targets:
        funding_path = output / target["funding_history_path"]
        assert funding_path.exists()
        payload = json.loads(funding_path.read_text(encoding="utf-8"))
        assert payload["target_id"] == target["target_id"]
        assert payload["wallet_address"] == target["wallet_address"]
        assert payload["coin"] == target["coin"]
        assert payload["start_ms"] == target["start_exchange_ts_ms"]
        assert payload["end_ms"] == target["end_exchange_ts_ms"]
        assert payload["required_hourly_boundaries"] == target[
            "required_hourly_funding_boundaries"
        ]

    on_disk = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    listed_paths = {entry["path"] for entry in on_disk["files"]}
    assert set(funding_paths).issubset(listed_paths)
