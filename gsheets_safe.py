# gsheets_safe.py
import json
import logging
import math
import random
import re
import threading
import time
from datetime import datetime, timezone

from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError

_GS_LOCK = threading.Lock()
_RETRIABLE_HTTP_STATUS_CODES = frozenset((429, 500, 502, 503, 504))
_GSPREAD_CALL_ATTEMPTS = 5
_GSPREAD_CALL_MAX_DELAY_S = 8.0
logger = logging.getLogger(__name__)


class JsonCellTooLargeError(ValueError):
    """Raised when complete JSON cannot fit safely in a Google Sheets cell."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_retriable_net_error(exc: Exception) -> bool:
    msg = str(exc)
    if isinstance(exc, (RequestsConnectionError, RequestsTimeout, ProtocolError, ConnectionResetError, TimeoutError)):
        return True
    if "WinError 10054" in msg or "Connection aborted" in msg or "ConnectionResetError" in msg:
        return True
    return False


def response_status_code(exc: Exception) -> int | None:
    """Return an HTTP status without relying on Response truthiness.

    Requests Response objects are false for error responses. gspread's APIError
    also uses ``code=-1`` when an HTML gateway body cannot be parsed, so the
    status must come from ``response.status_code`` instead.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    raw = getattr(response, "status_code", None)
    if raw is None:
        raw = getattr(response, "status", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def is_retriable_sheets_error(exc: Exception) -> bool:
    """Whether an error is a transient Sheets HTTP response or network error."""
    status = response_status_code(exc)
    if status is not None:
        return status in _RETRIABLE_HTTP_STATUS_CODES
    return is_retriable_net_error(exc)


def retry_after_seconds(exc: Exception, max_delay: float) -> float | None:
    """Return a finite, positive Retry-After delay capped to the retry budget."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        delay = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(delay) or delay <= 0:
        return None
    return min(delay, max_delay)


def operation_label(fn) -> str:
    method = getattr(fn, "__func__", fn)
    name = getattr(method, "__name__", type(method).__name__)
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:80] or "call"


def gspread_call(fn, *args, **kwargs):
    """
    Thread-safe + retry wrapper for gspread calls.
    Use this around any append_row / update / batch_update calls.
    """
    operation = operation_label(fn)
    delay = 0.5
    last_exc: Exception | None = None

    for attempt in range(1, _GSPREAD_CALL_ATTEMPTS + 1):
        try:
            with _GS_LOCK:
                return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            status = response_status_code(e)
            if not is_retriable_sheets_error(e):
                raise
            if attempt == _GSPREAD_CALL_ATTEMPTS:
                logger.warning(
                    "Sheets operation %s exhausted %s attempts (status=%s, error_type=%s)",
                    operation, _GSPREAD_CALL_ATTEMPTS, status, type(e).__name__,
                )
                raise
            sleep_s = retry_after_seconds(e, _GSPREAD_CALL_MAX_DELAY_S)
            if sleep_s is None:
                sleep_s = min(delay, _GSPREAD_CALL_MAX_DELAY_S)
                sleep_s += random.random() * sleep_s
            logger.warning(
                "Sheets operation %s failed (status=%s, error_type=%s) on attempt %s/%s; retrying in %.2fs",
                operation, status, type(e).__name__, attempt,
                _GSPREAD_CALL_ATTEMPTS, sleep_s,
            )
            time.sleep(sleep_s)
            delay = min(delay * 2.0, 8.0)

    if last_exc:
        raise last_exc


def truncate_for_cell(s: str, limit: int = 49000) -> str:
    # Google Sheets cells have hard limits. Keep a safety margin.
    if len(s) <= limit:
        return s
    return s[:limit] + "…(truncated)"


def serialize_json_for_cell(value, limit: int = 49000) -> str:
    """Serialize complete compact JSON that fits the Sheets cell limit.

    JSON is ledger data and must never be truncated. Use a conservative UTF-16
    size budget below the cell ceiling and fail before callers issue a ledger
    write when the complete value is too large.
    """
    serialized = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    utf16_units = len(serialized.encode("utf-16-le", errors="surrogatepass")) // 2
    if utf16_units > limit:
        raise JsonCellTooLargeError(
            f"Serialized JSON is {utf16_units} UTF-16 code units; conservative JSON cell budget is {limit}."
        )
    return serialized


def spool_jsonl(path: str, record: dict) -> None:
    """
    Last-resort: write to disk so you don’t lose events if Sheets is down.
    """
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("Failed to spool JSONL locally")
