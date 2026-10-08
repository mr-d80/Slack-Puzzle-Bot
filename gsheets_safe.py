# gsheets_safe.py
import json
import logging
import random
import threading
import time
from datetime import datetime, timezone

from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from urllib3.exceptions import ProtocolError

_GS_LOCK = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_retriable_net_error(exc: Exception) -> bool:
    msg = str(exc)
    if isinstance(exc, (RequestsConnectionError, RequestsTimeout, ProtocolError, ConnectionResetError, TimeoutError)):
        return True
    if "WinError 10054" in msg or "Connection aborted" in msg or "ConnectionResetError" in msg:
        return True
    return False


def gspread_call(fn, *args, **kwargs):
    """
    Thread-safe + retry wrapper for gspread calls.
    Use this around any append_row / update / batch_update calls.
    """
    delay = 0.5
    last_exc: Exception | None = None

    for attempt in range(1, 6):  # 5 tries
        try:
            with _GS_LOCK:
                return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if (not is_retriable_net_error(e)) or attempt == 5:
                raise
            time.sleep(delay + random.random() * delay)  # jitter
            delay = min(delay * 2.0, 8.0)

    if last_exc:
        raise last_exc


def truncate_for_cell(s: str, limit: int = 49000) -> str:
    # Google Sheets cells have hard limits. Keep a safety margin.
    if len(s) <= limit:
        return s
    return s[:limit] + "…(truncated)"


def spool_jsonl(path: str, record: dict) -> None:
    """
    Last-resort: write to disk so you don’t lose events if Sheets is down.
    """
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("Failed to spool JSONL locally")