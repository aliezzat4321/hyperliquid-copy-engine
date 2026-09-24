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
