from hlcopy.profitability.lane1_audit_bundle import collect_audit_targets

WALLET = "0x" + "a" * 40


def test_confirmation_cohort_becomes_replay_target_when_selection_is_empty() -> None:
    targets = collect_audit_targets(
        {"candidates": []},
        {"robust_candidates": []},
        {"targets": []},
        confirmation_rows=[
            {
                "wallet_address": WALLET,
                "coin": "BTC",
                "selection_return_bps": "-12",
            }
        ],
        screening_rows=[
            {
                "wallet_address": "0x" + "b" * 40,
                "coin": "ETH",
                "selection_return_bps": "5",
            }
        ],
    )

    assert [(row.wallet_address, row.coin, row.roles) for row in targets] == [
        (WALLET, "BTC", ("confirmed_fallback",))
    ]
