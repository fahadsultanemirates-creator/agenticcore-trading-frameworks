"""Bounded independent-market confirmation for already-qualified candidates."""

from __future__ import annotations

from typing import Any, Dict

from config.settings import CryptoSettings
from domain.models import Candidate


MAX_CROSS_MARKET_ADJUSTMENT = 6


def _without_paper_plan(candidate: Candidate) -> None:
    """Do not let a negative confirmation leave a stale sized paper plan."""
    candidate.planned_quantity = None
    candidate.planned_margin_usdt = None
    candidate.planned_stop_price = None
    candidate.planned_take_profit_price = None
    candidate.profit_lock_trigger_price = None
    candidate.profit_lock_stop_price = None
    candidate.planned_target_profit_usdt = None


def apply_cross_market_confirmation(
    candidate: Candidate,
    evidence: Dict[str, Any] | None,
    settings: CryptoSettings,
) -> Candidate:
    """
    Add only a small, transparent confirmation/penalty.

    This function never assigns direction, changes risk sizing, or reopens a
    plan that deterministic scoring rejected. It may withhold an existing local
    paper plan when independent public venues materially conflict.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    candidate.cross_market_evidence = evidence
    candidate.cross_market_status = str(evidence.get("status") or "unavailable")
    candidate.cross_market_agreement = str(evidence.get("agreement") or "neutral")
    candidate.cross_market_adjustment = 0

    if candidate.planned_side not in {"long", "short"}:
        candidate.note += "; cross-market recorded (no deterministic direction)"
        return candidate
    if candidate.confidence is None:
        candidate.note += "; cross-market recorded (insufficient deterministic data)"
        return candidate
    if candidate.cross_market_status not in {"live", "partial"}:
        candidate.note += "; cross-market unavailable"
        return candidate

    agreement = candidate.cross_market_agreement
    live_provider_count = int(evidence.get("live_provider_count") or 0)
    adjustment = 0
    if agreement == candidate.planned_side and live_provider_count >= 1:
        adjustment = MAX_CROSS_MARKET_ADJUSTMENT
        candidate.note += f"; cross-market {agreement} confirmation (+{adjustment})"
    elif agreement in {"long", "short"} and agreement != candidate.planned_side:
        adjustment = -MAX_CROSS_MARKET_ADJUSTMENT
        candidate.note += f"; cross-market conflicts with {candidate.planned_side} ({adjustment})"
    elif agreement == "mixed":
        adjustment = -3
        candidate.note += "; cross-market provider disagreement (-3)"
    else:
        candidate.note += "; cross-market neutral"

    candidate.cross_market_adjustment = adjustment
    candidate.confidence = max(0, min(100, candidate.confidence + adjustment))
    if adjustment < 0 and candidate.confidence < settings.minimum_signal_confidence:
        _without_paper_plan(candidate)
        candidate.note += "; paper plan withheld below confidence floor"
    return candidate