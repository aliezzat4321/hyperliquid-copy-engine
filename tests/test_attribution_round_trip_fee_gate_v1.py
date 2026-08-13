import pytest

from hlcopy.profitability.attribution_cli import (
    ROUND_TRIP_PNL_MODEL,
    _assert_round_trip_fee_truth,
)


def test_legacy_pnl_model_is_refused() -> None:
    with pytest.raises(SystemExit):
        _assert_round_trip_fee_truth({"pnl_model": "PROPORTIONAL_POSITION_CHANGE_V2", "realized_slices": []})


def test_missing_entry_fee_allocation_is_refused() -> None:
    with pytest.raises(SystemExit):
        _assert_round_trip_fee_truth(
            {
                "pnl_model": ROUND_TRIP_PNL_MODEL,
                "realized_slices": [{"net_pnl_usd": "1", "fee_usd": "0.1"}],
            }
        )


def test_explicit_zero_entry_fee_is_valid_truth() -> None:
    _assert_round_trip_fee_truth(
        {
            "pnl_model": ROUND_TRIP_PNL_MODEL,
            "realized_slices": [
                {
                    "net_pnl_usd": "1",
                    "fee_usd": "0.1",
                    "entry_fee_usd_allocated": "0",
                }
            ],
        }
    )
