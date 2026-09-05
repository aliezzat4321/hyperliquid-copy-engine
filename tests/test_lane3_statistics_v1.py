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
