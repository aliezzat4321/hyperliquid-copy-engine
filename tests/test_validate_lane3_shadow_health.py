import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_lane3_shadow_health.py"
SPEC = importlib.util.spec_from_file_location("validate_lane3_shadow_health", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
validate = MODULE.validate


def healthy() -> dict:
    return {
        "ok": True,
        "live": False,
        "initialized": True,
        "initializedSurfaces": ["following", "trending", "fire_moves", "most_recent"],
        "shadowOperationalReady": True,
        "shadowOperationalFailures": [],
        "fundingEconomicsReady": True,
        "feedPollHealthy": True,
        "directWatch": {
            "enabled": True,
            "capacityHealthy": True,
            "admissionsHealthy": True,
            "admissionSuspensionReason": None,
            "transportTargetCeiling": 16,
            "requestBudgetCoordinated": True,
        },
        "invoRequestBudget": {
            "coordinated": True,
            "primaryClass": "FEED",
            "feedPriorityHealthy": True,
            "feedPriorityFailures": [],
            "reservedForFeedRequestsPerSecond": 4,
            "cooldownRemainingMs": {"FEED": 0, "DIRECT_WATCH": 0},
            "classes": {"FEED": {"waitExceeded": 0}, "DIRECT_WATCH": {"waitExceeded": 0}},
        },
        "feedPortfolioEvidence": {"assimilationSuspended": False},
    }


def test_health_contract_accepts_only_complete_shadow_readiness() -> None:
    assert validate(healthy()) == []
    cases = [
        ("ok", False),
        ("live", True),
        ("shadowOperationalReady", False),
        ("initialized", False),
        ("fundingEconomicsReady", False),
        ("feedPollHealthy", False),
    ]
    for key, value in cases:
        payload = healthy()
        payload[key] = value
        assert validate(payload)
    for key, value in [
        ("capacityHealthy", False),
        ("admissionsHealthy", False),
        ("admissionSuspensionReason", "failure"),
        ("transportTargetCeiling", 0),
        ("requestBudgetCoordinated", False),
    ]:
        payload = healthy()
        payload["directWatch"][key] = value
        assert validate(payload)
    payload = healthy()
    payload["feedPortfolioEvidence"]["assimilationSuspended"] = True
    assert validate(payload)
    for invalid_ceiling in [True, 0.5, "16", None]:
        payload = healthy()
        payload["directWatch"]["transportTargetCeiling"] = invalid_ceiling
        assert "direct_watch_transport_ceiling_invalid" in validate(payload)


def test_health_contract_rejects_malformed_or_missing_fields() -> None:
    assert validate(None)
    payload = healthy()
    del payload["shadowOperationalFailures"]
    assert validate(payload)
    payload = healthy()
    payload["initializedSurfaces"] = ["following"]
    assert validate(payload)


def test_health_contract_requires_one_coordinated_budget_with_feed_priority() -> None:
    # An uncoordinated budget is exactly the failure this gate exists to catch: both paths
    # pacing themselves against one Invo quota, with reconciliation free to starve the
    # primary feed admission path.
    payload = healthy()
    del payload["invoRequestBudget"]
    assert "invo_request_budget_missing" in validate(payload)
    for key, value, expected in [
        ("coordinated", False, "invo_request_budget_not_coordinated"),
        ("primaryClass", "DIRECT_WATCH", "invo_request_budget_primary_class_not_feed"),
        ("feedPriorityHealthy", False, "invo_request_budget_feed_priority_unhealthy"),
        ("feedPriorityFailures", ["feed_budget_wait_exceeded"],
         "invo_request_budget_feed_priority_failures_present"),
        ("reservedForFeedRequestsPerSecond", 0, "invo_request_budget_feed_reserve_invalid"),
        ("reservedForFeedRequestsPerSecond", True, "invo_request_budget_feed_reserve_invalid"),
        ("reservedForFeedRequestsPerSecond", "4", "invo_request_budget_feed_reserve_invalid"),
        ("cooldownRemainingMs", None, "invo_request_budget_cooldown_missing"),
    ]:
        payload = healthy()
        payload["invoRequestBudget"][key] = value
        assert expected in validate(payload), (key, value)

    # The feed must never be throttled harder than reconciliation.
    payload = healthy()
    payload["invoRequestBudget"]["cooldownRemainingMs"] = {"FEED": 5_000, "DIRECT_WATCH": 1_000}
    assert "invo_request_budget_feed_gated_longer_than_direct_watch" in validate(payload)

    # A feed request that exhausted its bounded wait is a starvation observation.
    payload = healthy()
    payload["invoRequestBudget"]["classes"]["FEED"]["waitExceeded"] = 2
    assert "invo_request_budget_feed_wait_exhausted" in validate(payload)

    # A reconciliation-only cooldown is operationally acceptable and must stay acceptable.
    payload = healthy()
    payload["invoRequestBudget"]["cooldownRemainingMs"] = {"FEED": 0, "DIRECT_WATCH": 4_000}
    assert validate(payload) == []
