#!/usr/bin/env python3
"""
scan_slack_day.py

Backfill Scores by scanning Slack message history for a given SCORE_DAY_TZ day.

Why this exists:
- reconcile_day.py normally replays *Events* and overlays current Slack history.
- This script provides the explicit history-scan/backfill CLI for messages missed while the bot was offline.
- This script fetches Slack history directly (including thread replies), parses scores with your existing
  parse_score(), and upserts into Scores.

Usage:
  python scan_slack_day.py 2026-01-19
  python scan_slack_day.py 2026-01-19 --channel C0123ABCDEF
  python scan_slack_day.py 2026-01-19 --no-replies
  python scan_slack_day.py 2026-01-19 --reparse-all
  python scan_slack_day.py 2026-01-19 --dry-run
  python scan_slack_day.py 2026-01-19 --finalize
  python scan_slack_day.py 2026-01-19 --finalize --no-post

Notes:
- Requires your bot token to have appropriate history scopes for the channel type
  (public: channels:history, private: groups:history, plus channels:read is often needed).
- Uses the same SCORE_DAY_TZ and parse_score logic from score-bot.py.
"""

import argparse
import hashlib
import json
import reconcile_day
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from slack_sdk.errors import SlackApiError


def _load_bot_module():
    """Load the bot module from the same folder as this script."""
    import importlib.util

    base = Path(__file__).resolve().parent
    candidates = [
        base / "score-bot_release.py",
        base / "score_bot_release.py",
        base / "score-bot.py",
        base / "score_bot.py",
        base / "score-bot_refactor.py",
        base / "score_bot_refactor.py",
    ]

    for p in candidates:
        if p.exists():
            spec = importlib.util.spec_from_file_location("score_bot_loaded", str(p))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod  # register before exec so sub-module singletons (game_registry) resolve correctly
                spec.loader.exec_module(mod)  # type: ignore[attr-defined]
                return mod

    raise FileNotFoundError(
        "Could not find score-bot.py (or score-bot_release.py) next to scan_slack_day.py"
    )


def _day_window_utc(bot, day_key: str) -> Tuple[float, float]:
    """
    Compute [start, end) window in UTC for the given day key in SCORE_DAY_TZ.
    This matches day_key_from_ts() bucketing.
    """
    d = datetime.strptime(day_key, bot.DAY_FMT).date()
    start_local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=bot.SCORE_DAY_TZ)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).timestamp()
    end_utc = end_local.astimezone(timezone.utc).timestamp()
    return start_utc, end_utc


def _slack_call_with_retry(fn, **kwargs) -> Dict[str, Any]:
    """Slack API wrapper that handles rate limits."""
    while True:
        try:
            return fn(**kwargs)
        except SlackApiError as e:
            status = getattr(e.response, "status_code", None)
            retry_after = None
            try:
                retry_after = int(e.response.headers.get("Retry-After", "0"))
            except Exception:
                retry_after = None

            if status == 429 and retry_after:
                time.sleep(retry_after + 1)
                continue
            raise


def _fetch_channel_messages(
    bot,
    channel: str,
    oldest_ts: float,
    latest_ts: float,
) -> List[Dict[str, Any]]:
    """
    Fetch all top-level messages in [oldest_ts, latest_ts) from the channel.
    Slack returns newest-first; caller can sort by ts if needed.
    """
    messages: List[Dict[str, Any]] = []
    cursor = None

    # Exclude the exact end boundary to avoid leaking into the next bucket.
    # Also cap at "now" — Slack's conversations_history can return an empty page
    # when `latest` is a future timestamp (happens when scanning an in-progress
    # SCORE_DAY_TZ day whose exclusive window end is still in the future).
    now_ts = datetime.now(tz=timezone.utc).timestamp()
    latest = max(oldest_ts, min(latest_ts, now_ts) - 0.000001)

    while True:
        resp = _slack_call_with_retry(
            bot.client.conversations_history,
            channel=channel,
            oldest=str(oldest_ts),
            latest=str(latest),
            inclusive=True,
            limit=200,
            cursor=cursor,
        )
        batch = resp.get("messages") or []
        if isinstance(batch, list):
            messages.extend(batch)

        meta = resp.get("response_metadata") or {}
        cursor = (meta.get("next_cursor") or "").strip()
        if not cursor:
            break

    return messages


