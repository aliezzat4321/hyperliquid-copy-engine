"""Deterministic, fail-closed capital risk eligibility."""

from .governor import RiskState, evaluate_risk_eligibility

__all__ = ["RiskState", "evaluate_risk_eligibility"]
