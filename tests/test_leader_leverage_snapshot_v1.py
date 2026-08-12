from hlcopy.profitability.leader_leverage_snapshot_cli import parse_clearinghouse_state


def test_parse_clearinghouse_state_tracks_effective_and_position_leverage() -> None:
    state = {
        "marginSummary": {
            "accountValue": "2500",
            "totalNtlPos": "100000",
            "totalMarginUsed": "2500",
        },
        "crossMaintenanceMarginUsed": "1250",
        "withdrawable": "1000",
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "1",
                    "positionValue": "100000",
                    "marginUsed": "2500",
                    "entryPx": "100000",
                    "liquidationPx": "98000",
                    "unrealizedPnl": "0",
                    "leverage": {"type": "cross", "value": 40},
                }
            }
        ],
    }
    row = parse_clearinghouse_state("0xABC", state)
    assert row["effective_gross_leverage"] == "40"
    assert row["wallet_address"] == "0xabc"
    assert row["positions"][0]["leverage_value"] == "40"
    assert row["positions"][0]["leverage_type"] == "cross"
