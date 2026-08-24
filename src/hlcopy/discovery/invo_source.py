from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


INVO_API_BASE = "https://api.invoapp.com"
DEFAULT_INVO_APP_VERSION = "0.0.75"


class InvoApiError(RuntimeError):
    pass


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


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


@dataclass(frozen=True, slots=True)
class InvoPortfolioCandidate:
    portfolio_id: str
    owner_id: str
    username: str
    name: str | None
    closed_positions: int
    won_positions: int
    lost_positions: int
    win_rate: float
    percent_change: float
    current_win_streak: int
    follower_count: int
    created_at_ms: int | None
    liquidated: bool
    raw: Mapping[str, Any]

    @property
    def days_active(self) -> float | None:
        if self.created_at_ms is None:
            return None
        elapsed_ms = datetime.now(tz=UTC).timestamp() * 1000 - self.created_at_ms
        return max(0.0, elapsed_ms / 86_400_000)

    @property
    def win_loss_ratio(self) -> float:
        if self.lost_positions <= 0:
            return float(self.won_positions)
        return self.won_positions / self.lost_positions

    def to_dict(self) -> dict[str, object]:
        return {
            "portfolio_id": self.portfolio_id,
            "owner_id": self.owner_id,
            "username": self.username,
            "name": self.name,
            "closed_positions": self.closed_positions,
            "won_positions": self.won_positions,
            "lost_positions": self.lost_positions,
            "win_rate": self.win_rate,
            "percent_change": self.percent_change,
            "current_win_streak": self.current_win_streak,
            "follower_count": self.follower_count,
            "created_at_ms": self.created_at_ms,
            "liquidated": self.liquidated,
            "win_loss_ratio": self.win_loss_ratio,
        }


