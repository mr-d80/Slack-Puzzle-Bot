#!/usr/bin/env python3
"""score-bot.py

Slack Puzzle Tracker — main entry point.

Wires up the Slack app, creates the SheetStore and WebClient globals,
registers slash commands, and handles incoming messages (score parsing
and natural-language queries).

All business logic lives in dedicated modules:
  - config.py          — environment, logging, constants
  - game_registry.py   — dynamic game registry + compiled regexes
  - parser.py          — score parsing (ParsedScore, parse_score)
  - day_utils.py       — day-key arithmetic, puzzle filtering
  - sheet_store.py     — Google Sheets data layer (SheetStore)
  - scoring.py         — winner computation, summary formatting
  - finalization.py    — finalize_day / finalize_due_days
  - monthly_summary.py — month-end standings, records, and commentary
  - insights.py        — daily recap generation + AI rewriting
  - nl_query.py        — natural-language query translation + execution
"""

import sys
import types
import time
from datetime import datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.web import WebClient

# ---------------------------------------------------------------------------
# Import all modules (re-export for backward compatibility with
# reconcile_day.py, scan_slack_day.py, and slash-command modules that
# dynamically load score-bot.py and access attributes on the module).
# ---------------------------------------------------------------------------
from config import (
    TZ, SCORE_DAY_TZ, DAY_FMT,
    SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SCORE_CHANNEL_ID, ADMIN_USER_IDS,
    SPREADSHEET_ID, GOOGLE_SA_FILE,
    PLAYERS_EXPECTED, TROPHY, NECKTIE,
    POST_DAILY_RECAP, DAILY_RECAP_IN_THREAD,
    NL_QUERY_ENABLED, NL_QUERY_IN_THREAD,
    validate_runtime_config,
)

try:
    validate_runtime_config()
except ValueError as exc:
    raise SystemExit(str(exc)) from None

from game_registry import game_registry, _DEFAULT_GAMES, GameRegistryCache
from parser import (
    ParsedScore, parse_score, parse_score_for_day, normalize_game, canonical_user_id,
    strip_mrkdwn_wrap,
)
from day_utils import (
    day_key_from_ts, day_end_utc_from_day_key, is_day_closed,
    prev_day_key, next_day_key, month_range_for_day_key,
    month_key_for_day, month_bounds, prev_month_key, is_month_closed,
    choose_primary_puzzle_ids, filter_records_to_primary_puzzles,
    expected_players_for_day, count_complete_players,
    primary_puzzle_id_for_day_game, bump_day_if_future_puzzle,
    move_future_puzzle_scores, resolve_pinpoint_fail_score,
)
from sheet_store import SheetStore
from scoring import compute_daily_winners, format_summary, compact_awards
from finalization import finalize_day as _finalize_day_impl, finalize_due_days as _finalize_due_days_impl
from monthly_summary import (
    finalize_month as _finalize_month_impl,
    finalize_due_months as _finalize_due_months_impl,
    build_monthly_facts, load_month_facts, render_monthly_standings_text,
)
from gsheets_safe import utc_now_iso, spool_jsonl
from insights import build_daily_facts, build_daily_recap_text, build_monthly_recap_text
from nl_query import is_nl_query_text, answer_nl_query

from recap_commands import register_recap_commands
from update_commands import register_update_commands


# ---------------------------------------------------------------------------
# NL-query routing
# ---------------------------------------------------------------------------
def _looks_like_nl_query(text: str) -> bool:
    """Treat `bot:`, `bot,`, and leading `?` as explicit history queries."""
    return is_nl_query_text((text or "").strip())


# ---------------------------------------------------------------------------
# Thin wrappers that bind the module-level store/client globals
# (so slash commands and utility scripts can call finalize_day(day, channel)
# with the same signature as the original monolith).
# ---------------------------------------------------------------------------
def finalize_day(day, channel, post=True, force=False, now_utc=None):
    return _finalize_day_impl(day, channel, store, client, post=post, force=force, now_utc=now_utc)


def finalize_due_days(channel, post=True):
    return _finalize_due_days_impl(channel, store, client, post=post)


def finalize_month(month, channel, post=True, force=False, now_utc=None):
    return _finalize_month_impl(month, channel, store, client, post=post, force=force, now_utc=now_utc)


def finalize_due_months(channel, post=True, now_utc=None):
    return _finalize_due_months_impl(channel, store, client, post=post, now_utc=now_utc)


# ---------------------------------------------------------------------------
# Slack app + globals
# ---------------------------------------------------------------------------
app = App(token=SLACK_BOT_TOKEN)
client = WebClient(token=SLACK_BOT_TOKEN)
store = SheetStore(SPREADSHEET_ID, GOOGLE_SA_FILE)

_NL_USER_NAME_CACHE = {}
_NL_USER_NAME_CACHE_FETCHED_AT = 0.0


def _normalize_nl_user_name(value):
    return " ".join(str(value or "").strip().casefold().split())


