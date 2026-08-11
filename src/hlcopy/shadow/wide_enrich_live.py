from __future__ import annotations

import time
from typing import Any

from hlcopy.shadow.wide_enrich import WideTradeOfficialEnricher


class ProspectiveWideTradeOfficialEnricher(WideTradeOfficialEnricher):
    """Enrich only evidence that was observable prospectively by this process.

    Existing wide-trade files may contain subscription replay/backfill from an older
    collector build. Skipping pre-start rows lets the persistent checkpoint advance
    without turning historical rows into fake live latency. A second age guard keeps
    REST enrichment from building an unbounded queue during bursts.
    """

    def __init__(
        self,
        *args: Any,
        max_event_age_ms: float = 10_000.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.started_ns = time.time_ns()
        self.max_event_age_ms = max(0.0, float(max_event_age_ms))
        self.skipped_prestart = 0
        self.skipped_stale = 0

    async def enrich(self, event: dict[str, Any]) -> dict[str, Any]:
        try:
            public_received_at_ns = int(event["received_at_ns"])
        except (KeyError, TypeError, ValueError):
            return await super().enrich(event)

        if public_received_at_ns < self.started_ns:
            self.skipped_prestart += 1
            return {
                "kind": "wide_official_fill_skipped",
                "reason": "PRESTART_BACKFILL",
                "wallet_address": str(event.get("wallet_address") or "").lower(),
                "coin": event.get("coin"),
                "tid": event.get("tid"),
            }

        age_ms = (time.time_ns() - public_received_at_ns) / 1_000_000
        if age_ms > self.max_event_age_ms:
            self.skipped_stale += 1
            return {
                "kind": "wide_official_fill_skipped",
                "reason": "ENRICHMENT_QUEUE_STALE",
                "wallet_address": str(event.get("wallet_address") or "").lower(),
                "coin": event.get("coin"),
                "tid": event.get("tid"),
                "age_ms": age_ms,
            }

        return await super().enrich(event)
