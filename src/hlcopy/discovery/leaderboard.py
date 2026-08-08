from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True, slots=True)
class WindowPerformance:
    pnl: float = 0.0
    roi: float = 0.0
    volume: float = 0.0


@dataclass(frozen=True, slots=True)
class LeaderboardCandidate:
    address: str
    display_name: str | None
    account_value: float
    windows: dict[str, WindowPerformance]
    raw: dict[str, Any]
    cheap_score: float = 0.0

    def window(self, name: str) -> WindowPerformance:
        return self.windows.get(name, WindowPerformance())


def parse_leaderboard(payload: dict[str, Any]) -> list[LeaderboardCandidate]:
    rows = payload.get("leaderboardRows", [])
    result: list[LeaderboardCandidate] = []
    for row in rows:
        address = str(row.get("ethAddress", "")).lower()
        if not address.startswith("0x") or len(address) != 42:
            continue
        windows: dict[str, WindowPerformance] = {}
        for item in row.get("windowPerformances", []):
            if not isinstance(item, list) or len(item) != 2:
                continue
            name, stats = item
            stats = stats or {}
            windows[str(name)] = WindowPerformance(
                pnl=_f(stats.get("pnl")),
                roi=_f(stats.get("roi")),
                volume=_f(stats.get("vlm")),
            )
        result.append(
            LeaderboardCandidate(
                address=address,
                display_name=row.get("displayName"),
                account_value=_f(row.get("accountValue")),
                windows=windows,
                raw=row,
            )
        )
    return result


def _percentile_ranks(values: list[float]) -> list[float]:
    if len(values) <= 1:
        return [1.0] * len(values)
    ordered = sorted((value, idx) for idx, value in enumerate(values))
    ranks = [0.0] * len(values)
    for rank, (_, idx) in enumerate(ordered):
        ranks[idx] = rank / (len(values) - 1)
    return ranks


def shortlist(
    candidates: list[LeaderboardCandidate],
    *,
    limit: int,
    min_account_value: float,
    min_month_roi: float,
    min_month_volume: float,
) -> list[LeaderboardCandidate]:
    eligible = []
    for candidate in candidates:
        month = candidate.window("month")
        all_time = candidate.window("allTime")
        if candidate.account_value < min_account_value:
            continue
        if month.roi < min_month_roi or month.volume < min_month_volume:
            continue
        if month.pnl <= 0 or all_time.pnl <= 0:
            continue
        eligible.append(candidate)

    if not eligible:
        return []

    month_roi_r = _percentile_ranks([c.window("month").roi for c in eligible])
    all_roi_r = _percentile_ranks([c.window("allTime").roi for c in eligible])
    month_pnl_r = _percentile_ranks(
        [c.window("month").pnl / max(c.account_value, 1.0) for c in eligible]
    )

    scored: list[LeaderboardCandidate] = []
    for i, candidate in enumerate(eligible):
        positive_windows = sum(
            candidate.window(name).pnl > 0 for name in ("day", "week", "month", "allTime")
        )
        persistence = positive_windows / 4
        turnover = candidate.window("month").volume / max(candidate.account_value, 1.0)
        turnover_penalty = min(1.0, max(0.0, (turnover - 250.0) / 750.0))
        score = 100 * (
            0.35 * month_roi_r[i]
            + 0.20 * all_roi_r[i]
            + 0.20 * month_pnl_r[i]
            + 0.25 * persistence
        )
        score -= 20 * turnover_penalty
        scored.append(
            LeaderboardCandidate(
                address=candidate.address,
                display_name=candidate.display_name,
                account_value=candidate.account_value,
                windows=candidate.windows,
                raw=candidate.raw,
                cheap_score=round(score, 4),
            )
        )
    return sorted(scored, key=lambda c: c.cheap_score, reverse=True)[:limit]
