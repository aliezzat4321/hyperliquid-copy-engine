from hlcopy.lane3.ledger import reconstruct_ledger
from hlcopy.lane3.reconstruction import Disposition, OrphanCause


def _signal(key="k", base="b", action="open"):
    return {"key": key, "sourceBaseId": base, "username": "alice", "coin": "ETH",
            "side": "long", "action": action, "entryPrice": 100, "closingPrice": 101}


def test_unpriced_close_surfaces_and_reconciles():
    rows = [
        {"ts": "2026-01-01T00:00:00Z", "type": "shadow_opened", "signal": _signal(),
         "entryMid": 100, "size": 1, "notionalUsd": 100},
        {"ts": "2026-01-01T01:00:00Z", "type": "shadow_close_unpriced",
         "signal": _signal("c", action="close")},
    ]
    result = reconstruct_ledger(rows)
    assert result.positions[0].disposition == Disposition.QUARANTINE_UNPRICED_CLOSE
    assert result.positions[0].quarantine_sensitivity_usd == 1
    assert result.reconcile()["i2_rhs"] == 1


def test_duplicate_close_is_counted_and_orphan_classified():
    opened = {"ts": "2026-01-01T00:00:00Z", "type": "shadow_opened", "signal": _signal(),
              "entryMid": 100, "size": 1, "notionalUsd": 100}
    close = {"ts": "2026-01-01T01:00:00Z", "type": "shadow_closed",
             "signal": _signal("c", action="close"), "entryMid": 100, "exitMid": 101,
             "size": 1, "sourceClosingPrice": 101}
    result = reconstruct_ledger([opened, close, {**close, "signal": _signal("c2", action="close")}])
    assert result.duplicate_close_rows == 1
    orphan = reconstruct_ledger([
        {"ts": "2026-01-01T00:00:00Z", "type": "skip",
         "reason": "stale_signal_over_25s_window", "signal": _signal()},
        {"ts": "2026-01-01T01:00:00Z", "type": "skip",
         "reason": "close_not_owned_by_service",
         "signal": _signal("c", action="close")},
    ])
    assert orphan.orphan_causes["b"] == OrphanCause.OPEN_SKIPPED_STALE


def test_lane3_preserves_canonical_multiplier_symbol_case():
    signal = {**_signal(), "coin": "kBONK"}
    opened = {
        "ts": "2026-01-01T00:00:00Z",
        "type": "shadow_opened",
        "signal": signal,
        "entryMid": "0.01",
        "size": "100",
        "notionalUsd": "1",
    }

    result = reconstruct_ledger([opened], {"managed": {"b": {}}})

    assert result.positions[0].coin == "kBONK"
