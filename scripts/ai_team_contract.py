#!/usr/bin/env python3
"""Schema, enums and fail-closed validation for the shared AI-team contract.

Importable so the adversarial tests in ``tests/test_ai_team_contract_v2.py`` can
exercise the rules directly instead of shelling out to a script.

Design rule: **fail closed**. Unknown fields, unknown enum members, malformed
timestamps, missing provenance and stale snapshots are all rejected. A contract
that only checks a file against its own renderer proves internal consistency, not
accuracy, and cannot be trusted to gate capital.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any


class ContractError(Exception):
    """A durable-state or governance invariant was violated."""


# --- enums -----------------------------------------------------------------

#: Logical agent identities. GitHub currently cannot prove these apart (both
#: agents act through one account), so they are recorded, not authenticated.
#: See docs/ai-team/REVIEW_PROVENANCE.md.
AGENTS = frozenset({"CLAUDE", "CODEX_CHATGPT", "USER"})
BUILDER_AGENTS = frozenset({"CLAUDE", "CODEX_CHATGPT"})

PRIORITIES = frozenset({"P0", "P1", "P2"})
WORK_STATUSES = frozenset({"OPEN", "IN_PROGRESS", "IN_REVIEW", "BLOCKED", "DONE", "DROPPED"})
ACTIVE_WORK_STATUSES = frozenset({"OPEN", "IN_PROGRESS", "IN_REVIEW", "BLOCKED"})

LANE_KEYS = ("lane_1", "lane_2", "lane_3")
LANE_STATUSES = frozenset({
    "RESEARCH_ACTIVE_HANDOFF_MANUAL",
    "ZERO_CURRENT_PUBLICATION_YIELD",
    "SHADOW_EVIDENCE_MEASUREMENT_INCOMPLETE",
    "SHADOW_VALIDATED_CANDIDATE",
    "MICRO_LIVE",
    "PAUSED",
    "RETIRED",
})
INFRA_STATUSES = frozenset({"OK", "DEGRADED", "P0_STORAGE_PRESSURE", "P0_CAPTURE_DOWN"})
LIVE_STATUSES = frozenset({"DISABLED", "AUTHORIZED", "SUSPENDED"})
LIVE_STAGES = frozenset({"MICRO_LIVE", "SMALL_LIVE", "NORMAL", "SCALED"})

SOURCE_TYPES = frozenset({
    "WORKFLOW_RUN", "PULL_REQUEST", "ISSUE", "EXPERIMENT", "COMMIT", "MANUAL_OBSERVATION",
})
UNITS = frozenset({"count", "usd", "bps", "pct", "bool", "text"})

EVIDENCE_LEVELS = frozenset({
    "EXPLORATORY",
    "FROZEN_PROSPECTIVE",
    "SHADOW_VALIDATED",
    "MICRO_LIVE_CANDIDATE",
    "MICRO_LIVE_VALIDATED",
    "SCALED_CANDIDATE",
})
EXPERIMENT_STATUSES = frozenset({"PLANNED", "RUNNING", "IN_REVIEW", "COMPLETE", "ABANDONED"})
EXPERIMENT_RESULTS = frozenset({"PASS", "FAIL", "INCONCLUSIVE", "PENDING"})

#: Strings that look like a value but encode "nobody decided yet". These must never
#: satisfy an owner, reviewer or blocker field on active work.
PLACEHOLDERS = frozenset({
    "", "-", "?", "TBD", "TODO", "NONE", "N/A", "NA", "UNKNOWN", "UNASSIGNED",
    "UNASSIGNED_ONE_BUILDER", "OTHER_AI_AGENT", "SOMEONE", "AGENT",
})

APPROVAL_REFERENCE_RE = re.compile(r"^LIVE-AUTH-\d{4}-\d{2}-\d{2}-\d{3}$")
FACT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NEXT_RE = re.compile(r"^(Issue #\d+|NONE)$")
EXPERIMENT_ID_RE = re.compile(r"^EXP-\d{3}$")

SOURCE_REF_RE: dict[str, re.Pattern[str]] = {
    "WORKFLOW_RUN": re.compile(r"^\d{6,}$"),
    "PULL_REQUEST": re.compile(r"^#\d+$"),
    "ISSUE": re.compile(r"^#\d+$"),
    "EXPERIMENT": EXPERIMENT_ID_RE,
    "COMMIT": re.compile(r"^[0-9a-f]{7,40}$"),
    "MANUAL_OBSERVATION": re.compile(r"^\S.{7,}$"),
}

SCHEMA_VERSION = 2
#: A snapshot older than this is treated as unknown state rather than current fact.
MAX_SNAPSHOT_AGE_HOURS = 72
#: Tolerance for clock skew between the observing runner and CI.
FUTURE_SKEW = timedelta(minutes=10)


# --- primitives ------------------------------------------------------------


def _obj(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{where} must be an object")
    return value


def _keys(
    value: Any, required: Sequence[str], optional: Iterable[str] = (), *, where: str
) -> Mapping[str, Any]:
    """Exact-key check. Unknown fields fail closed rather than being ignored."""
    obj = _obj(value, where)
    present = set(obj)
    missing = sorted(set(required) - present)
    if missing:
        raise ContractError(f"{where} is missing required field(s): {missing}")
    unknown = sorted(present - set(required) - set(optional))
    if unknown:
        raise ContractError(f"{where} has unknown field(s) {unknown}; failing closed")
    return obj


def _text(value: Any, where: str, *, allow_placeholder: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} must be a non-empty string")
    if not allow_placeholder and value.strip().upper() in PLACEHOLDERS:
        raise ContractError(f"{where} is the placeholder {value!r}; a real value is required")
    return value


def _enum(value: Any, allowed: frozenset[str], where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{where} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{where} must be a boolean, got {type(value).__name__}")
    return value


def _timestamp(value: Any, where: str, *, now: datetime, allow_future: bool = False) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{where} must be an RFC3339 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{where} is not a valid RFC3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{where} must carry a timezone offset: {value!r}")
    parsed = parsed.astimezone(UTC)
    if not allow_future and parsed > now + FUTURE_SKEW:
        raise ContractError(f"{where} is in the future: {value!r}")
    return parsed


# --- facts -----------------------------------------------------------------

_FACT_FIELDS = ("key", "label", "value", "unit", "observed_at", "source_type", "source_ref")


def validate_fact(fact: Any, *, where: str, now: datetime) -> str:
    obj = _keys(fact, _FACT_FIELDS, where=where)
    key = _text(obj["key"], f"{where}.key")
    if not FACT_KEY_RE.match(key):
        raise ContractError(f"{where}.key must be lower_snake_case, got {key!r}")
    _text(obj["label"], f"{where}.label")
    unit = _enum(obj["unit"], UNITS, f"{where}.unit")

    value = obj["value"]
    if unit == "bool":
        _bool(value, f"{where}.value")
    elif unit == "text":
        _text(value, f"{where}.value")
    elif unit == "count":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractError(f"{where}.value must be a non-negative integer for unit 'count'")
    else:  # usd, bps, pct
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f"{where}.value must be numeric for unit {unit!r}")
        if unit == "pct" and not (0 <= float(value) <= 100):
            raise ContractError(f"{where}.value must be within 0..100 for unit 'pct'")

    _timestamp(obj["observed_at"], f"{where}.observed_at", now=now)
    source_type = _enum(obj["source_type"], SOURCE_TYPES, f"{where}.source_type")
    source_ref = _text(obj["source_ref"], f"{where}.source_ref")
    pattern = SOURCE_REF_RE[source_type]
    if not pattern.match(source_ref):
        raise ContractError(
            f"{where}.source_ref {source_ref!r} does not match the required format "
            f"for source_type {source_type} ({pattern.pattern})"
        )
    return key


def validate_facts(facts: Any, *, where: str, now: datetime) -> None:
    if not isinstance(facts, list) or not facts:
        raise ContractError(f"{where} must be a non-empty list of structured facts")
    seen: set[str] = set()
    for index, fact in enumerate(facts):
        key = validate_fact(fact, where=f"{where}[{index}]", now=now)
        if key in seen:
            raise ContractError(f"{where} repeats fact key {key!r}")
        seen.add(key)


# --- live trading ----------------------------------------------------------

_SCOPE_FIELDS = ("lane", "slice", "service", "stage", "max_notional_usd")
_AUTH_FIELDS = (
    "authorized_by", "scope", "authorized_at", "approval_reference", "expires_at", "revoked",
)


def validate_live_trading(live: Any, *, now: datetime) -> None:
    """Real capital requires a complete, unexpired, user-issued authorization object.

    This function never grants authorization; it only refuses to accept a claim of
    authorization that is not fully specified.
    """
    obj = _keys(live, ("authorized", "status", "authorization"), where="live_trading")
    authorized = _bool(obj["authorized"], "live_trading.authorized")
    status = _enum(obj["status"], LIVE_STATUSES, "live_trading.status")
    auth = obj["authorization"]

    if not authorized:
        if auth is not None:
            raise ContractError(
                "live_trading.authorization must be null while authorized is false"
            )
        if status == "AUTHORIZED":
            raise ContractError("live_trading.status AUTHORIZED contradicts authorized=false")
        return

    if status != "AUTHORIZED":
        raise ContractError("live_trading.authorized=true requires status AUTHORIZED")

    auth_obj = _keys(auth, _AUTH_FIELDS, where="live_trading.authorization")
    if auth_obj["authorized_by"] != "USER":
        raise ContractError(
            "live_trading.authorization.authorized_by must be USER; "
            "no agent, CI result or review can authorize capital"
        )

    scope = _keys(auth_obj["scope"], _SCOPE_FIELDS, where="live_trading.authorization.scope")
    _enum(scope["lane"], frozenset(LANE_KEYS), "live_trading.authorization.scope.lane")
    _text(scope["slice"], "live_trading.authorization.scope.slice")
    _text(scope["service"], "live_trading.authorization.scope.service")
    _enum(scope["stage"], LIVE_STAGES, "live_trading.authorization.scope.stage")
    notional = scope["max_notional_usd"]
    if isinstance(notional, bool) or not isinstance(notional, (int, float)) or notional <= 0:
        raise ContractError(
            "live_trading.authorization.scope.max_notional_usd must be a positive number"
        )

    reference = _text(
        auth_obj["approval_reference"], "live_trading.authorization.approval_reference"
    )
    if not APPROVAL_REFERENCE_RE.match(reference):
        raise ContractError(
            f"live_trading.authorization.approval_reference {reference!r} must match "
            f"{APPROVAL_REFERENCE_RE.pattern}"
        )

    authorized_at = _timestamp(
        auth_obj["authorized_at"], "live_trading.authorization.authorized_at", now=now
    )
    expires_at = _timestamp(
        auth_obj["expires_at"],
        "live_trading.authorization.expires_at",
        now=now,
        allow_future=True,
    )
    if expires_at <= authorized_at:
        raise ContractError("live_trading.authorization.expires_at must be after authorized_at")
    if expires_at <= now:
        raise ContractError(
            "live_trading.authorization has expired; set authorized=false rather than "
            "carrying a stale authorization"
        )
    if _bool(auth_obj["revoked"], "live_trading.authorization.revoked"):
        raise ContractError("live_trading.authorization is revoked but authorized is still true")


# --- priorities ------------------------------------------------------------

_PRIORITY_FIELDS = (
    "priority", "issue", "title", "owner", "reviewer", "status", "profitability_critical",
)


def validate_priorities(rows: Any) -> None:
    if not isinstance(rows, list) or not rows:
        raise ContractError("priorities must be a non-empty list")
    issues: list[int] = []
    for index, row in enumerate(rows):
        where = f"priorities[{index}]"
        obj = _keys(row, _PRIORITY_FIELDS, where=where)
        _enum(obj["priority"], PRIORITIES, f"{where}.priority")
        status = _enum(obj["status"], WORK_STATUSES, f"{where}.status")
        _text(obj["title"], f"{where}.title")
        critical = _bool(obj["profitability_critical"], f"{where}.profitability_critical")
        owner = _enum(obj["owner"], BUILDER_AGENTS, f"{where}.owner")
        reviewer = _enum(obj["reviewer"], AGENTS, f"{where}.reviewer")

        issue = obj["issue"]
        active = status in ACTIVE_WORK_STATUSES
        if active:
            if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
                raise ContractError(f"{where}.issue must be a positive GitHub Issue number")
            issues.append(issue)
            if owner == reviewer:
                raise ContractError(
                    f"{where} has owner == reviewer ({owner}); active work needs an "
                    "independent reviewer"
                )
            if critical and reviewer not in BUILDER_AGENTS:
                raise ContractError(
                    f"{where} is profitability-critical and needs an AI reviewer, got {reviewer}"
                )
        elif issue is not None and (
            isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0
        ):
            raise ContractError(f"{where}.issue must be a positive integer or null")

    duplicates = sorted({issue for issue in issues if issues.count(issue) > 1})
    if duplicates:
        raise ContractError(f"active priorities reference duplicate Issue(s): {duplicates}")


# --- lanes and infrastructure ---------------------------------------------

_LANE_FIELDS = ("name", "status", "facts", "blocker", "next")


def _validate_section(section: Any, *, where: str, statuses: frozenset[str], now: datetime) -> None:
    obj = _keys(section, _LANE_FIELDS, where=where)
    _text(obj["name"], f"{where}.name")
    _enum(obj["status"], statuses, f"{where}.status")
    _text(obj["blocker"], f"{where}.blocker")
    nxt = _text(obj["next"], f"{where}.next", allow_placeholder=True)
    if not NEXT_RE.match(nxt):
        raise ContractError(f"{where}.next must be 'Issue #<n>' or 'NONE', got {nxt!r}")
    validate_facts(obj["facts"], where=f"{where}.facts", now=now)


def validate_lanes(lanes: Any, *, now: datetime) -> None:
    obj = _keys(lanes, LANE_KEYS, where="lanes")
    for key in LANE_KEYS:
        _validate_section(obj[key], where=f"lanes.{key}", statuses=LANE_STATUSES, now=now)


# --- top level -------------------------------------------------------------

_STATE_FIELDS = (
    "schema_version", "snapshot_at", "updated_by", "head_observed", "mission",
    "live_trading", "priorities", "lanes", "infrastructure",
)


def validate_state(
    data: Any, *, now: datetime | None = None, max_age_hours: int = MAX_SNAPSHOT_AGE_HOURS
) -> None:
    now = now or datetime.now(UTC)
    obj = _keys(data, _STATE_FIELDS, where="state.json")

    if obj["schema_version"] != SCHEMA_VERSION:
        raise ContractError(
            f"state.json schema_version must be {SCHEMA_VERSION}, got {obj['schema_version']!r}"
        )
    snapshot_at = _timestamp(obj["snapshot_at"], "state.json snapshot_at", now=now)
    age_hours = (now - snapshot_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise ContractError(
            f"state.json snapshot is {age_hours:.1f}h old, exceeding the {max_age_hours}h bound; "
            "refresh it from current observations rather than treating it as fact"
        )
    _enum(obj["updated_by"], BUILDER_AGENTS, "state.json updated_by")
    head = _text(obj["head_observed"], "state.json head_observed")
    if not COMMIT_RE.match(head):
        raise ContractError(f"state.json head_observed must be a 40-hex commit SHA, got {head!r}")
    _text(obj["mission"], "state.json mission")

    validate_live_trading(obj["live_trading"], now=now)
    validate_priorities(obj["priorities"])
    validate_lanes(obj["lanes"], now=now)
    _validate_section(
        obj["infrastructure"], where="infrastructure", statuses=INFRA_STATUSES, now=now
    )


# --- experiment registry ---------------------------------------------------

_EXPERIMENT_FIELDS = (
    "id", "lane", "hypothesis", "slice", "status", "evidence_level", "result",
    "issue", "pr", "builder", "reviewer", "reviewed_commit", "retest_condition", "updated_at",
)


def validate_experiments(data: Any, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    obj = _keys(data, ("schema_version", "updated_at", "experiments"), where="registry.json")
    if obj["schema_version"] != 1:
        raise ContractError("registry.json schema_version must be 1")
    _timestamp(obj["updated_at"], "registry.json updated_at", now=now)

    rows = obj["experiments"]
    if not isinstance(rows, list):
        raise ContractError("registry.json experiments must be a list")

    seen: set[str] = set()
    for index, row in enumerate(rows):
        where = f"experiments[{index}]"
        entry = _keys(row, _EXPERIMENT_FIELDS, where=where)
        experiment_id = _text(entry["id"], f"{where}.id")
        if not EXPERIMENT_ID_RE.match(experiment_id):
            raise ContractError(f"{where}.id must match EXP-###, got {experiment_id!r}")
        if experiment_id in seen:
            raise ContractError(f"registry.json repeats experiment id {experiment_id}")
        seen.add(experiment_id)

        _enum(entry["lane"], frozenset(LANE_KEYS), f"{where}.lane")
        _text(entry["hypothesis"], f"{where}.hypothesis")
        _text(entry["slice"], f"{where}.slice")
        status = _enum(entry["status"], EXPERIMENT_STATUSES, f"{where}.status")
        _enum(entry["evidence_level"], EVIDENCE_LEVELS, f"{where}.evidence_level")
        result = _enum(entry["result"], EXPERIMENT_RESULTS, f"{where}.result")
        _text(entry["retest_condition"], f"{where}.retest_condition")
        _enum(entry["builder"], BUILDER_AGENTS, f"{where}.builder")
        _timestamp(entry["updated_at"], f"{where}.updated_at", now=now)

        for field in ("issue", "pr"):
            value = entry[field]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ContractError(f"{where}.{field} must be a positive integer or null")

        reviewer = entry["reviewer"]
        if reviewer is not None:
            _enum(reviewer, BUILDER_AGENTS, f"{where}.reviewer")
        reviewed_commit = entry["reviewed_commit"]
        if reviewed_commit is not None and not COMMIT_RE.match(str(reviewed_commit)):
            raise ContractError(f"{where}.reviewed_commit must be a 40-hex SHA or null")

        if status == "COMPLETE":
            # A completed experiment is accepted research evidence. It must name who
            # reviewed it and exactly which commit they reviewed.
            if result == "PENDING":
                raise ContractError(f"{where} is COMPLETE but its result is still PENDING")
            if reviewer is None or reviewed_commit is None:
                raise ContractError(
                    f"{where} is COMPLETE and requires both reviewer and reviewed_commit"
                )
            if reviewer == entry["builder"]:
                raise ContractError(f"{where} was reviewed by its own builder ({reviewer})")