def _fetch_thread_replies(bot, channel: str, thread_ts: str) -> List[Dict[str, Any]]:
    """
    Fetch replies for a thread.
    Slack returns the parent message as the first entry; we filter it out to avoid duplication.
    """
    out: List[Dict[str, Any]] = []
    cursor = None
    while True:
        resp = _slack_call_with_retry(
            bot.client.conversations_replies,
            channel=channel,
            ts=thread_ts,
            limit=200,
            cursor=cursor,
        )
        batch = resp.get("messages") or []
        if isinstance(batch, list):
            for m in batch:
                if isinstance(m, dict) and str(m.get("ts") or "").strip() == str(thread_ts).strip():
                    continue  # skip parent
                out.append(m)

        meta = resp.get("response_metadata") or {}
        cursor = (meta.get("next_cursor") or "").strip()
        if not cursor:
            break
    return out


@dataclass(frozen=True)
class CandidateMsg:
    channel: str
    user_id: str
    slack_ts: str
    text: str


def _extract_candidates(
    bot,
    channel: str,
    msgs: Iterable[Dict[str, Any]],
    day_key: str,
) -> List[CandidateMsg]:
    """
    Keep only unique user messages in this channel that:
    - are not subtype/bot messages, except "thread_broadcast" (a human post
      made as a thread reply with "Also send to #channel" checked)
    - have text/user/ts
    - map to the target puzzle day, or the message's local day for undated shares
    """
    out: List[CandidateMsg] = []
    seen_ts: Set[str] = set()

    for m in msgs:
        if not isinstance(m, dict):
            continue
        if m.get("bot_id") or m.get("bot_profile"):
            continue
        subtype = m.get("subtype")
        if subtype and subtype != "thread_broadcast":
            continue

        user_id = (m.get("user") or "").strip()
        slack_ts = (m.get("ts") or "").strip()
        text = (m.get("text") or "").strip()

        if not user_id or not slack_ts or not text:
            continue
        if slack_ts in seen_ts:
            continue
        seen_ts.add(slack_ts)

        try:
            message_day = bot.day_key_from_ts(slack_ts)
            parse_for_day = getattr(bot, "parse_score_for_day", None)
            parsed = parse_for_day(text, message_day) if callable(parse_for_day) else None
            if (getattr(parsed, "score_day", None) or message_day) != day_key:
                continue
        except Exception:
            continue

        out.append(CandidateMsg(channel=channel, user_id=user_id, slack_ts=slack_ts, text=text))

    return out


def _history_event_id(candidate: CandidateMsg) -> str:
    """Return an event id that changes when the Slack message text changes."""
    revision = hashlib.sha256(
        f"{candidate.user_id}\0{candidate.text}".encode("utf-8")
    ).hexdigest()[:16]
    return f"history_scan:{candidate.channel}:{candidate.slack_ts}:{revision}"


def sync_slack_history_for_day(
    bot: Any,
    day: str,
    channel: str,
    *,
    reparse_all: bool = True,
    log_events: bool = True,
) -> int:
    """Fetch Slack's current message text for a day and upsert parseable scores.

    This is intentionally callable by reconcile_day. Unlike an Events replay,
    Slack history reflects the latest version of an edited message even when
    the bot never received the corresponding ``message_changed`` event.
    """
    store = bot.store
    oldest_utc, latest_utc = _day_window_utc(bot, day)
    thread_lookback_secs = 2 * 86400
    top_msgs = _fetch_channel_messages(
        bot,
        channel,
        oldest_utc - thread_lookback_secs,
        latest_utc + 2 * 86400,
    )

    all_msgs: List[Dict[str, Any]] = list(top_msgs)
    seen_threads: Set[str] = set()
    for message in top_msgs:
        if not isinstance(message, dict) or int(message.get("reply_count") or 0) <= 0:
            continue

        # conversations_history normally returns a thread parent with its own
        # ts and reply_count, but no thread_ts. Replies themselves use
        # thread_ts, so accept either representation.
        thread_ts = (message.get("thread_ts") or message.get("ts") or "").strip()
        if not thread_ts or thread_ts in seen_threads:
            continue
        seen_threads.add(thread_ts)
        all_msgs.extend(_fetch_thread_replies(bot, channel, thread_ts))

    candidates = _extract_candidates(bot, channel, all_msgs, day)

    def _candidate_ts(candidate: CandidateMsg) -> float:
        try:
            return float(candidate.slack_ts)
        except (TypeError, ValueError):
            return 0.0

    candidates.sort(key=_candidate_ts)

    existing_slack_ts: Set[str] = set()
    if not reparse_all:
        existing_slack_ts = {
            (row.get("slack_ts") or "").strip()
            for row in store.load_scores_for_day(day)
            if (row.get("slack_ts") or "").strip()
        }

    # Accumulate all upserts/events and flush in bulk. Writing one score (and
    # one event) per candidate here bursts past the Sheets "write requests per
    # minute per user" quota on busy days; batching keeps a full-day reconcile
    # to a couple of writes total.
    upserts: List[Tuple[str, str, Any, str, str]] = []
    pending_events: List[Tuple[str, dict]] = []

    for candidate in candidates:
        if candidate.slack_ts in existing_slack_ts:
            continue
        parse_for_day = getattr(bot, "parse_score_for_day", None)
        message_day = bot.day_key_from_ts(candidate.slack_ts)
        parsed = parse_for_day(candidate.text, message_day) if callable(parse_for_day) else bot.parse_score(candidate.text)
        if not parsed:
            parsed = bot.resolve_pinpoint_fail_score(candidate.text, day, store_obj=store)
        if not parsed:
            continue

        upserts.append((day, candidate.user_id, parsed, candidate.slack_ts, candidate.text))

        if log_events:
            event_id = _history_event_id(candidate)
            if not store.seen_event(event_id):
                pending_events.append((
                    event_id,
                    {
                        "source": "history_scan",
                        "event": {
                            "channel": candidate.channel,
                            "user": candidate.user_id,
                            "ts": candidate.slack_ts,
                            "text": candidate.text,
                        },
                    },
                ))

    rebuilt = store.bulk_upsert_scores(upserts)
    if pending_events:
        store.bulk_log_events(pending_events)

    return rebuilt


