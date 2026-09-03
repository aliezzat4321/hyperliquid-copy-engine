from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import httpx

from hlcopy.discovery import invo_identifier_durable_job, invo_identifier_job
from hlcopy.resolver.identifier import WalletIdentificationResult
from hlcopy.resolver.sqd_fills import SqdHyperliquidFillsClient


class _FakeClient:
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.request_count = 9
        self.request_latency_ms = 18.5
        self.retry_count = 1
        self.request_metrics_by_owner = {
            "portfolio-0": {
                "query_count": 5,
                "query_latency_ms": 10.0,
                "retry_count": 1,
            },
            "portfolio-1": {
                "query_count": 4,
                "query_latency_ms": 8.5,
                "retry_count": 0,
            },
        }

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def _result(wallet: str) -> WalletIdentificationResult:
    return WalletIdentificationResult(
        status="VERIFIED",
        wallet=wallet,
        candidate=wallet,
        confidence=Decimal("0.75"),
        input_trades=20,
        rejected_rows=0,
        discovery_matches=4,
        discovery_anchors=8,
        candidate_unique=True,
        historical_matches=5,
        historical_attempted=12,
        verification_source="sqd_finalized_fills",
        median_clock_offset_ms=1000.0,
        median_price_bps=Decimal("1.5"),
        report_path=None,
    )


def test_identifier_runs_portfolios_concurrently_but_bounded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_dir = tmp_path / "invo"
    _FakeClient.instances = 0
    queue_dir = state_dir / "resolution_queue"
    queue_dir.mkdir(parents=True)
    queue = []
    for index in range(6):
        evidence = queue_dir / f"trader-{index}.csv"
        evidence.write_text(f"evidence-{index}", encoding="utf-8")
        queue.append(
            {
                "portfolio_id": f"portfolio-{index}",
                "username": f"trader-{index}",
                "resolver_csv": str(evidence),
                "evidence_count": 50 - index,
                "distinct_coin_count": 8,
            }
        )
    (queue_dir / "resolution_queue.json").write_text(
        json.dumps({"queue": queue}),
        encoding="utf-8",
    )

    active = 0
    peak = 0

    async def fake_identify(path: Path, **_: object) -> WalletIdentificationResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        index = int(path.stem.split("-")[-1])
        return _result("0x" + f"{index + 1:040x}")

    monkeypatch.setattr(invo_identifier_job, "SqdHyperliquidFillsClient", _FakeClient)
    monkeypatch.setattr(invo_identifier_job, "identify_wallet_from_csv", fake_identify)
    args = Namespace(
        state_dir=state_dir,
        max_portfolios=6,
        concurrency=2,
        priority_trader=[],
        unresolved_retry_minutes=60,
    )

    result = asyncio.run(invo_identifier_job.run_once(args))

    assert result["attempted"] == 6
    assert result["verified"] == 6
    assert result["errors"] == 0
    assert result["concurrency"] == 2
    assert peak == 2
    assert _FakeClient.instances == 1
    assert result["api"]["query_count"] == 9
    assert result["api"]["by_portfolio"]["portfolio-0"] == {
        "query_count": 5,
        "query_latency_ms": 10.0,
        "retry_count": 1,
    }
    assert result["api"]["by_portfolio"]["portfolio-5"]["query_count"] == 0
    assert result["queue_backlog"] == 0
    assert result["portfolio_latency_ms"]["p50"] is not None
    assert result["time_to_first_candidate_ms"]["p99"] is not None
    assert result["time_to_verified_identity_ms"]["p90"] is not None
    assert result["verified_yield"] == 1.0


def test_sqd_request_metrics_are_attributed_across_concurrent_portfolios() -> None:
    async def exercise() -> SqdHyperliquidFillsClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            return httpx.Response(204, request=request)

        client = SqdHyperliquidFillsClient()
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with client:
            async def request_for(portfolio_id: str) -> None:
                with client.request_scope(portfolio_id):
                    await client._request("GET", "probe", retries=0)

            await asyncio.gather(request_for("portfolio-a"), request_for("portfolio-b"))
        return client

    client = asyncio.run(exercise())

    assert client.request_count == 2
    assert client.request_metrics_by_owner["portfolio-a"]["query_count"] == 1
    assert client.request_metrics_by_owner["portfolio-b"]["query_count"] == 1
    attributed_latency = sum(
        float(metrics["query_latency_ms"])
        for metrics in client.request_metrics_by_owner.values()
    )
    assert attributed_latency == client.request_latency_ms


def test_production_identifier_uses_wide_bounded_batch() -> None:
    service = Path(
        "deploy/systemd/hyperliquid-invo-wallet-identifier.service"
    ).read_text(encoding="utf-8")

    assert "--max-portfolios 64" in service
    assert "--concurrency 4" in service
    assert "--unresolved-retry-minutes 60" in service
    assert "REAL_TRADING_ENABLED=NO" in service


def test_durable_wrapper_publishes_after_partial_portfolio_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = Namespace(state_dir=tmp_path)
    publication_calls = 0

    async def fake_run_once(_: Namespace) -> dict[str, object]:
        raise invo_identifier_job.PortfolioResolutionBatchError(
            "1 of 4 Invo wallet identification attempts failed",
            summary={
                "attempted": 4,
                "verified": 2,
                "unresolved": 1,
                "errors": 1,
                "partial_failure": True,
            },
        )

    def fake_publish(*, state_dir: Path) -> dict[str, object]:
        nonlocal publication_calls
        assert state_dir == tmp_path
        publication_calls += 1
        return {
            "verified_count": 2,
            "identities": [
                {"username": "alpha"},
                {"username": "beta"},
            ],
        }

    monkeypatch.setattr(invo_identifier_durable_job, "_parse_args", lambda: args)
    monkeypatch.setattr(invo_identifier_durable_job, "run_once", fake_run_once)
    monkeypatch.setattr(
        invo_identifier_durable_job,
        "publish_durable_verified_identities",
        fake_publish,
    )

    assert asyncio.run(invo_identifier_durable_job._main()) == 0
    assert publication_calls == 1
