#!/usr/bin/env python3
"""reconcile_day.py

Replay Events and current Slack history -> Scores for a given day, then
(optionally) finalize/post. Slack history supplies the latest text for edited
messages whose ``message_changed`` event was not captured while the bot ran.

Key testing feature:
  --force-repost allows you to re-post standings for a day that is already
  marked as posted in DailyResults, WITHOUT modifying DailyResults or Totals.

Usage:
  python reconcile_day.py 2026-01-13
  python reconcile_day.py 2026-01-13 --no-post
  python reconcile_day.py 2026-01-13 --channel C0123ABCDEF
  python reconcile_day.py 2026-01-13 --force-repost --channel C0TESTCHAN
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import sys
import types

from day_utils import ScoreDayResolver
from slack_safe import message_ts


def _load_bot_module():
    """Load the bot module from the same folder as this script."""
    # Ensure we load .env from the project folder (not just CWD), so
    # reconcile runs behave the same regardless of where you launch it.
    try:
        from dotenv import load_dotenv  # type: ignore

        base = Path(__file__).resolve().parent
        if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
            load_dotenv(dotenv_path=base / ".env", override=False)
    except Exception:
        pass

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
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)  # type: ignore[attr-defined]
                return mod

    raise FileNotFoundError(
        "Could not find score-bot.py (or score-bot_release.py) next to reconcile_day.py"
    )


def _extract_message_fields(payload: Dict[str, Any]) -> Optional[Tuple[str, str, str, str]]:
    event = payload.get("event") or {}
    if not isinstance(event, dict):
        return None
    if event.get("bot_id") or event.get("bot_profile"):
        return None

    subtype = event.get("subtype")
    channel = (event.get("channel") or "").strip()

    # message_changed: pull user/ts/text from the inner edited message so
    # corrections posted via Slack edit get reparsed into Scores.
    if subtype == "message_changed":
        inner = event.get("message") or {}
        if not isinstance(inner, dict) or inner.get("subtype") not in (None, "thread_broadcast"):
            return None
        if inner.get("bot_id") or inner.get("bot_profile"):
            return None
        user_id = (inner.get("user") or "").strip()
        slack_ts = (inner.get("ts") or "").strip()
        text = (inner.get("text") or "").strip()
    elif subtype and subtype != "thread_broadcast":
        # "thread_broadcast" is a human score post made as a thread reply with
        # "Also send to #channel" checked; replay it like a plain message.
        return None
    else:
        user_id = (event.get("user") or "").strip()
        slack_ts = (event.get("ts") or "").strip()
        text = (event.get("text") or "").strip()

    if not channel or not user_id or not slack_ts or not text:
        return None
    return channel, user_id, slack_ts, text


def replay_events_for_day(
    bot_module: Any,
    day: str,
    source_channel: str = "",
    *,
    day_resolver: Optional[ScoreDayResolver] = None,
) -> int:
    """Re-parse all Events for *day* through the current parse_score() and upsert.

    Returns the number of score rows rebuilt/updated.
    Can be called from slash commands or other scripts.
    """
    src_filter = str(source_channel or "").strip() or str(
        getattr(bot_module, "SCORE_CHANNEL_ID", "") or ""
    ).strip()
    if not src_filter:
        raise ValueError("A source channel must be provided or configured before replaying Events.")

    store = bot_module.store
    day_resolver = day_resolver or ScoreDayResolver(store)
    ev_rows = store.events.get_all_values()
    if len(ev_rows) <= 1:
        return 0

    header = ev_rows[0]
    if "payload_json" not in header:
        return 0
    payload_i = header.index("payload_json")

    # Collect every replayed score and upsert them in one batched flush. A
    # busy day holds many Events; upserting them one-by-one here bursts past
    # the Sheets write-per-minute quota (the 429 storms seen in reconcile).
    upserts = []

    for r in ev_rows[1:]:
        if len(r) <= payload_i:
            continue
        raw = r[payload_i]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        extracted = _extract_message_fields(payload)
        if not extracted:
            continue

        channel, user_id, slack_ts, text = extracted

        if src_filter and channel != src_filter:
            continue

        parsed_day = bot_module.day_key_from_ts(slack_ts)
        parse_for_day = getattr(bot_module, "parse_score_for_day", None)
        parsed = parse_for_day(text, parsed_day) if callable(parse_for_day) else bot_module.parse_score(text)
        if not parsed:
            parsed = bot_module.resolve_pinpoint_fail_score(text, parsed_day, store_obj=store)
        if not parsed:
            continue

        effective_day = day_resolver.resolve(parsed_day, parsed)
        if effective_day != day:
            continue
        upserts.append((effective_day, user_id, parsed, slack_ts, text))

    return store.bulk_upsert_scores(upserts)


def sync_slack_history_for_day(bot_module: Any, day: str, source_channel: str = "") -> int:
    """Overlay Events with Slack's current text, including missed edits."""
    channel = source_channel.strip() or getattr(bot_module, "SCORE_CHANNEL_ID", "").strip()
    if not channel:
        return 0

    # Imported lazily because scan_slack_day also exposes this module's CLI
    # reconcile flow.
    from scan_slack_day import sync_slack_history_for_day as sync_history

    return sync_history(
        bot_module,
        day,
        channel,
        reparse_all=True,
        log_events=True,
    )


