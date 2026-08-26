from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from hlcopy.discovery.invo_durable_identity import publish_durable_verified_identities
from hlcopy.discovery.invo_identifier_job import _parse_args, run_once

_BATCH_FAILURE_RE = re.compile(
    r"^(?P<errors>\d+) of (?P<attempted>\d+) "
    r"Invo wallet identification attempts failed$"
)


def _partial_batch_result(exc: RuntimeError) -> dict[str, Any] | None:
    match = _BATCH_FAILURE_RE.fullmatch(str(exc).strip())
    if match is None:
        return None
    errors = int(match.group("errors"))
    attempted = int(match.group("attempted"))
    if attempted <= 0 or errors <= 0 or errors >= attempted:
        return None
    return {
        "attempted": attempted,
        "errors": errors,
        "completed_without_error": attempted - errors,
        "partial_failure": True,
        "failure": str(exc),
    }


def _output(*, result: dict[str, Any], publication: dict[str, Any]) -> dict[str, Any]:
    return {
        "identifier": result,
        "durable_verified_count": publication["verified_count"],
        "durable_identity_usernames": [
            row["username"]
            for row in publication["identities"]
            if isinstance(row, dict) and row.get("username")
        ],
    }


async def _main() -> int:
    args = _parse_args()
    try:
        result = await run_once(args)
    except RuntimeError as exc:
        partial = _partial_batch_result(exc)
        if partial is None:
            # Preserve the failing service result for complete/unknown failures.
            # Publication is still refreshed first, and its current-evidence SHA
            # gate prevents stale proofs from surviving this path.
            publish_durable_verified_identities(state_dir=args.state_dir)
            raise
        result = partial

    # A transient failure in one portfolio must not prevent successful portfolios
    # in the same bounded batch from reaching scoring/shadow research.
    publication = publish_durable_verified_identities(state_dir=args.state_dir)
    print(json.dumps(_output(result=result, publication=publication), sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
