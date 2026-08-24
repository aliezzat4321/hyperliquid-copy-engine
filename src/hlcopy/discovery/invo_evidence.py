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


def _iso8601(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _nonnegative_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric >= 0 else None


def closed_trade_evidence(event: InvoTradeEvent) -> dict[str, object] | None:
    """Convert a verified Invo close update to fail-closed resolver evidence."""
    if not event.verified_trade or event.is_open is not False:
        return None
    if event.entry_price is None or event.entry_price <= 0:
        return None
    if event.closing_price is None or event.closing_price <= 0:
        return None
    if event.leverage is None or event.leverage <= 0:
        return None
    if not event.portfolio_id or not event.post_id:
        return None

    # A post ID identifies one social update, not necessarily one source position. Only
    # stable Invo trade-level identifiers may contribute resolver evidence.
    trade_id = event.base_id or event.base_short_id
    if not trade_id:
        return None

    post = event.raw
    update = post.get("update")
    if not isinstance(update, Mapping):
        return None

    entry_size = _nonnegative_number(update.get("entrySize"))
    if entry_size is None:
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
        "trade_id": trade_id,
        "username": event.username or "unknown",
        "ticker": event.coin,
        "direction": event.direction,
        "leverage": event.leverage,
        "entry_price": event.entry_price,
        "closing_price": event.closing_price,
        "entry_size": entry_size,
        "opened_at": _iso8601(opened_at_ms),
        "closed_at": _iso8601(closed_at_ms),
        "portfolio_id": event.portfolio_id,
        "source_post_id": event.post_id,
    }