def run_reconcile(
    *,
    bot: Any,
    day: str,
    post: bool = True,
    channel: str = "",
    source_channel: str = "",
    force_repost: bool = False,
    recap: Optional[bool] = None,
    recap_only: bool = False,
    ai: Optional[bool] = None,
    recap_style: str = "",
    force_finalize: bool = False,
    sync_slack_history: bool = True,
    finalize: bool = True,
    replay_events: bool = True,
) -> Tuple[int, str]:
    store = bot.store

    if ai is True:
        os.environ["AI_REWRITE_ENABLED"] = "1"
    elif ai is False:
        os.environ["AI_REWRITE_ENABLED"] = "0"

    if recap_style:
        os.environ["AI_RECAP_STYLE"] = recap_style

    if recap_only:
        recap = True
    effective_recap = (
        bool(getattr(bot, "POST_DAILY_RECAP", True))
        if recap is None else bool(recap)
    )

    rebuilt = replay_events_for_day(bot, day, source_channel=source_channel) if replay_events else 0
    if sync_slack_history:
        rebuilt += sync_slack_history_for_day(bot, day, source_channel=source_channel)

    if not finalize:
        return rebuilt, f"Replayed {day}; finalization skipped."

    def _post_standings_only(channel_id: str) -> str:
        records = store.load_scores_for_day(day)
        if not records:
            return "No Scores records found for day; nothing to post."

        primary = bot.choose_primary_puzzle_ids(records)
        records = bot.filter_records_to_primary_puzzles(records, primary)

        winners_by_game, awards_by_user, best_display = bot.compute_daily_winners(records, day=day)
        totals_map = {}
        try:
            if hasattr(store, "load_monthly_totals_map") and hasattr(bot, "_month_range_for_day_key"):
                m_start, _m_end = bot._month_range_for_day_key(day)
                totals_map = store.load_monthly_totals_map(m_start, day)
            elif hasattr(store, "load_totals_map"):
                totals_map = store.load_totals_map()
        except Exception:
            totals_map = store.load_totals_map() if hasattr(store, "load_totals_map") else {}

        players = sorted({(r.get("user_id") or "").strip() for r in records if (r.get("user_id") or "").strip()})
        day_msg_ts = ""
        if not recap_only:
            msg = bot.format_summary(day, winners_by_game, awards_by_user, totals_map, best_display, players=players)
            day_msg_ts = message_ts(bot.client.chat_postMessage(channel=channel_id, text=msg))

        if effective_recap:
            try:
                expected = bot.expected_players_for_day(day)
            except Exception:
                expected = len(players)
            try:
                complete = bot.count_complete_players(records, day=day)
            except Exception:
                complete = len(players)

            orig_load_totals = getattr(store, "load_totals_map", None)
            patched_load_totals = False

            try:
                if callable(orig_load_totals):
                    store.load_totals_map = types.MethodType(lambda _self: totals_map, store)  # type: ignore[assignment]
                    patched_load_totals = True

                recap_facts = bot.build_daily_facts(
                    day=day,
                    records=records,
                    awards_by_user=awards_by_user,
                    best_display_by_game=best_display,
                    expected_players=expected,
                    complete_players=complete,
                )
                recap_text = bot.build_daily_recap_text(recap_facts)
            except Exception:
                recap_text = ""
            finally:
                if patched_load_totals and callable(orig_load_totals):
                    store.load_totals_map = orig_load_totals  # type: ignore[assignment]

            post_recap = effective_recap
            in_thread = bool(getattr(bot, "DAILY_RECAP_IN_THREAD", True))
            if post_recap and recap_text.strip():
                if in_thread and day_msg_ts:
                    bot.client.chat_postMessage(channel=channel_id, text=recap_text, thread_ts=day_msg_ts)
                else:
                    bot.client.chat_postMessage(channel=channel_id, text=recap_text)

        if recap_only:
            return f"Re-posted recap-only for {day} to {channel_id} (no ledger changes)."
        return f"Re-posted standings for {day} to {channel_id} (no ledger changes)."

    if post:
        channel_to_post = channel.strip() or getattr(bot, "SCORE_CHANNEL_ID", "").strip()
        if not channel_to_post:
            return rebuilt, "Could not determine channel to post into (pass --channel or set SCORE_CHANNEL_ID)."

        already_posted = store.day_already_posted(day)
        if already_posted and not force_finalize and (force_repost or recap_only or bool(channel.strip())):
            return rebuilt, _post_standings_only(channel_to_post)

        status = bot.finalize_day(
            day,
            channel_to_post,
            post=True,
            force=bool(force_finalize),
            post_scores=not recap_only,
            post_recap=effective_recap,
        )
        return rebuilt, status

    status = bot.finalize_day(day, channel="", post=False, force=bool(force_finalize))
    return rebuilt, status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("day", help="YYYY-MM-DD (SCORE_DAY_TZ, default GMT-12 / Etc/GMT+12)")
    ap.add_argument("--no-post", dest="post", action="store_false", help="Do not post to Slack")
    ap.add_argument("--channel", default="", help="Hard override channel ID for posting (recommended for testing)")
    ap.add_argument(
        "--source-channel",
        default="",
        help="Filter Events by source channel ID (defaults to SCORE_CHANNEL_ID if set).",
    )
    ap.add_argument(
        "--force-repost",
        action="store_true",
        help="Post standings even if the day is already marked posted (does not modify DailyResults/Totals).",
    )
    ap.add_argument(
        "--no-recap",
        dest="recap",
        action="store_false",
        help="Do not post the recap message.",
    )
    ap.add_argument(
        "--recap-only",
        action="store_true",
        help="Post only the recap (no standings/scores message).",
    )
    ap.add_argument(
        "--ai",
        dest="ai",
        action="store_true",
        help="Force-enable AI rewrite for this run (sets AI_REWRITE_ENABLED=1).",
    )
    ap.add_argument(
        "--recap-style",
        default="",
        choices=["", "basic", "flair"],
        help="Override AI_RECAP_STYLE for this run (basic or flair).",
    )
    ap.add_argument(
        "--no-ai",
        dest="ai",
        action="store_false",
        help="Force-disable AI rewrite for this run (sets AI_REWRITE_ENABLED=0).",
    )
    ap.add_argument(
        "--force-finalize",
        action="store_true",
        help="Force finalization even if the day isn't 'ready' (still respects day boundary logic).",
    )
    ap.set_defaults(post=True)
    ap.set_defaults(recap=None)
    ap.set_defaults(ai=None)
    args = ap.parse_args()

    bot = _load_bot_module()

    rebuilt, status = run_reconcile(
        bot=bot,
        day=args.day,
        post=bool(args.post),
        channel=args.channel,
        source_channel=(args.source_channel or ""),
        force_repost=bool(args.force_repost),
        recap=args.recap,
        recap_only=bool(args.recap_only),
        ai=args.ai,
        recap_style=args.recap_style,
        force_finalize=bool(args.force_finalize),
    )
    print(f"Rebuilt/updated {rebuilt} score rows for {args.day} from Events.")
    if args.post:
        print(f"Finalize status: {status}")
    else:
        print(f"Finalize status (no post): {status}")


if __name__ == "__main__":
    main()
