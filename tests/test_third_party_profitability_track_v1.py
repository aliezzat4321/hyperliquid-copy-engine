from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlcopy.third_party.profitability_cli import (
    BASE_NOTIONAL,
    BASE_SCENARIO,
    _copyability_score,
    _robust_notionals,
    _status,
)
from hlcopy.third_party.registry_sync import sync_publications


def _publication(path: Path, identities: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "invo",
                "identities": identities,
            }
        ),
        encoding="utf-8",
    )


def test_verified_identity_enters_separate_research_registry_and_revokes_stale(
    tmp_path: Path,
) -> None:
    publication = tmp_path / "identified_wallets.json"
    registry = tmp_path / "third-party-wallets.json"
    _publication(
        publication,
        [
            {
                "portfolio_id": "bones-portfolio",
                "username": "bones",
                "wallet": "0x7a5973ca24c3d36cea16632711ac7a6cff684789",
                "evidence_sha256": "a" * 64,
                "resolver_rule_version": "resolver-v3",
            }
        ],
    )

    result = sync_publications(
        publications={"invo": publication},
        registry_path=registry,
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    wallet = payload["wallets"][0]
    assert result["enabled_wallets"] == 1
    assert result["stages"] == ["research"]
    assert wallet["stage"] == "research"
    assert wallet["enabled"] is True
    assert wallet["source_ref"] == "0x7a5973ca24c3d36cea16632711ac7a6cff684789"
    assert "third_party_source=invo" in wallet["notes"]
    assert result["safety"]["consumes_user_specific_websocket_slot"] is False

    _publication(publication, [])
    sync_publications(publications={"invo": publication}, registry_path=registry)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert payload["wallets"][0]["stage"] == "research"
    assert payload["wallets"][0]["enabled"] is False


def test_third_party_registry_refuses_real_trading(tmp_path: Path, monkeypatch) -> None:
    publication = tmp_path / "identified_wallets.json"
    _publication(publication, [])
    monkeypatch.setenv("REAL_TRADING_ENABLED", "YES")
    with pytest.raises(RuntimeError, match="REAL_TRADING_ENABLED"):
        sync_publications(
            publications={"invo": publication},
            registry_path=tmp_path / "wallets.json",
        )


def _scenario_rows(
    *,
    net_return_bps: str = "25",
    actions: int = 12,
    execution_pct: float = 90.0,
) -> list[dict[str, object]]:
    return [
        {
            "scenario": scenario,
            "notional_usd": str(BASE_NOTIONAL),
            "net_return_bps": net_return_bps,
            "realized_actions": actions,
            "execution_pct": execution_pct,
        }
        for scenario in ("LIVE_100MS", "LIVE_250MS", "LIVE_500MS", "LIVE_1000MS")
    ]


def test_copyability_requires_latency_and_execution_robustness() -> None:
    rows = _scenario_rows()
    robust = _robust_notionals(rows)
    score, components = _copyability_score(
        base_rows=rows,
        robust_notionals=robust,
    )
    base = next(row for row in rows if row["scenario"] == BASE_SCENARIO.name)
    assert robust[0]["notional_usd"] == str(BASE_NOTIONAL)
    assert components["execution"] == 90.0
    assert components["latency_robustness"] == 100.0
    assert score > 70
    assert (
        _status(
            event_count=50,
            base_500ms=base,
            base_rows=rows,
            robust=robust,
        )
        == "ROBUST_DEVELOPING"
    )

    weak = _scenario_rows(net_return_bps="-5")
    weak_base = next(row for row in weak if row["scenario"] == BASE_SCENARIO.name)
    assert _robust_notionals(weak) == []
    assert (
        _status(
            event_count=50,
            base_500ms=weak_base,
            base_rows=weak,
            robust=[],
        )
        == "NEGATIVE_AT_BASE"
    )


def test_third_party_ops_use_root_disk_and_no_pull_request_deploy() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/deploy-third-party-track.yml").read_text()
    deploy = (root / "scripts/deploy_third_party_track_self_hosted.sh").read_text()
    watcher = (
        root / "deploy/systemd/hyperliquid-third-party-wide-watch.service"
    ).read_text()
    enricher = (
        root / "deploy/systemd/hyperliquid-third-party-wide-enrichment.service"
    ).read_text()

    assert "pull_request:" not in workflow
    assert "self-hosted" in workflow
    assert "hyperliquid" in workflow
    assert "persist-credentials: false" in workflow
    assert "git merge --ff-only origin/main" in deploy
    assert "/var/lib/hyperliquid-copy-engine/third-party/wide-trades" in watcher
    assert "/var/lib/hyperliquid-copy-engine/third-party/wide-enriched" in enricher
    assert "REAL_TRADING_ENABLED=NO" in watcher
    assert "REAL_TRADING_ENABLED=NO" in enricher
