"""finalization.py

Day finalization: the single path that writes DailyResults and Totals.

All scoring, recap generation, and Slack posting for a completed day
flows through ``finalize_day``. Once a day is finalized, the same path checks
whether a month has rolled over and hands off to ``monthly_summary``.
"""

import logging
import threading
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List, Optional

from awards import unpack_awards
from config import POST_DAILY_RECAP, DAILY_RECAP_IN_THREAD
from day_utils import (
    is_day_closed, choose_primary_puzzle_ids, filter_records_to_primary_puzzles,
    expected_players_for_day, count_complete_players, month_range_for_day_key,
    move_future_puzzle_scores,
)
from scoring import compute_daily_winners, format_summary
from insights import build_daily_facts, build_daily_recap_text
from monthly_summary import finalize_due_months
from slack_safe import message_ts


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------
def finalize_day(
    day: str,
    channel: str,
    store: Any,
    client: Any,
    post: bool = True,
    force: bool = False,
    now_utc: Optional[datetime] = None,
) -> str:
    """
    Finalization is the only place that can affect DailyResults and Totals.
    Totals is derived from DailyResults.summary_json.

    v2.1 rules:
      - Day boundary is SCORE_DAY_TZ (default America/Vancouver).
      - A day can post early once enough *complete* players have posted all tracked games.
      - Missing players/games are allowed once the day closes (America/Vancouver cutoff).
      - Per game, we only score the dominant puzzle_id for that day.
      - Early next-day posts (higher puzzle_id) are moved forward into the next day bucket.
    """
    if store.day_already_posted(day) and not force:
        return 'already_posted'

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    records = store.load_scores_for_day(day)
    if not records:
        return 'no_scores'

    primary = choose_primary_puzzle_ids(records)

    # Move likely-next-day scores forward (higher puzzle_id than today's dominant puzzle).
    moved_any = move_future_puzzle_scores(day, records, primary, store)

    if moved_any:
        records = store.load_scores_for_day(day)
        if not records:
            return 'no_scores'
        primary = choose_primary_puzzle_ids(records)

    records_primary = filter_records_to_primary_puzzles(records, primary)

    expected = expected_players_for_day(day, store_obj=store)
    complete = count_complete_players(records_primary, day=day)

    if (not force) and (complete < expected) and (not is_day_closed(day, now_utc)):
        return 'not_ready'

    # The score day, not the clock, decides the scoring rules, so a day that
    # closes after midnight keeps the rules it was played under.
    winners_by_game, awards_by_user, best_display = compute_daily_winners(records_primary, day=day)

    recap_facts = build_daily_facts(
        day=day,
        records=records_primary,
        awards_by_user=awards_by_user,
        best_display_by_game=best_display,
        expected_players=expected,
        complete_players=complete,
    )
    recap_text = build_daily_recap_text(recap_facts)

    # For the summary text (and Slack post), compute *monthly* running totals.
    m_start, _m_end = month_range_for_day_key(day)
    already_posted = store.day_already_posted(day)
    if already_posted:
        yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        monthly_totals_map = store.load_monthly_totals_map(m_start, yesterday) if m_start <= yesterday else {}
    else:
        monthly_totals_map = store.load_monthly_totals_map(m_start, day)

    # Merge today's (re)computed awards.
    for uid, v in awards_by_user.items():
        monthly_totals_map[uid] = (
            unpack_awards(monthly_totals_map.get(uid)) + unpack_awards(v)
        ).as_dict()

    players = sorted({(r.get('user_id') or '').strip() for r in records_primary if (r.get('user_id') or '').strip()})
    scores_text = format_summary(day, winners_by_game, awards_by_user, monthly_totals_map, best_display, players=players)

    summary_payload = {
        'day': day,
        'winners_by_game': winners_by_game,
        'awards_by_user': awards_by_user,
        'best_display': best_display,
        'recap_facts': recap_facts,
        'recap_text': recap_text,
        'scores_text': scores_text,
        'primary_puzzle_id_by_game': primary,
        'expected_players': expected,
        'complete_players': complete,
    }

    if force and store.day_already_posted(day):
        store.replace_day_summary(day, summary_payload)
    else:
        store.mark_day_posted(day, summary_payload)

    store.rebuild_totals_from_daily()

    if post:
        day_msg_ts = message_ts(client.chat_postMessage(channel=channel, text=scores_text))

        recap_ts = ''
        if POST_DAILY_RECAP and recap_text.strip():
            if DAILY_RECAP_IN_THREAD and day_msg_ts:
                recap_ts = message_ts(client.chat_postMessage(
                    channel=channel, text=recap_text, thread_ts=day_msg_ts,
                ))
            else:
                recap_ts = message_ts(client.chat_postMessage(channel=channel, text=recap_text))

        if day_msg_ts or recap_ts:
            store.update_day_summary(day, {
                'slack_day_results_ts': day_msg_ts,
                'slack_day_recap_ts': recap_ts,
            })

    return 'posted'


# ---------------------------------------------------------------------------
# Batch finalization
# ---------------------------------------------------------------------------
_finalize_lock = threading.Lock()


def finalize_due_days(channel: str, store: Any, client: Any, post: bool = True) -> None:
    """Finalize any unposted days that are ready (enough complete players) or closed."""
    if not _finalize_lock.acquire(blocking=False):
        logger.info("finalize_due_days: skipping, another finalization is already in progress")
        return
    try:
        _finalize_due_days_inner(channel, store, client, post=post)
    finally:
        _finalize_lock.release()


def _finalize_due_days_inner(channel: str, store: Any, client: Any, post: bool = True) -> None:
    now_utc = datetime.now(timezone.utc)
    posted_any_day = False
    while True:
        did_any = False
        for day in store.list_days_with_scores():
            if store.day_already_posted(day):
                continue
            try:
                status = finalize_day(day, channel, store, client, post=post, now_utc=now_utc)
                if status == 'posted':
                    did_any = True
                    posted_any_day = True
            except Exception as e:
                logger.exception(f"finalize_day({day}) failed: {e}")
        if not did_any:
            break

    # Month rollover. Gated on having actually finalized a day so the sheet
    # reads happen a few times a day rather than on every score message.
    # Finalizing the last day of a month is itself what finishes that month
    # (see monthly_summary.month_is_final), so on a normal month this fires
    # right after the final day's results post rather than waiting for the new
    # month. finalize_due_months is idempotent (MonthlyResults) and non-throwing.
    if posted_any_day:
        finalize_due_months(channel, store, client, post=post, now_utc=now_utc)
