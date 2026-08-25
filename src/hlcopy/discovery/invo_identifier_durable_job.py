from __future__ import annotations

import asyncio
import json

from hlcopy.discovery.invo_durable_identity import publish_durable_verified_identities
from hlcopy.discovery.invo_identifier_job import _parse_args, run_once


async def _main() -> int:
    args = _parse_args()
    result = await run_once(args)
    publication = publish_durable_verified_identities(state_dir=args.state_dir)
    print(
        json.dumps(
            {
                "identifier": result,
                "durable_verified_count": publication["verified_count"],
                "durable_identity_usernames": [
                    row["username"] for row in publication["identities"]
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
