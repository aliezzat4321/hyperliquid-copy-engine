import hashlib
import json
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from hlcopy.profitability import lane1_audit_bundle as bundle
from hlcopy.profitability.position_copy import CopyFillEvent

D = Decimal
WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40


def _event(*, coin: str = "BTC", ts: int = 1_800_000_000_000) -> CopyFillEvent:
    return CopyFillEvent(
        lane="WIDE",
        wallet_id="wide",
        wallet_address=WALLET,
        coin=coin,
        exchange_ts_ms=ts,
        received_at_ns=ts * 1_000_000,
        tid=1,
        leader_start=D("0"),
        leader_after=D("1"),
        leader_delta=D("1"),
        source_price=D("100"),
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_market_rows(path: Path, received_at_ns: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "received_at_ns": received_at_ns,
            "exchange_ts_ms": [value // 1_000_000 for value in received_at_ns],
        }
    ).write_parquet(path)


def test_collect_targets_unions_and_deduplicates_roles() -> None:
    targets = bundle.collect_audit_targets(
        {
            "candidates": [
                {
                    "status": "challenger",
                    "wallet_address": WALLET.upper(),
                    "coin": "BTC",
                }
            ]
        },
        {
            "robust_candidates": [
                {"wallet_address": WALLET, "coin": "BTC"},
                {"wallet_address": OTHER, "coin": "ETH"},
            ]
        },
        {"targets": [{"wallet_address": WALLET, "coin": "BTC"}]},
    )

    assert [(row.wallet_address, row.coin, row.roles) for row in targets] == [
        (WALLET, "BTC", ("challenger", "prospective", "robust_oos")),
        (OTHER, "ETH", ("robust_oos",)),
    ]


def test_partition_copy_uses_hip3_wire_symbol_and_excludes_unrelated_coin(tmp_path: Path) -> None:
    day = "2027-01-15"
    market = tmp_path / "market"
    wanted = market / f"date={day}" / "coin=xyz:ZHIPU" / "channel=l2Book"
    unrelated = market / f"date={day}" / "coin=BTC" / "channel=l2Book"
    wanted.mkdir(parents=True)
    unrelated.mkdir(parents=True)
    (wanted / "wanted.parquet").write_bytes(b"wanted")
    (unrelated / "unrelated.parquet").write_bytes(b"unrelated")

    destination = tmp_path / "bundle"
    copied = bundle._copy_partition_files(
        market,
        destination,
        coin="XYZ:ZHIPU",
        channel="l2Book",
        dates=(day,),
    )

    expected = (
        destination
        / f"market/date={day}/coin=xyz:ZHIPU/channel=l2Book/wanted.parquet"
    )
    assert [path.name for path in copied] == ["wanted.parquet"]
    assert expected.exists()
    assert not list(destination.rglob("unrelated.parquet"))


def test_build_bundle_fails_closed_without_matching_l2(tmp_path: Path, monkeypatch) -> None:
    queue = tmp_path / "queue.json"
    funnel = tmp_path / "funnel.json"
    prospective = tmp_path / "prospective.json"
    _write_json(
        queue,
        {"candidates": [{"status": "challenger", "wallet_address": WALLET, "coin": "BTC"}]},
    )
    _write_json(funnel, {})
    _write_json(prospective, {})
    monkeypatch.setattr(bundle, "load_wide_events", lambda *_args, **_kwargs: [_event()])

    async def fake_funding(_ranges):
        return {"BTC": []}, {}

    monkeypatch.setattr(bundle, "fetch_official_funding_history", fake_funding)

    with pytest.raises(bundle.Lane1AuditBundleError, match="MISSING_L2_EVIDENCE"):
        bundle.build_lane1_audit_bundle(
            challenger_queue_path=queue,
            funnel_report_path=funnel,
            prospective_report_path=prospective,
            wide_enriched_dir=tmp_path / "wide",
            cutoff_ns=0,
            market_dir=tmp_path / "market",
            output_dir=tmp_path / "evidence",
            git_sha="abc123",
        )


def test_build_bundle_contains_exact_target_market_funding_events_and_hashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    start = 1_800_000_000_000
    end = ((start // bundle.HOUR_MS) + 1) * bundle.HOUR_MS + 1
    events = [_event(ts=start), _event(ts=end)]
    required = bundle._required_hourly_boundaries(start, end)
    assert required

    queue = tmp_path / "queue.json"
    funnel = tmp_path / "funnel.json"
    prospective = tmp_path / "prospective.json"
    _write_json(
        queue,
        {"candidates": [{"status": "challenger", "wallet_address": WALLET, "coin": "BTC"}]},
    )
    _write_json(funnel, {"robust_candidates": []})
    _write_json(prospective, {"targets": []})
    monkeypatch.setattr(bundle, "load_wide_events", lambda *_args, **_kwargs: events)

    async def fake_funding(_ranges):
        return {
            "BTC": [
                {"coin": "BTC", "time": boundary, "fundingRate": "0.0001"}
                for boundary in required
            ]
        }, {}

    monkeypatch.setattr(bundle, "fetch_official_funding_history", fake_funding)

    market = tmp_path / "market"
    event_received = [start * 1_000_000, end * 1_000_000]
    for day in bundle._utc_dates(start, end + 2_000):
        directory = market / f"date={day}" / "coin=BTC" / "channel=l2Book"
        _write_market_rows(directory / "l2.parquet", event_received)
    oracle_received = [(boundary - 1_000) * 1_000_000 for boundary in required]
    for day in bundle._utc_dates(max(0, start - 60_000), end + 2_000):
        directory = market / f"date={day}" / "coin=BTC" / "channel=activeAssetCtx"
        _write_market_rows(directory / "ctx.parquet", oracle_received)

    output = tmp_path / "evidence"
    manifest = bundle.build_lane1_audit_bundle(
        challenger_queue_path=queue,
        funnel_report_path=funnel,
        prospective_report_path=prospective,
        wide_enriched_dir=tmp_path / "wide",
        cutoff_ns=0,
        market_dir=market,
        output_dir=output,
        git_sha="abc123",
    )

    assert manifest["bundle_version"] == bundle.BUNDLE_VERSION
    assert manifest["git_sha"] == "abc123"
    assert manifest["target_count"] == 1
    target = manifest["targets"][0]
    assert target["required_hourly_funding_boundaries"] == list(required)
    assert target["l2_row_count"] > 0
    assert target["active_asset_ctx_row_count"] > 0
    event_lines = (output / "events/lane1_target_events.jsonl").read_text().splitlines()
    assert len(event_lines) == 2
    assert all(json.loads(line)["wallet_address"].lower() == WALLET for line in event_lines)
    funding_files = list((output / "funding_history").glob("*.json"))
    assert len(funding_files) == 1
    funding_payload = json.loads(funding_files[0].read_text())
    assert [row["time"] for row in funding_payload["rows"]] == list(required)

    on_disk = json.loads((output / "manifest.json").read_text())
    event_entry = next(
        row for row in on_disk["files"] if row["path"] == "events/lane1_target_events.jsonl"
    )
    raw = (output / event_entry["path"]).read_bytes()
    assert event_entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert event_entry["size_bytes"] == len(raw)


def test_build_bundle_fails_when_required_funding_boundary_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    start = 1_800_000_000_000
    end = ((start // bundle.HOUR_MS) + 1) * bundle.HOUR_MS + 1
    queue = tmp_path / "queue.json"
    funnel = tmp_path / "funnel.json"
    prospective = tmp_path / "prospective.json"
    _write_json(
        queue,
        {"candidates": [{"status": "challenger", "wallet_address": WALLET, "coin": "BTC"}]},
    )
    _write_json(funnel, {})
    _write_json(prospective, {})
    monkeypatch.setattr(
        bundle,
        "load_wide_events",
        lambda *_args, **_kwargs: [_event(ts=start), _event(ts=end)],
    )

    async def fake_funding(_ranges):
        return {"BTC": []}, {}

    monkeypatch.setattr(bundle, "fetch_official_funding_history", fake_funding)
    market = tmp_path / "market"
    event_received = [start * 1_000_000, end * 1_000_000]
    for day in bundle._utc_dates(start, end + 2_000):
        directory = market / f"date={day}" / "coin=BTC" / "channel=l2Book"
        _write_market_rows(directory / "l2.parquet", event_received)
    required = bundle._required_hourly_boundaries(start, end)
    oracle_received = [(boundary - 1_000) * 1_000_000 for boundary in required]
    for day in bundle._utc_dates(max(0, start - 60_000), end + 2_000):
        directory = market / f"date={day}" / "coin=BTC" / "channel=activeAssetCtx"
        _write_market_rows(directory / "ctx.parquet", oracle_received)

    with pytest.raises(bundle.Lane1AuditBundleError, match="MISSING_FUNDING_EVIDENCE"):
        bundle.build_lane1_audit_bundle(
            challenger_queue_path=queue,
            funnel_report_path=funnel,
            prospective_report_path=prospective,
            wide_enriched_dir=tmp_path / "wide",
            cutoff_ns=0,
            market_dir=market,
            output_dir=tmp_path / "evidence",
            git_sha="abc123",
        )
