from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from hlcopy.discovery.invo_source import InvoTradeEvent


def _timestamp_ms(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = int(value)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        numeric = int(text)
        return numeric if numeric > 10_000_000_000 else numeric * 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def closed_trade_evidence(event: InvoTradeEvent) -> dict[str, object] | None:
    """Convert a verified Invo close update to fail-closed resolver evidence."""
    if not event.verified_trade or event.is_open is not False:
        return None
    if event.entry_price is None or event.entry_price <= 0:
        return None
    if event.closing_price is None or event.closing_price <= 0:
        return None
    if not event.portfolio_id or not event.post_id:
        return None

    post = event.raw
    update = post.get("update")
    if not isinstance(update, Mapping):
        return None

    opened_at_ms = _timestamp_ms(update.get("createdAt"))
    if opened_at_ms is None:
        return None
    close_candidates = (
        _timestamp_ms(update.get("updatedAt")),
        _timestamp_ms(post.get("updatedAt")),
        _timestamp_ms(post.get("createdAt")),
    )
    closed_at_ms = next(
        (value for value in close_candidates if value is not None and value > opened_at_ms),
        None,
    )
    if closed_at_ms is None:
        return None

    return {
        "trade_id": event.base_id or event.base_short_id or event.post_id,
        "username": event.username or "unknown",
        "ticker": event.coin,
        "direction": event.direction,
        "leverage": event.leverage if event.leverage is not None else "",
        "entry_price": event.entry_price,
        "closing_price": event.closing_price,
        "opened_at": opened_at_ms,
        "closed_at": closed_at_ms,
        "portfolio_id": event.portfolio_id,
        "source_post_id": event.post_id,
    }
