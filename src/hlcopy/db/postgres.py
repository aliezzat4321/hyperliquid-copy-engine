from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb

from hlcopy.discovery.leaderboard import LeaderboardCandidate
from hlcopy.models import Fill
from hlcopy.positions.state_machine import PositionEpisode

if TYPE_CHECKING:
    from hlcopy.analytics.trader_profile import TraderProfile


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.conn: psycopg.AsyncConnection[Any] | None = None

    async def __aenter__(self) -> Database:
        self.conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.conn is not None:
            await self.conn.close()

    def _require(self) -> psycopg.AsyncConnection[Any]:
        if self.conn is None:
            raise RuntimeError("database is not connected")
        return self.conn

    async def init_schema(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text()
        conn = self._require()
        for statement in schema.split(";"):
            if statement.strip():
                await conn.execute(statement)

    async def store_raw(
        self,
        *,
        source: str,
        endpoint: str,
        request_payload: dict[str, Any] | None,
        response_payload: Any,
        fetched_at_ms: int,
    ) -> None:
        canonical = json.dumps(response_payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        await self._require().execute(
            """
            INSERT INTO raw_api_responses
              (source, endpoint, request_json, response_json, fetched_at, content_sha256)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                source,
                endpoint,
                Jsonb(request_payload) if request_payload is not None else None,
                Jsonb(response_payload),
                _dt(fetched_at_ms),
                digest,
            ),
        )

    async def upsert_leaderboard(
        self, candidates: list[LeaderboardCandidate], snapshot_at_ms: int
    ) -> None:
        conn = self._require()
        snapshot_at = _dt(snapshot_at_ms)
        period_ranks: dict[str, dict[str, int]] = {}
        periods = {period for candidate in candidates for period in candidate.windows}
        for period in periods:
            ordered = sorted(candidates, key=lambda c: c.window(period).pnl, reverse=True)
            period_ranks[period] = {c.address: i for i, c in enumerate(ordered, start=1)}
        for candidate in candidates:
            await conn.execute(
                """
                INSERT INTO wallets(
                  address, first_seen, last_seen, source, display_name, metadata_json
                )
                VALUES (%s, %s, %s, 'official_leaderboard', %s, %s)
                ON CONFLICT(address) DO UPDATE SET
                  last_seen = EXCLUDED.last_seen,
                  display_name = COALESCE(EXCLUDED.display_name, wallets.display_name)
                """,
                (candidate.address, snapshot_at, snapshot_at, candidate.display_name, Jsonb({})),
            )
            for period, stats in candidate.windows.items():
                await conn.execute(
                    """
                    INSERT INTO leaderboard_snapshots
                      (snapshot_at, address, ranking_period, rank, pnl, roi, volume,
                       account_value, raw_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        snapshot_at,
                        candidate.address,
                        period,
                        period_ranks.get(period, {}).get(candidate.address),
                        stats.pnl,
                        stats.roi,
                        stats.volume,
                        candidate.account_value,
                        Jsonb(candidate.raw),
                    ),
                )

    async def upsert_fills(self, fills: list[Fill]) -> None:
        conn = self._require()
        for fill in fills:
            await conn.execute(
                """
                INSERT INTO fills
                  (wallet_address, tid, oid, tx_hash, timestamp, coin, side, direction,
                   price, size, start_position, closed_pnl, fee, fee_token, crossed,
                   builder_fee, raw_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(wallet_address, tid) DO NOTHING
                """,
                (
                    fill.wallet_address,
                    fill.tid,
                    fill.oid,
                    fill.tx_hash,
                    _dt(fill.timestamp_ms),
                    fill.coin,
                    fill.side,
                    fill.direction,
                    fill.price,
                    fill.size,
                    fill.start_position,
                    fill.closed_pnl,
                    fill.fee,
                    fill.fee_token,
                    fill.crossed,
                    fill.builder_fee,
                    Jsonb(fill.raw),
                ),
            )

    async def replace_episodes(self, wallet: str, episodes: list[PositionEpisode]) -> None:
        conn = self._require()
        await conn.execute("DELETE FROM position_episodes WHERE wallet_address = %s", (wallet,))
        for ep in episodes:
            await conn.execute(
                """
                INSERT INTO position_episodes
                  (wallet_address, coin, direction, opened_at, closed_at, avg_entry, avg_exit,
                   max_size, realized_pnl, fees, funding, holding_seconds, complete_start,
                   fill_count, fill_tids)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    ep.wallet_address,
                    ep.coin,
                    ep.direction,
                    _dt(ep.opened_at_ms) if ep.opened_at_ms is not None else None,
                    _dt(ep.closed_at_ms) if ep.closed_at_ms is not None else None,
                    ep.avg_entry,
                    ep.avg_exit,
                    ep.max_abs_size,
                    ep.realized_pnl,
                    ep.fees,
                    ep.funding,
                    ep.holding_seconds,
                    ep.complete_start,
                    ep.fill_count,
                    ep.fill_tids,
                ),
            )

    async def store_metrics(
        self, wallet: str, as_of_ms: int, lookback: str, metrics: dict[str, Any]
    ) -> None:
        await self._require().execute(
            """
            INSERT INTO wallet_metrics(wallet_address, as_of_timestamp, lookback, metrics_json)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(wallet_address, as_of_timestamp, lookback)
            DO UPDATE SET metrics_json = EXCLUDED.metrics_json
            """,
            (wallet, _dt(as_of_ms), lookback, Jsonb(metrics)),
        )

    async def store_trader_profile(self, profile: TraderProfile) -> None:
        await self._require().execute(
            """
            INSERT INTO trader_profiles(
              wallet_address, as_of_timestamp, lookback_start, model_version, profile_json
            )
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(wallet_address, as_of_timestamp, model_version)
            DO UPDATE SET
              lookback_start = EXCLUDED.lookback_start,
              profile_json = EXCLUDED.profile_json
            """,
            (
                profile.wallet_address,
                _dt(profile.as_of_ms),
                _dt(profile.lookback_start_ms),
                profile.model_version,
                Jsonb(profile.to_dict()),
            ),
        )