def main():
    bot = _load_bot_module()
    store = bot.store

    ap = argparse.ArgumentParser()
    ap.add_argument("day", help="YYYY-MM-DD (bucketed using SCORE_DAY_TZ, default America/Vancouver)")
    ap.add_argument("--channel", default="", help="Channel ID to scan (defaults to SCORE_CHANNEL_ID env var)")
    ap.add_argument("--no-replies", action="store_true", help="Do NOT scan thread replies (default scans replies)")
    ap.add_argument("--reparse-all", action="store_true", help="Re-parse even if slack_ts is already in Scores")
    ap.add_argument("--no-log-events", action="store_true", help="Do not write synthetic history entries to Events")
    ap.add_argument("--dry-run", action="store_true", help="Print what would change, but do not write to Sheets")
    ap.add_argument("--no-reconcile", action="store_true", help="Do not replay the day through reconcile_day after scanning")
    ap.add_argument("--finalize", action="store_true", help="Finalize the day after backfill (writes DailyResults/Totals)")
    ap.add_argument("--no-post", dest="post", action="store_false", help="When used with --finalize, do not post to Slack")
    ap.set_defaults(post=True)
    args = ap.parse_args()

    day = args.day.strip()
    channel = args.channel.strip() or getattr(bot, "SCORE_CHANNEL_ID", "").strip()
    if not channel:
        raise SystemExit("No channel provided. Pass --channel or set SCORE_CHANNEL_ID in env.")

    # Compute the strict day window (used for candidate filtering).
    oldest_utc, latest_utc = _day_window_utc(bot, day)

    # Fetch top-level messages with a 2-day lookback beyond the window start.
    # Scores are often posted as replies to a daily thread whose *parent* was
    # created the day before.  Without this, the parent never lands in top_msgs,
    # so its reply-fetch is skipped entirely.  _extract_candidates still gates
    # candidates on their effective score day, so the wider window adds no false hits.
    THREAD_LOOKBACK_SECS = 2 * 86400
    top_msgs = _fetch_channel_messages(bot, channel, oldest_utc - THREAD_LOOKBACK_SECS, latest_utc + 2 * 86400)

    # Always scan replies unless explicitly disabled
    all_msgs: List[Dict[str, Any]] = list(top_msgs)
    if not args.no_replies:
        seen_threads: Set[str] = set()
        for m in top_msgs:
            if not isinstance(m, dict):
                continue
            # Only bother if Slack thinks there are replies
            rc = m.get("reply_count")
            if rc is None or int(rc or 0) <= 0:
                continue

            thread_ts = (m.get("thread_ts") or m.get("ts") or "").strip()
            if not thread_ts or thread_ts in seen_threads:
                continue

            seen_threads.add(thread_ts)
            replies = _fetch_thread_replies(bot, channel, thread_ts)
            all_msgs.extend(replies)

    # Extract candidate messages that belong to this day bucket
    candidates = _extract_candidates(bot, channel, all_msgs, day)

    # Sort oldest->newest so later scores overwrite earlier ones naturally
    def _tsf(ts: str) -> float:
        try:
            return float(ts)
        except Exception:
            return 0.0

    candidates.sort(key=lambda c: _tsf(c.slack_ts))

    # Load existing Scores-day snapshot for compare/reporting
    existing = store.load_scores_for_day(day)
    existing_slack_ts: Set[str] = {
        (r.get("slack_ts") or "").strip()
        for r in existing
        if (r.get("slack_ts") or "").strip()
    }

    # Key by upsert identity (user,game,puzzle_id)
    existing_keys: Set[Tuple[str, str, int]] = set()
    for r in existing:
        uid = (r.get("user_id") or "").strip()
        game = (r.get("game") or "").strip()
        pid_raw = (r.get("puzzle_id") or "").strip()
        if not uid or not game or not pid_raw:
            continue
        try:
            pid = int(pid_raw)
        except Exception:
            continue
        existing_keys.add((uid, bot.normalize_game(game), pid))

    found_scores = 0
    would_insert = 0
    would_update = 0
    skipped_already_seen_ts = 0
    parsed_fail = 0

    run_seen_keys: Set[Tuple[str, str, int]] = set()

    # Collected for a batched flush after the loop (avoids per-candidate writes
    # tripping the Sheets write-per-minute quota); dry-run collects nothing.
    upserts: List[Tuple[str, str, Any, str, str]] = []
    pending_events: List[Tuple[str, dict]] = []

    for c in candidates:
        if (not args.reparse_all) and (c.slack_ts in existing_slack_ts):
            skipped_already_seen_ts += 1
            continue

        parse_for_day = getattr(bot, "parse_score_for_day", None)
        message_day = bot.day_key_from_ts(c.slack_ts)
        parsed = parse_for_day(c.text, message_day) if callable(parse_for_day) else bot.parse_score(c.text)
        if not parsed:
            parsed = bot.resolve_pinpoint_fail_score(c.text, day, store_obj=store)
        if not parsed:
            parsed_fail += 1
            continue

        found_scores += 1
        key = (c.user_id, parsed.game, int(parsed.puzzle_id))

        if key in existing_keys or key in run_seen_keys:
            would_update += 1
        else:
            would_insert += 1
            run_seen_keys.add(key)

        if args.dry_run:
            continue

        upserts.append((day, c.user_id, parsed, c.slack_ts, c.text))

        # Optionally log a synthetic event so reconcile_day can replay it too
        if not args.no_log_events:
            event_id = _history_event_id(c)
            if not store.seen_event(event_id):
                pending_events.append((
                    event_id,
                    {
                        "source": "history_scan",
                        "event": {
                            "channel": c.channel,
                            "user": c.user_id,
                            "ts": c.slack_ts,
                            "text": c.text,
                        },
                    },
                ))

    if not args.dry_run:
        store.bulk_upsert_scores(upserts)
        if pending_events:
            store.bulk_log_events(pending_events)

    print(f"Day: {day}")
    print(f"Channel: {channel}")
    print(f"Slack messages fetched (top-level): {len(top_msgs)}")
    print(f"Slack messages fetched (incl. replies): {len(all_msgs)}" if not args.no_replies else "Thread replies: disabled")
    print(f"Candidate user msgs in bucket: {len(candidates)}")
    print(f"Parseable score posts found: {found_scores}")
    print(f"Would insert: {would_insert}")
    print(f"Would update: {would_update}")
    print(f"Skipped (slack_ts already present): {skipped_already_seen_ts}")
    print(f"Non-score msgs / parse fails: {parsed_fail}")
    if args.dry_run:
        print("DRY RUN: no Sheets changes were made.")
        return

    if args.no_reconcile:
        return
    if args.no_log_events:
        print("Reconcile skipped: --no-log-events prevents history entries from being replayed via Events.")
        return

    rebuilt, status = reconcile_day.run_reconcile(
        bot=bot,
        day=day,
        post=bool(args.finalize),
        channel=channel,
        source_channel=channel,
        sync_slack_history=False,
    )
    print(f"Reconcile rebuilt/updated from Events: {rebuilt}")
    if args.finalize:
        print(f"Finalize status: {status}")
    else:
        print("Reconcile completed: day replayed from Events (no finalize requested).")


if __name__ == "__main__":
    main()
