from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    method: str
    level: float
    lower: float | None
    upper: float | None
    p_value: float | None
    clusters: int
    seed: int
    verdict: str


def day_block_bootstrap(
    observations: list[tuple[str, float]], *, replicates: int = 10_000,
    seed: int = 193, confidence_level: float = 0.90, min_clusters: int = 10,
) -> BootstrapResult:
    grouped: dict[str, list[float]] = {}
    for day, value in observations:
        grouped.setdefault(day, []).append(float(value))
    days = sorted(grouped)
    if len(days) < min_clusters:
        return BootstrapResult("utc_day_block_bootstrap", confidence_level, None, None,
                               None, len(days), seed, "INSUFFICIENT_DEPENDENCE_STRUCTURE")
    rng = random.Random(seed)
    draws: list[float] = []
    nonpositive = 0
    for _ in range(replicates):
        selected = [rng.choice(days) for _ in days]
        values = [value for day in selected for value in grouped[day]]
        estimate = sum(values) / len(values)
        draws.append(estimate)
        nonpositive += estimate <= 0
    draws.sort()
    tail = (1 - confidence_level) / 2
    lo = draws[min(len(draws) - 1, int(tail * len(draws)))]
    hi = draws[min(len(draws) - 1, int((1 - tail) * len(draws)))]
    return BootstrapResult("utc_day_block_bootstrap", confidence_level, lo, hi,
                           (nonpositive + 1) / (replicates + 1), len(days), seed, "EVALUATED")


def romano_wolf_stepdown(
    observed_statistics: list[float], bootstrap_statistics: list[list[float]],
) -> list[float]:
    """One-sided Romano-Wolf max-T step-down adjusted p-values.

    Rows are joint, day-block bootstrap draws and columns are the screened hypotheses.
    """
    count = len(observed_statistics)
    if not count:
        return []
    if any(len(row) != count for row in bootstrap_statistics):
        raise ValueError("joint bootstrap matrix width mismatch")
    order = sorted(range(count), key=lambda index: observed_statistics[index], reverse=True)
    remaining = set(range(count))
    adjusted = [1.0] * count
    prior = 0.0
    denominator = len(bootstrap_statistics) + 1
    for index in order:
        exceed = sum(
            max(row[j] for j in remaining) >= observed_statistics[index]
            for row in bootstrap_statistics
        )
        value = max(prior, (exceed + 1) / denominator)
        adjusted[index] = min(1.0, value)
        prior = adjusted[index]
        remaining.remove(index)
    return adjusted


def studentized(mean: float, standard_error: float) -> float:
    if standard_error <= 0 or not math.isfinite(standard_error):
        return 0.0
    return mean / standard_error
