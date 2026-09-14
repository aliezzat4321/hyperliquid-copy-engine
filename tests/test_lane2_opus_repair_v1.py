from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

from hlcopy.discovery import invo_identifier_durable_job, invo_identifier_job
from hlcopy.discovery.invo_identifier_durable_job import _run_legacy_resolver_without_publication


def test_durable_wrapper_never_exposes_legacy_publication(tmp_path: Path, monkeypatch) -> None:
    public_path = tmp_path / "identified_wallets.json"
    public_path.write_text(
        json.dumps({"contract": "durable-last-known-good"}),
        encoding="utf-8",
    )
    weak_payload = {
        "version": 1,
        "source": "invo",
        "verified_count": 1,
        "identities": [
            {"resolver_rule_version": "sqd-public-trade-v3-size-aware-sequence"}
        ],
    }

    async def fake_run_once(args: Namespace) -> dict[str, object]:
        invo_identifier_job._save_object(
            args.state_dir / "identified_wallets.json",
            weak_payload,
        )
        assert json.loads(public_path.read_text(encoding="utf-8")) == {
            "contract": "durable-last-known-good"
        }
        return {"attempted": 1}

    monkeypatch.setattr(invo_identifier_durable_job, "run_once", fake_run_once)
    result = asyncio.run(
        _run_legacy_resolver_without_publication(Namespace(state_dir=tmp_path))
    )

    assert result == {"attempted": 1}
    assert json.loads(public_path.read_text(encoding="utf-8")) == {
        "contract": "durable-last-known-good"
    }
    private = json.loads(
        (tmp_path / "identified_wallets_legacy_diagnostic.json").read_text(
            encoding="utf-8"
        )
    )
    assert private == weak_payload


def test_verified_shadow_sync_uses_same_pipeline_flock_as_identifier() -> None:
    root = Path(__file__).resolve().parents[1]
    identifier = (
        root / "deploy/systemd/hyperliquid-invo-wallet-identifier.service"
    ).read_text()
    sync = (
        root / "deploy/systemd/hyperliquid-invo-verified-shadow-sync.service"
    ).read_text()
    lock = "/run/hyperliquid-copy-engine/invo-pipeline.lock"
    assert lock in identifier
    assert lock in sync
    assert "/usr/bin/flock --wait 60" in sync
    assert "--reserved-invo-validation-slots 2" in sync
