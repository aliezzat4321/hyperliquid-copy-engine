from datetime import UTC, datetime
from decimal import Decimal as D

import hlcopy.lane3.cli as cli
from hlcopy.lane3.costs import CostCompleteness, PositionEconomics
from hlcopy.lane3.reconstruction import Disposition, ExecutionLeg, ReconstructedPosition
from hlcopy.lane3.statistics import day_block_bootstrap, romano_wolf_stepdown


def test_block_bootstrap_seed_and_few_clusters():
    rows = [(f"d{i}", float(i)) for i in range(10)]
    first = day_block_bootstrap(rows, replicates=100, seed=7)
    second = day_block_bootstrap(rows, replicates=100, seed=7)
    assert first == second
    assert day_block_bootstrap(rows[:9]).verdict == "INSUFFICIENT_DEPENDENCE_STRUCTURE"


def test_romano_wolf_is_monotone_in_stepdown_order():
    adjusted = romano_wolf_stepdown([3, 2, 1], [[0, 0, 0], [4, 1, 0], [1, 3, 2]])
    assert adjusted[0] <= adjusted[1] <= adjusted[2]


def test_net_returns_keep_exit_day_aligned_when_some_costs_are_unmeasured():
    day_one = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
    day_two = int(datetime(2026, 1, 2, tzinfo=UTC).timestamp() * 1000)

    def position(key: str, exit_ms: int) -> ReconstructedPosition:
        entry = ExecutionLeg(exit_ms - 1_000, D("100"), D("1"), D("100"), "ENTRY")
        exit_leg = ExecutionLeg(exit_ms, D("101"), D("1"), D("101"), "EXIT")
        return ReconstructedPosition(
            key, "alice", "ETH", "long", [entry], exit_leg, Disposition.VALID_CLOSED
        )

    positions = [position("measured-a", day_one), position("unmeasured", day_two),
                 position("measured-b", day_one + 3_600_000)]
    economics = {
        "measured-a": PositionEconomics(D("1"), D("0"), D("0"), D("0"), D("0"),
                                         D("0"), D("1"), D("100"),
                                         CostCompleteness.MEASURED),
        "unmeasured": PositionEconomics(D("1"), D("0"), D("0"), None, None, None,
                                         None, None, CostCompleteness.UNMEASURED_NO_BOOK),
        "measured-b": PositionEconomics(D("2"), D("0"), D("0"), D("0"), D("0"),
                                         D("0"), D("2"), D("200"),
                                         CostCompleteness.MEASURED),
    }
    observations = cli._net_return_observations(
        positions, [economics[position.source_base_id] for position in positions]
    )

    assert observations == [("2026-01-01", 100.0), ("2026-01-01", 200.0)]
