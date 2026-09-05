import pytest

from hlcopy.lane3.promotion import validate_slice_spec


@pytest.mark.parametrize("field", ["held_ms", "add_count", "net_pnl_usd", "outcome"])
def test_realized_fields_raise_in_promotable_slice_spec(field):
    with pytest.raises(TypeError):
        validate_slice_spec({"trader": "alice", "coin": "ETH", field: 1})
