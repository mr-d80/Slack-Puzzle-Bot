"""Small shared helpers for interpreting stored score metrics."""

from typing import Any, Optional


_FAILED_STATUSES = {"failed", "dnf", "conceded", "rescued"}


def higher_is_better(metric_type: str) -> bool:
    """Points increase with performance; time and guesses decrease."""
    return str(metric_type or "").strip().lower() == "points"


def metric_sort_value(value: Any, metric_type: str) -> Any:
    """Return a sortable value where lower always ranks as better."""
    return -value if higher_is_better(metric_type) else value


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def record_is_dnf(record: Any) -> bool:
    """Read explicit failure state, with compatibility for legacy guess rows."""
    raw_status = _field(record, "status")
    if raw_status is not None and str(raw_status).strip():
        return str(raw_status).strip().lower() in _FAILED_STATUSES

    metric_type = str(_field(record, "metric_type", "") or "").strip().lower()
    if metric_type != "guesses":
        return False
    try:
        return int(_field(record, "metric_value", 0)) >= 100
    except (TypeError, ValueError):
        return False


def metric_unit(metric_type: str) -> Optional[str]:
    """Return the display unit for a known metric, or None for unknown types."""
    return {
        "points": "points",
        "guesses": "guesses",
        "time": "seconds",
    }.get(str(metric_type or "").strip().lower())
