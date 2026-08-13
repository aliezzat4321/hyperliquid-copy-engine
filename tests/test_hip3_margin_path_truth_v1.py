from decimal import Decimal

from hlcopy.profitability.margin_tables import parse_margin_metadata, snapshot_table_at


def test_hip3_margin_metadata_uses_canonical_namespaced_coin() -> None:
    snapshot = parse_margin_metadata(
        {
            "universe": [
                {"name": "NBIS", "maxLeverage": 10, "marginTableId": 51},
            ],
            "marginTables": [
                [
                    51,
                    {
                        "description": "tiered 10x",
                        "marginTiers": [
                            {"lowerBound": "0", "maxLeverage": 10},
                            {"lowerBound": "3000000", "maxLeverage": 5},
                        ],
                    },
                ]
            ],
        },
        fetched_at_ns=100,
        dex="xyz",
    )

    assert snapshot.dex == "xyz"
    assert set(snapshot.by_coin()) == {"XYZ:NBIS"}
    table = snapshot_table_at((snapshot,), "xyz:NBIS", 101)
    assert table is not None
    assert table.coin == "XYZ:NBIS"
    assert table.tier_for_notional(Decimal("1000")).max_leverage == Decimal("10")


def test_default_margin_metadata_does_not_gain_fake_namespace() -> None:
    snapshot = parse_margin_metadata(
        {
            "universe": [{"name": "BTC", "maxLeverage": 40, "marginTableId": 40}],
            "marginTables": [],
        },
        fetched_at_ns=100,
        dex="",
    )
    assert set(snapshot.by_coin()) == {"BTC"}
