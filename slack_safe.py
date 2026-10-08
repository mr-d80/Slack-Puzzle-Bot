# slack_safe.py
"""Small defensive helpers for reading Slack API responses.

Mirrors gsheets_safe.py: the point is to keep the awkward, easy-to-get-wrong
details of the client API in one place instead of repeated at every call site.
"""

from typing import Any


def message_ts(resp: Any) -> str:
    """Pull the message ts out of a chat_postMessage response.

    chat_postMessage returns a slack_sdk ``SlackResponse``. It supports ``.get()``
    and ``[]``, but it is NOT a dict subclass, so an ``isinstance(resp, dict)``
    guard always falls through. That was silently returning "" everywhere, which
    meant no thread_ts was ever available to reply under (recaps and monthly
    breakdowns posted as separate top-level messages instead of in a thread) and
    the ts was never recorded in the ledger.

    Returns "" rather than raising when the response is missing or malformed,
    since a missing ts should degrade to an unthreaded post, not kill the run.
    """
    return _field(resp, "ts")


def dm_channel_id(resp: Any) -> str:
    """Pull the DM channel id out of a conversations_open response.

    Same SlackResponse-is-not-a-dict trap as message_ts: the isinstance guard
    meant --dm resolved every channel to None and silently sent nothing.
    """
    channel = _field(resp, "channel", raw=True)
    if isinstance(channel, dict):
        return str(channel.get("id") or "").strip()
    return ""


def _field(resp: Any, key: str, raw: bool = False) -> Any:
    """Read one key off a SlackResponse (or dict), degrading to ""/None."""
    if resp is None:
        return None if raw else ""
    getter = getattr(resp, "get", None)
    if not callable(getter):
        return None if raw else ""
    try:
        value = getter(key)
    except Exception:
        return None if raw else ""
    if raw:
        return value
    return str(value or "").strip()
