from __future__ import annotations

import math

from hlcopy.db.postgres import _json_safe


def test_json_safe_normalizes_non_finite_metrics_recursively() -> None:
    payload = {
        "profit_factor": float("inf"),
        "bad_negative": float("-inf"),
        "bad_nan": float("nan"),
        "nested": [1.5, {"x": float("inf")}],
        "finite": 42.0,
        "count": 7,
    }

    result = _json_safe(payload)

    assert result["profit_factor"] is None
    assert result["bad_negative"] is None
    assert result["bad_nan"] is None
    assert result["nested"] == [1.5, {"x": None}]
    assert result["finite"] == 42.0
    assert result["count"] == 7
    assert math.isfinite(result["finite"])
