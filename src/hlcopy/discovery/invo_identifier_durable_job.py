from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hlcopy.discovery.invo_durable_identity import publish_durable_verified_identities
from hlcopy.discovery.invo_identifier_job import (
    PortfolioResolutionBatchError,
    _parse_args,
    run_once,
)


def _persist_measurement(
    *,
    state_dir: Path,
    payload: dict[str, object],
    append_history: bool = True,
) -> None:
    """Persist lane-specific acceptance evidence independently of generic runners."""
    evidence_dir = state_dir / "lane2_measurements"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, sort_keys=True)

    latest = evidence_dir / "latest.json"
    temporary = latest.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(rendered + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(latest)

    if append_history:
        history = evidence_dir / "runs.ndjson"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    directory_fd = os.open(evidence_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _main() -> int:
    args = _parse_args()
    run_id = uuid4().hex
    started_at = datetime.now(tz=UTC).isoformat()
    common: dict[str, object] = {
        "schema": "hlcopy-lane2-resolution-measurement/v1",
        "run_id": run_id,
        "started_at": started_at,
        "real_trading_enabled": False,
    }
    # Publish a fresh lifecycle record before resolver/network work. The generic
    # acceptance runner is not required for operators to distinguish an active
    # Lane 2 run from a service that never started.
    _persist_measurement(
        state_dir=args.state_dir,
        payload={
            **common,
            "status": "STARTED",
            "observed_at": started_at,
        },
        append_history=False,
    )
    result: dict[str, object] | None = None
    try:
        try:
            result = await run_once(args)
        except PortfolioResolutionBatchError as exc:
            # Individual portfolio failures are already persisted as ERROR and are
            # never published as identities. Do not hold successful verified
            # portfolios back from the durable scoring/shadow handoff.
            result = exc.summary
        publication = publish_durable_verified_identities(state_dir=args.state_dir)
    except Exception as exc:
        failed_at = datetime.now(tz=UTC).isoformat()
        failure: dict[str, object] = {
            **common,
            "status": "FAILED",
            "observed_at": failed_at,
            "completed_at": failed_at,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        if result is not None:
            # Preserve the full timing/yield funnel when only the downstream
            # durable-publication step failed.
            failure["identifier"] = result
        _persist_measurement(
            state_dir=args.state_dir,
            payload=failure,
        )
        raise

    completed_at = datetime.now(tz=UTC).isoformat()
    measurement: dict[str, object] = {
        **common,
        "status": "COMPLETED",
        "observed_at": completed_at,
        "completed_at": completed_at,
        "identifier": result,
        "durable_verified_count": publication["verified_count"],
        "durable_identity_usernames": [
            row["username"] for row in publication["identities"]
        ],
    }
    _persist_measurement(state_dir=args.state_dir, payload=measurement)
    print(json.dumps(measurement, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
