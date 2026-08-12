from __future__ import annotations

from dataclasses import dataclass

from hlcopy.discovery.leaderboard import LeaderboardCandidate


@dataclass(frozen=True, slots=True)
class UniverseRow:
    address: str
    display_name: str | None
    account_value: float
    score: float
    rank: int
    positive_windows: int
    day_roi: float
    week_roi: float
    month_roi: float
    all_time_roi: float
    month_pnl: float
    month_volume: float

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "display_name": self.display_name,
            "account_value": self.account_value,
            "score": self.score,
            "rank": self.rank,
            "positive_windows": self.positive_windows,
            "day_roi": self.day_roi,
            "week_roi": self.week_roi,
            "month_roi": self.month_roi,
            "all_time_roi": self.all_time_roi,
            "month_pnl": self.month_pnl,
            "month_volume": self.month_volume,
        }


def _percentile_ranks(values: list[float]) -> list[float]:
    if not values:
        return []
    if len(values) == 1:
        return [1.0]
    ordered = sorted((value, index) for index, value in enumerate(values))
    result = [0.0] * len(values)
    for rank, (_, index) in enumerate(ordered):
        result[index] = rank / (len(values) - 1)
    return result


def rank_universe(
    candidates: list[LeaderboardCandidate],
    *,
    min_account_value: float = 1_000.0,
) -> list[UniverseRow]:
    """Broad discovery ranking, intentionally looser than trading approval.

    This ranking is only a cheap screen for prospective observation. It combines
    several leaderboard horizons so a wallet cannot rank highly solely from one
    short-lived PnL spike, while still allowing newly strong day/week traders to enter
    the research universe quickly.
    """

    eligible = [
        candidate
        for candidate in candidates
        if candidate.account_value >= min_account_value
        and any(
            candidate.window(name).pnl > 0
            for name in ("day", "week", "month", "allTime")
        )
    ]
    if not eligible:
        return []

    day_r = _percentile_ranks([c.window("day").roi for c in eligible])
    week_r = _percentile_ranks([c.window("week").roi for c in eligible])
    month_r = _percentile_ranks([c.window("month").roi for c in eligible])
    all_r = _percentile_ranks([c.window("allTime").roi for c in eligible])
    capital_pnl_r = _percentile_ranks(
        [c.window("month").pnl / max(c.account_value, 1.0) for c in eligible]
    )

    scored: list[tuple[float, LeaderboardCandidate, int]] = []
    for index, candidate in enumerate(eligible):
        positive_windows = sum(
            candidate.window(name).pnl > 0
            for name in ("day", "week", "month", "allTime")
        )
        persistence = positive_windows / 4.0
        turnover = candidate.window("month").volume / max(candidate.account_value, 1.0)
        turnover_penalty = min(1.0, max(0.0, (turnover - 300.0) / 1_200.0))
        score = 100.0 * (
            0.10 * day_r[index]
            + 0.25 * week_r[index]
            + 0.25 * month_r[index]
            + 0.10 * all_r[index]
            + 0.15 * capital_pnl_r[index]
            + 0.15 * persistence
        )
        score -= 10.0 * turnover_penalty
        scored.append((score, candidate, positive_windows))

    scored.sort(key=lambda item: item[0], reverse=True)
    rows: list[UniverseRow] = []
    for rank, (score, candidate, positive_windows) in enumerate(scored, 1):
        rows.append(
            UniverseRow(
                address=candidate.address,
                display_name=candidate.display_name,
                account_value=candidate.account_value,
                score=round(score, 6),
                rank=rank,
                positive_windows=positive_windows,
                day_roi=candidate.window("day").roi,
                week_roi=candidate.window("week").roi,
                month_roi=candidate.window("month").roi,
                all_time_roi=candidate.window("allTime").roi,
                month_pnl=candidate.window("month").pnl,
                month_volume=candidate.window("month").volume,
            )
        )
    return rows


def movement_signals(
    current: list[UniverseRow],
    previous_ranks: dict[str, int],
) -> dict[str, tuple[str, ...]]:
    signals: dict[str, tuple[str, ...]] = {}
    for row in current:
        previous = previous_ranks.get(row.address)
        tags: list[str] = []
        if previous is None:
            tags.append("NEW_TO_OBSERVED_LEADERBOARD")
        if row.rank <= 100 and (previous is None or previous > 100):
            tags.append("ENTERED_TOP_100")
        if row.rank <= 50:
            tags.append("TOP_50")
        if previous is not None and previous - row.rank >= 25:
            tags.append("RANK_JUMP_25")
        if row.day_roi > 0 and row.week_roi > 0 and row.month_roi > 0:
            tags.append("POSITIVE_DAY_WEEK_MONTH")
        if tags:
            signals[row.address] = tuple(tags)
    return signals
