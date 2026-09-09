from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from hlcopy.discovery.invo_durable_identity import publish_durable_verified_identities
from hlcopy.discovery.invo_identifier_job import (
    PortfolioResolutionBatchError,
    _parse_args,
    run_once,
)


def _persist_measurement(*, state_dir: Path, payload: dict[str, object]) -> None:
    """Persist lane-specific acceptance evidence independently of generic runners."""
    evidence_dir = state_dir / "lane2_measurements"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, sort_keys=True)

    latest = evidence_dir / "latest.json"
    temporary = latest.with_suffix(".json.tmp")
    temporary.write_text(rendered + "\n", encoding="utf-8")
    temporary.replace(latest)

    history = evidence_dir / "runs.ndjson"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(rendered + "\n")
        handle.flush()
        os.fsync(handle.fileno())


async def _main() -> int:
    args = _parse_args()
    started_at = datetime.now(tz=UTC)
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
        # The lane-specific runner is also the operational evidence source. Persist a
        # precise blocker before failing the service so a broken queue, publication,
        # or dependency cannot degrade into an unobservable generic-runner timeout.
        failed: dict[str, object] = {
            "schema": "hlcopy-lane2-resolution-measurement/v1",
            "run_started_at": started_at.isoformat(),
            "observed_at": datetime.now(tz=UTC).isoformat(),
            "status": "RUNTIME_BLOCKED",
            "runtime_blocker": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "real_trading_enabled": False,
        }
        _persist_measurement(state_dir=args.state_dir, payload=failed)
        print(json.dumps(failed, sort_keys=True))
        raise

    measurement: dict[str, object] = {
        "schema": "hlcopy-lane2-resolution-measurement/v1",
        "run_started_at": started_at.isoformat(),
        "observed_at": datetime.now(tz=UTC).isoformat(),
        "status": "PARTIAL_FAILURE" if result.get("partial_failure") else "COMPLETE",
        "identifier": result,
        "durable_verified_count": publication["verified_count"],
        "durable_identity_usernames": [
            row["username"] for row in publication["identities"]
        ],
        "real_trading_enabled": False,
    }
    _persist_measurement(state_dir=args.state_dir, payload=measurement)
    print(json.dumps(measurement, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