def _resolve_nl_user_name(name):
    """Resolve an exact Slack display name/real name, caching the directory for an hour."""
    global _NL_USER_NAME_CACHE, _NL_USER_NAME_CACHE_FETCHED_AT
    normalized = _normalize_nl_user_name(name)
    if not normalized:
        return ""
    if time.time() - _NL_USER_NAME_CACHE_FETCHED_AT > 3600:
        directory = {}
        cursor = None
        try:
            while True:
                response = client.users_list(limit=200, cursor=cursor) if cursor else client.users_list(limit=200)
                for member in response.get("members", []) or []:
                    if member.get("deleted") or member.get("is_bot"):
                        continue
                    profile = member.get("profile") or {}
                    uid = str(member.get("id") or "").strip()
                    if not uid:
                        continue
                    for alias in (profile.get("display_name"), profile.get("real_name"), member.get("real_name"), member.get("name")):
                        key = _normalize_nl_user_name(alias)
                        if key:
                            directory.setdefault(key, set()).add(uid)
                cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor:
                    break
            _NL_USER_NAME_CACHE = directory
        except Exception:
            # If Slack has no users:read scope or is temporarily unavailable,
            # preserve any prior cache and let the query invite an @mention.
            pass
        finally:
            _NL_USER_NAME_CACHE_FETCHED_AT = time.time()
    matches = _NL_USER_NAME_CACHE.get(normalized) or set()
    return next(iter(matches)) if len(matches) == 1 else ""

# Register slash commands (pass this module so they can access store, etc.)
_this_mod = sys.modules.get(__name__)
if _this_mod is None:
    _this_mod = types.SimpleNamespace(**globals())

register_recap_commands(app, _this_mod)
register_update_commands(app, _this_mod)

# Load the dynamic game registry from the GameRegistry sheet.
game_registry.rebuild(store)


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------
@app.event("message")
def handle_message(body, event, logger):
    # Keep the shared Events ledger limited to human messages from the single
    # configured score channel. An empty configuration fails closed here.
    subtype = event.get("subtype")
    channel = event.get("channel", "")
    if not SCORE_CHANNEL_ID or channel != SCORE_CHANNEL_ID:
        return
    if subtype not in (None, "message_changed", "thread_broadcast"):
        return

    # Treat edits as fresh posts: promote event["message"] to top-level
    # so a corrected score replaces the prior value via upsert_score.
    if subtype == "message_changed":
        inner = event.get("message") or {}
        if (
            not isinstance(inner, dict)
            or inner.get("subtype") not in (None, "thread_broadcast")
            or inner.get("bot_id")
            or inner.get("bot_profile")
        ):
            return
        text = (inner.get("text") or "").strip()
        user_id = canonical_user_id(inner.get("user") or "")
        slack_ts = (inner.get("ts") or "").strip()
    elif event.get("bot_id") or event.get("bot_profile"):
        return
    else:
        # "thread_broadcast" is an ordinary human post made as a thread reply
        # with "Also send to #channel" checked, so keep it in channel history.
        text = (event.get("text") or "").strip()
        user_id = canonical_user_id(event.get("user") or "")
        slack_ts = (event.get("ts") or "").strip()

    if not text or not user_id or not slack_ts:
        return

    # Dedup + persist only supported, human, in-channel events. Keep ordinary
    # score-channel text so later registry changes can re-parse missed scores.
    event_id = body.get("event_id", "")
    if event_id:
        if not store.claim_event(event_id):
            return
        store.log_event(event_id, body)

    message_day = day_key_from_ts(slack_ts)
    parsed = parse_score_for_day(text, message_day)

    # "pinpoint fail": a player conceding Pinpoint without a shareable score
    # card. Resolve it to a worst-case DNF against the day's Pinpoint puzzle so
    # it still counts as a submission. Applies to fresh posts and edits alike.
    if not parsed:
        fail_day = day_key_from_ts(slack_ts)
        parsed = resolve_pinpoint_fail_score(text, fail_day, store_obj=store)

    if not parsed:
        # Natural-language history queries require an explicit `bot:` prefix.
        # Don't trigger NL-query replies for edits — only for fresh posts.
        if subtype != "message_changed" and NL_QUERY_ENABLED and _looks_like_nl_query(text):
            try:
                today = datetime.now(TZ).date()
                answer = answer_nl_query(
                    text,
                    asker_user_id=user_id,
                    store=store,
                    games=game_registry.game_names(),
                    normalize_game=normalize_game,
                    today=today,
                    slack_ts=slack_ts,
                    resolve_user_name=_resolve_nl_user_name,
                )
                kwargs = {"channel": channel, "text": answer}
                if NL_QUERY_IN_THREAD and slack_ts:
                    kwargs["thread_ts"] = slack_ts
                client.chat_postMessage(**kwargs)
            except Exception as e:
                logger.exception(f"NL query failed: {e}")
        return

    day = getattr(parsed, "score_day", None) or message_day
    if not getattr(parsed, "score_day", None):
        day = bump_day_if_future_puzzle(day, parsed, store_obj=store)

    try:
        store.upsert_score(day, user_id, parsed, slack_ts, text)
    except Exception as e:
        logger.exception(
            "Failed to persist score day=%s user=%s game=%s puzzle_id=%s ts=%s",
            day,
            user_id,
            parsed.game,
            parsed.puzzle_id,
            slack_ts,
        )
        try:
            spool_jsonl(
                "scores_spool.jsonl",
                {
                    "day": day,
                    "user_id": user_id,
                    "slack_ts": slack_ts,
                    "raw_text": text,
                    "parsed": {
                        "game": parsed.game,
                        "puzzle_id": parsed.puzzle_id,
                        "metric_type": parsed.metric_type,
                        "metric_value": parsed.metric_value,
                        "display": parsed.display,
                        "tiebreak_value": parsed.tiebreak_value,
                        "status": getattr(parsed, "status", ""),
                        "score_day": getattr(parsed, "score_day", None),
                    },
                    "error": repr(e),
                    "logged_at": utc_now_iso(),
                },
            )
        except Exception:
            logger.exception("Failed to spool score write after persistence error")
        return

    # Single finalization path
    try:
        finalize_due_days(channel, post=True)
    except Exception as e:
        logger.exception(f"finalize_day failed: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    SocketModeHandler(app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
