#!/usr/bin/env python3
"""Fail-closed deployment validation for the integrated Lane 3 shadow pipeline."""
from __future__ import annotations
import json
import sys

REQUIRED_SURFACES = {"following", "trending", "fire_moves", "most_recent"}

def validate(payload: object) -> list[str]:
    failures: list[str] = []
    if not isinstance(payload, dict): return ["health_payload_not_object"]
    if payload.get("ok") is not True: failures.append("top_level_ok_false")
    if payload.get("live") is not False: failures.append("live_mode_not_false")
    if payload.get("shadowOperationalReady") is not True: failures.append("shadow_pipeline_not_ready")
    if payload.get("initialized") is not True: failures.append("feed_surfaces_not_initialized")
    surfaces = payload.get("initializedSurfaces")
    if not isinstance(surfaces, list) or not REQUIRED_SURFACES.issubset(set(surfaces)):
        failures.append("required_feed_surfaces_missing")
    if payload.get("fundingEconomicsReady") is not True: failures.append("funding_economics_not_ready")
    if payload.get("feedPollHealthy") is not True: failures.append("feed_poll_not_healthy")
    direct = payload.get("directWatch")
    if not isinstance(direct, dict): failures.append("direct_watch_missing")
    else:
        if direct.get("enabled") is not True: failures.append("direct_watch_not_enabled")
        if direct.get("capacityHealthy") is not True: failures.append("direct_watch_not_operational")
        if direct.get("admissionsHealthy") is not True: failures.append("direct_watch_admissions_unhealthy")
        if direct.get("admissionSuspensionReason") is not None: failures.append("direct_watch_admission_suspended")
        if direct.get("hardProvenResidentCap") != 16: failures.append("direct_watch_hard_cap_not_16")
    evidence = payload.get("feedPortfolioEvidence")
    if not isinstance(evidence, dict) or evidence.get("assimilationSuspended") is not False:
        failures.append("feed_evidence_persistence_suspended")
    reported = payload.get("shadowOperationalFailures")
    if not isinstance(reported, list) or reported: failures.append("health_failures_present")
    return failures

def main() -> int:
    try: payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"LANE3_SHADOW_HEALTH_INVALID: invalid_json: {exc}", file=sys.stderr); return 2
    failures = validate(payload)
    if failures:
        print("LANE3_SHADOW_HEALTH_INVALID: " + ",".join(failures), file=sys.stderr); return 1
    print("LANE3_SHADOW_HEALTH_OK"); return 0

if __name__ == "__main__": raise SystemExit(main())
