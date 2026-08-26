from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hlcopy.discovery.invo_identifier_durable_job as durable_job


def _publication() -> dict[str, object]:
    return {
        "verified_count": 1,
        "identities": [{"username": "new-trader"}],
    }


def test_partial_batch_publishes_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[Path] = []

    async def fake_run_once(_args: object) -> dict[str, object]:
        raise RuntimeError("1 of 16 Invo wallet identification attempts failed")

    def fake_publish(*, state_dir: Path) -> dict[str, object]:
        calls.append(state_dir)
        return _publication()

    monkeypatch.setattr(
        durable_job,
        "_parse_args",
        lambda: SimpleNamespace(state_dir=tmp_path),
    )
    monkeypatch.setattr(durable_job, "run_once", fake_run_once)
    monkeypatch.setattr(durable_job, "publish_durable_verified_identities", fake_publish)

    assert asyncio.run(durable_job._main()) == 0
    assert calls == [tmp_path]
    payload = json.loads(capsys.readouterr().out)
    assert payload["identifier"]["attempted"] == 16
    assert payload["identifier"]["errors"] == 1
    assert payload["identifier"]["completed_without_error"] == 15
    assert payload["identifier"]["partial_failure"] is True
    assert payload["durable_verified_count"] == 1
    assert payload["durable_identity_usernames"] == ["new-trader"]


def test_complete_batch_failure_refreshes_publication_then_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    async def fake_run_once(_args: object) -> dict[str, object]:
        raise RuntimeError("16 of 16 Invo wallet identification attempts failed")

    def fake_publish(*, state_dir: Path) -> dict[str, object]:
        calls.append(state_dir)
        return _publication()

    monkeypatch.setattr(
        durable_job,
        "_parse_args",
        lambda: SimpleNamespace(state_dir=tmp_path),
    )
    monkeypatch.setattr(durable_job, "run_once", fake_run_once)
    monkeypatch.setattr(durable_job, "publish_durable_verified_identities", fake_publish)

    with pytest.raises(RuntimeError, match="16 of 16"):
        asyncio.run(durable_job._main())
    assert calls == [tmp_path]


def test_unknown_runtime_failure_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[Path] = []

    async def fake_run_once(_args: object) -> dict[str, object]:
        raise RuntimeError("unexpected SQD invariant failure")

    def fake_publish(*, state_dir: Path) -> dict[str, object]:
        calls.append(state_dir)
        return _publication()

    monkeypatch.setattr(
        durable_job,
        "_parse_args",
        lambda: SimpleNamespace(state_dir=tmp_path),
    )
    monkeypatch.setattr(durable_job, "run_once", fake_run_once)
    monkeypatch.setattr(durable_job, "publish_durable_verified_identities", fake_publish)

    with pytest.raises(RuntimeError, match="unexpected SQD invariant"):
        asyncio.run(durable_job._main())
    assert calls == [tmp_path]