@dataclass(frozen=True, slots=True)
class InvoTradeEvent:
    post_id: str
    portfolio_id: str | None
    owner_id: str | None
    username: str | None
    base_id: str | None
    base_short_id: str | None
    coin: str
    direction: str
    leverage: float | None
    entry_price: float | None
    closing_price: float | None
    is_open: bool | None
    verified_trade: bool
    occurred_at_ms: int | None
    raw: Mapping[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {
            "post_id": self.post_id,
            "portfolio_id": self.portfolio_id,
            "owner_id": self.owner_id,
            "username": self.username,
            "base_id": self.base_id,
            "base_short_id": self.base_short_id,
            "coin": self.coin,
            "direction": self.direction,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "closing_price": self.closing_price,
            "is_open": self.is_open,
            "verified_trade": self.verified_trade,
            "occurred_at_ms": self.occurred_at_ms,
        }


def normalize_portfolio_candidate(
    row: Mapping[str, Any],
) -> InvoPortfolioCandidate | None:
    portfolio_id = str(row.get("id") or "").strip()
    owner_obj = row.get("owner") if isinstance(row.get("owner"), Mapping) else {}
    owner_id = str(row.get("ownerId") or owner_obj.get("id") or "").strip()
    if not portfolio_id or not owner_id:
        return None
    return InvoPortfolioCandidate(
        portfolio_id=portfolio_id,
        owner_id=owner_id,
        username=str(owner_obj.get("username") or row.get("username") or "unknown"),
        name=(str(row.get("name")).strip() if row.get("name") is not None else None),
        closed_positions=_as_int(row.get("closedPositions")),
        won_positions=_as_int(row.get("wonPositions")),
        lost_positions=_as_int(row.get("lostPositions")),
        win_rate=_as_float(row.get("winRate")),
        percent_change=_as_float(row.get("percentChange")),
        current_win_streak=_as_int(row.get("currentWinStreak")),
        follower_count=_as_int(row.get("followerCount")),
        created_at_ms=_timestamp_ms(row.get("createdAt")),
        liquidated=bool(row.get("liquidated", False)),
        raw=dict(row),
    )


def normalize_trade_event(post: Mapping[str, Any]) -> InvoTradeEvent | None:
    update = post.get("update")
    if not isinstance(update, Mapping):
        return None
    ticker = str(update.get("ticker") or "").strip().upper()
    if not ticker:
        return None
    direction_long = update.get("directionLong")
    if direction_long is True:
        direction = "LONG"
    elif direction_long is False:
        direction = "SHORT"
    else:
        return None
    portfolio = (
        update.get("portfolio")
        if isinstance(update.get("portfolio"), Mapping)
        else {}
    )
    owner = update.get("owner") if isinstance(update.get("owner"), Mapping) else {}
    leverage_raw = update.get("leverage")
    entry_raw = update.get("entryPrice")
    close_raw = update.get("closingPrice")
    is_open_raw = update.get("isOpen")
    return InvoTradeEvent(
        post_id=str(post.get("id") or ""),
        portfolio_id=(
            str(portfolio.get("id")) if portfolio.get("id") is not None else None
        ),
        owner_id=(str(owner.get("id")) if owner.get("id") is not None else None),
        username=(
            str(owner.get("username"))
            if owner.get("username") is not None
            else None
        ),
        base_id=(
            str(update.get("baseId")) if update.get("baseId") is not None else None
        ),
        base_short_id=(
            str(update.get("baseShortId"))
            if update.get("baseShortId") is not None
            else None
        ),
        coin=ticker,
        direction=direction,
        leverage=(_as_float(leverage_raw) if leverage_raw is not None else None),
        entry_price=(_as_float(entry_raw) if entry_raw is not None else None),
        closing_price=(_as_float(close_raw) if close_raw is not None else None),
        is_open=(bool(is_open_raw) if is_open_raw is not None else None),
        verified_trade=bool(update.get("verifiedTrade", False)),
        occurred_at_ms=(
            _timestamp_ms(update.get("createdAt"))
            or _timestamp_ms(post.get("createdAt"))
            or _timestamp_ms(update.get("updatedAt"))
        ),
        raw=dict(post),
    )


class InvoReadOnlyClient:
    """Read-only client for Invo discovery/feed endpoints.

    Authentication is supplied by the caller. Tokens are never logged or persisted by
    this class. The client intentionally does not expose follow or trading endpoints.
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        app_version: str = DEFAULT_INVO_APP_VERSION,
        base_url: str = INVO_API_BASE,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._access_token = (
            access_token.removeprefix("Bearer ") if access_token else None
        )
        self._refresh_token = (
            refresh_token.removeprefix("Bearer ") if refresh_token else None
        )
        self._app_version = app_version
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    async def __aenter__(self) -> InvoReadOnlyClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-app-version": self._app_version,
            "x-platform": "web",
        }

    async def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        response = await self._client.get(
            "/v1_0/auth/refresh_token",
            headers=self._headers(self._refresh_token),
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        token = payload.get("accessToken") if isinstance(payload, Mapping) else None
        if not token:
            return False
        self._access_token = str(token).removeprefix("Bearer ")
        return True

    async def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._access_token and not await self._refresh():
            raise InvoApiError("Invo authentication is required")
        assert self._access_token is not None
        response = await self._client.post(
            path,
            json=dict(payload),
            headers=self._headers(self._access_token),
        )
        if response.status_code == 401 and await self._refresh():
            assert self._access_token is not None
            response = await self._client.post(
                path,
                json=dict(payload),
                headers=self._headers(self._access_token),
            )
        if response.status_code >= 400:
            raise InvoApiError(f"Invo {path} returned HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, Mapping):
            raise InvoApiError(f"Invo {path} returned a non-object response")
        return data

    async def discover_portfolios(
        self,
        *,
        filter_name: str,
        page: int = 1,
        size: int = 50,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "filter": filter_name,
            "params": {"page": page, "size": size},
        }
        if user_id:
            payload["userId"] = user_id
        return await self._post("/v1_0/trending/get_portfolios_pl", payload)

    async def trending_users(
        self,
        *,
        page: int = 1,
        size: int = 25,
    ) -> Mapping[str, Any]:
        return await self._post(
            "/v1_0/trending/get_users",
            {"filter": "trending", "params": {"page": page, "size": size}},
        )

    async def feed(
        self,
        *,
        filter_name: str = "trending",
        last_post_id: str | None = None,
        item_limit: int = 50,
    ) -> Mapping[str, Any]:
        return await self._post(
            "/v1_0/posts/get_feed",
            {
                "filter": {"filter": filter_name, "assetTypes": []},
                "params": {"lastPostId": last_post_id, "itemLimit": item_limit},
            },
        )


def portfolio_candidates(payload: Mapping[str, Any]) -> list[InvoPortfolioCandidate]:
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    rows: list[InvoPortfolioCandidate] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        candidate = normalize_portfolio_candidate(item)
        if candidate is not None:
            rows.append(candidate)
    return rows


def verified_trade_events(payload: Mapping[str, Any]) -> list[InvoTradeEvent]:
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    events: list[InvoTradeEvent] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        event = normalize_trade_event(item)
        if event is not None and event.verified_trade:
            events.append(event)
    return events
