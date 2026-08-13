from decimal import Decimal as D

from hlcopy.profitability.margin_tables import parse_margin_metadata


def test_margin_table_deduction_is_continuous() -> None:
    payload = {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 100},
            {"name": "TEST", "szDecimals": 2, "maxLeverage": 10},
        ],
        "marginTables": [
            [
                100,
                {
                    "description": "",
                    "marginTiers": [
                        {"lowerBound": "0", "maxLeverage": 40},
                        {"lowerBound": "150000000", "maxLeverage": 20},
                    ],
                },
            ]
        ],
    }
    snapshot = parse_margin_metadata(payload, fetched_at_ns=1)
    tables = snapshot.by_coin()
    btc = tables["BTC"]
    assert len(btc.tiers) == 2
    first, second = btc.tiers
    assert first.maintenance_margin_rate == D("0.0125")
    assert second.maintenance_margin_rate == D("0.025")
    assert second.maintenance_margin_deduction_usd == D("1875000.0000")

    boundary = D("150000000")
    first_requirement = boundary * first.maintenance_margin_rate
    second_requirement = (
        boundary * second.maintenance_margin_rate
        - second.maintenance_margin_deduction_usd
    )
    assert first_requirement == second_requirement


def test_single_tier_id_below_50_uses_id_as_max_leverage() -> None:
    payload = {
        "universe": [{"name": "ABC", "szDecimals": 2, "maxLeverage": 10}],
        "marginTables": [],
    }
    snapshot = parse_margin_metadata(payload, fetched_at_ns=1)
    tier = snapshot.by_coin()["ABC"].tiers[0]
    assert tier.max_leverage == D("10")
    assert tier.maintenance_margin_rate == D("0.05")
