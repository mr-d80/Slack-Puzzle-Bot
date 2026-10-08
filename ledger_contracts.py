"""Shared payload builders and readers for persisted ledger summaries."""

from typing import Any, Dict


MONTHLY_SUMMARY_SCHEMA_VERSION = 1


def build_monthly_summary_payload(
    month: str,
    facts: Dict[str, Any],
    standings_text: str,
    recap_text: str,
) -> Dict[str, Any]:
    """Build the versioned MonthlyResults payload written to the ledger."""
    return {
        "schema_version": MONTHLY_SUMMARY_SCHEMA_VERSION,
        "month": month,
        "facts": facts,
        "standings_text": standings_text,
        "recap_text": recap_text,
    }


def monthly_facts_from_summary(payload: Any) -> Dict[str, Any]:
    """Read nested monthly facts, with compatibility for legacy flat rows."""
    if not isinstance(payload, dict):
        return {}
    facts = payload.get("facts")
    if isinstance(facts, dict):
        return facts
    return payload
