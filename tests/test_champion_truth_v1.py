from hlcopy.profitability.champion_truth import (
    REQUIRED_TRUTH_LAYERS,
    evaluate_champion_truth,
)


def test_missing_truth_layers_block_validated_champion() -> None:
    result = evaluate_champion_truth({"round_trip_fee_accounting": True})
    assert result.validated is False
    assert "continuous_mtm" in result.blockers
    assert "funding" in result.blockers
    assert "maintenance_margin" in result.blockers
    assert "liquidation_survival" in result.blockers
    assert "safe_leverage" in result.blockers


def test_only_literal_true_passes_a_truth_layer() -> None:
    truth = {name: True for name in REQUIRED_TRUTH_LAYERS}
    truth["funding"] = "TRUE"
    result = evaluate_champion_truth(truth)
    assert result.validated is False
    assert result.blockers == ("funding",)


def test_all_truth_layers_are_required_for_validation() -> None:
    truth = {name: True for name in REQUIRED_TRUTH_LAYERS}
    result = evaluate_champion_truth(truth)
    assert result.validated is True
    assert result.blockers == ()
    assert result.to_dict()["validation_status"] == "VALIDATED_CHAMPION_TRUTH_COMPLETE"
