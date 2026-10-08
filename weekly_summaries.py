#!/usr/bin/env python3
"""weekly_summaries.py

Generate per-player weekly trend summaries from the DailyResults ledger.

Default behavior is dry-run (prints to stdout). Use --post or --dm to deliver
messages through Slack.

Notes:
- Uses DailyResults.summary_json as the source of truth (same as Totals).
- Optional LLM rewriting is controlled by AI_REWRITE_ENABLED + OPENAI_API_KEY
  (see insights.py). Deterministic stats are always preserved.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from slack_sdk.errors import SlackApiError

from awards import AwardTally, format_points, render_tally, scoring_era, unpack_awards
from insights import medal_scoring_facts, rewrite_text
from slack_safe import dm_channel_id, message_ts


def _load_bot_module():
    """Load score-bot.py from the same folder as this script."""
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
                spec.loader.exec_module(mod)  # type: ignore[attr-defined]
                return mod

    raise FileNotFoundError("Could not find score-bot.py next to weekly_summaries.py")


def _slack_call_with_retry(fn, **kwargs):
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


def _list_posted_days(store) -> List[str]:
    rows = store.daily.get_all_values()
    if len(rows) <= 1:
        return []
    header = rows[0]
    if "day" not in header or "summary_json" not in header:
        return []
    day_i = header.index("day")
    sum_i = header.index("summary_json")

    days = []
    for r in rows[1:]:
        if len(r) <= max(day_i, sum_i):
            continue
        d = (r[day_i] or "").strip()
        s = (r[sum_i] or "").strip()
        if d and s:
            days.append(d)
    return sorted(set(days))


def _load_day_payloads(store, days: List[str]) -> Dict[str, Dict[str, Any]]:
    """Map day -> parsed summary_json payload."""
    out: Dict[str, Dict[str, Any]] = {}
    rows = store.daily.get_all_values()
    if len(rows) <= 1:
        return out
    header = rows[0]
    if "day" not in header or "summary_json" not in header:
        return out
    day_i = header.index("day")
    sum_i = header.index("summary_json")
    wanted = set(days)

    for r in rows[1:]:
        if len(r) <= max(day_i, sum_i):
            continue
        d = (r[day_i] or "").strip()
        if d not in wanted:
            continue
        s = (r[sum_i] or "").strip()
        if not s:
            continue
        try:
            payload = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out[d] = payload
    return out


@dataclass
class UserWeek:
    tally: AwardTally = field(default_factory=AwardTally)
    days_played: int = 0
    best_day: str = ""
    best_day_tally: AwardTally = field(default_factory=AwardTally)
    # Podium finishes per game: first places for legacy days, any medal for medal days.
    awards_by_game: Dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.awards_by_game is None:
            self.awards_by_game = {}


def _compute_period_stats(store, days: List[str], payloads: Dict[str, Dict[str, Any]]) -> Dict[str, UserWeek]:
    stats: Dict[str, UserWeek] = {}

    for day in days:
        p = payloads.get(day) or {}
        awards = p.get("awards_by_user") or {}
        winners_by_game = p.get("winners_by_game") or {}
        recap_facts = p.get("recap_facts") or {}
        players = recap_facts.get("players") or []

        # Fallback for older ledger rows that don't have recap_facts.
        if not players:
            try:
                recs = store.load_scores_for_day(day)
                players = sorted({str(r.get("user_id") or "").strip() for r in recs if str(r.get("user_id") or "").strip()})
            except Exception:
                players = []

        # participation
        if isinstance(players, list):
            for uid in players:
                uid = str(uid or "").strip()
                if not uid:
                    continue
                stats.setdefault(uid, UserWeek()).days_played += 1

        # awards
        if isinstance(awards, dict):
            for uid, v in awards.items():
                uid = str(uid or "").strip()
                if not uid:
                    continue
                day_tally = unpack_awards(v)
                u = stats.setdefault(uid, UserWeek())
                u.tally = u.tally + day_tally

                # best day tracking (the best tally; the earliest day wins a tie)
                if day_tally.rank_key > u.best_day_tally.rank_key:
                    u.best_day = day
                    u.best_day_tally = day_tally

        # per-game award counts (winner list is already filtered to primary puzzles)
        if isinstance(winners_by_game, dict):
            for game, outcome in winners_by_game.items():
                if not isinstance(outcome, dict):
                    continue
                podium = outcome.get("podium")
                if isinstance(podium, list):
                    # Medal day: every medalist counts, not just first place.
                    for entry in podium:
                        if not isinstance(entry, dict):
                            continue
                        for uid in entry.get("user_ids") or []:
                            uid = str(uid or "").strip()
                            if uid:
                                u = stats.setdefault(uid, UserWeek())
                                u.awards_by_game[game] = u.awards_by_game.get(game, 0) + 1
                    continue
                winners = outcome.get("winners") or []
                if not isinstance(winners, list):
                    continue
                for w in winners:
                    if not isinstance(w, dict):
                        continue
                    uid = str(w.get("user_id") or "").strip()
                    if not uid:
                        continue
                    u = stats.setdefault(uid, UserWeek())
                    u.awards_by_game[game] = u.awards_by_game.get(game, 0) + 1

    return stats


def _top_game(awards_by_game: Dict[str, int]) -> Tuple[str, int]:
    if not awards_by_game:
        return "", 0
    game, n = max(awards_by_game.items(), key=lambda kv: (kv[1], kv[0]))
    return str(game), int(n)


def _render_user_message(
    uid: str,
    period_days: List[str],
    cur: UserWeek,
    prev: Optional[UserWeek],
    prev_days: Optional[List[str]] = None,
) -> str:
    start = period_days[0]
    end = period_days[-1]
    n_days = len(period_days)

    cur_era = scoring_era(period_days)
    prev_era = scoring_era(prev_days) if prev_days else None
    prev_tally = prev.tally if prev else AwardTally()

    # A change since the previous period only means something when both periods
    # were scored the same way. Across the cut-over (or in a period that
    # straddles it) trophies and points are different currencies, so no delta.
    comparable = prev_era is None or prev_era == cur_era
    legacy_period = cur_era == "legacy" and comparable
    medal_period = cur_era == "medals" and comparable
    dw = cur.tally.wins - prev_tally.wins
    dt = cur.tally.ties - prev_tally.ties
    dp = cur.tally.points - prev_tally.points
    top_game, top_n = _top_game(cur.awards_by_game)

    if legacy_period:
        delta_text = f" (Δ {dw:+d} trophies, {dt:+d} ties)"
    elif medal_period:
        delta_text = f" (Δ {dp:+d} pts)"
    else:
        delta_text = ""

    lines: List[str] = []
    lines.append(f"*Weekly trend for <@{uid}>* ({start} to {end})")
    lines.append(f"- Awards: {render_tally(cur.tally, empty_era='medals' if cur_era == 'medals' else 'legacy')}{delta_text}")
    lines.append(f"- Participation: {cur.days_played}/{n_days} days")
    if top_game:
        lines.append(f"- Best game: {top_game} ({top_n} podiums)")
    if cur.best_day:
        # One day is scored one way, so its tally is trophies or points, never both.
        best_day = format_points(cur.best_day_tally.points) if cur.best_day_tally.has_medals else render_tally(cur.best_day_tally)
        lines.append(f"- Best day: {cur.best_day} ({best_day})")

    fallback = "\n".join(lines)
    facts: Dict[str, Any] = {
        "user_id": uid,
        "period_start": start,
        "period_end": end,
        "days_in_period": n_days,
    }
    if cur_era == "legacy":
        facts.update({
            "wins": cur.tally.wins,
            "ties": cur.tally.ties,
            "delta_wins": dw,
            "delta_ties": dt,
            "days_played": cur.days_played,
            "top_game": top_game,
            "top_game_podiums": top_n,
            "best_day": cur.best_day,
            "best_day_wins": cur.best_day_tally.wins,
            "best_day_ties": cur.best_day_tally.ties,
        })
    else:
        facts.update({
            "points": cur.tally.points,
            "gold": cur.tally.gold,
            "silver": cur.tally.silver,
            "bronze": cur.tally.bronze,
            "days_played": cur.days_played,
            "top_game": top_game,
            "top_game_podiums": top_n,
            "best_day": cur.best_day,
            "best_day_points": cur.best_day_tally.points,
        })
        if cur.tally.has_legacy:
            # A period that straddles the cut-over also carries trophies.
            facts.update({"wins": cur.tally.wins, "ties": cur.tally.ties})
        if medal_period:
            facts["delta_points"] = dp
        facts.update(medal_scoring_facts())
    return rewrite_text("weekly trend", facts, fallback)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Number of most-recent posted days to summarize")
    ap.add_argument("--channel", default="", help="Channel ID (defaults to SCORE_CHANNEL_ID env var)")
    ap.add_argument("--post", action="store_true", help="Post to channel (threaded under a single parent message)")
    ap.add_argument("--dm", action="store_true", help="DM each user their own summary")
    ap.add_argument("--no-ai", action="store_true", help="Disable AI rewrite for this run")
    args = ap.parse_args()

    if args.no_ai:
        import os

        os.environ["AI_REWRITE_ENABLED"] = "0"

    bot = _load_bot_module()
    store = bot.store
    client = bot.client

    all_days = _list_posted_days(store)
    if not all_days:
        raise SystemExit("No posted days found in DailyResults.summary_json")

    days = sorted(all_days)[-max(1, args.days):]
    prev_days = sorted(all_days)[: max(0, len(all_days) - len(days))][-len(days):]

    payloads = _load_day_payloads(store, days + prev_days)
    cur_stats = _compute_period_stats(store, days, payloads)
    prev_stats = _compute_period_stats(store, prev_days, payloads) if prev_days else {}

    uids = sorted(set(cur_stats.keys()) | set(prev_stats.keys()))
    if not uids:
        raise SystemExit("No users found in the selected period")

    # Build messages
    msgs: Dict[str, str] = {}
    for uid in uids:
        cur = cur_stats.get(uid) or UserWeek()
        prev = prev_stats.get(uid)
        msgs[uid] = _render_user_message(uid, days, cur, prev, prev_days)

    if not args.post and not args.dm:
        for uid in uids:
            print("\n" + "=" * 60)
            print(msgs[uid])
        return

    channel = (args.channel or getattr(bot, "SCORE_CHANNEL_ID", "") or "").strip()
    if args.post and not channel:
        raise SystemExit("No channel provided. Pass --channel or set SCORE_CHANNEL_ID in env.")

    # Channel post: one parent message, then thread replies.
    parent_ts = ""
    if args.post:
        title = f"*Weekly summaries* ({days[0]} to {days[-1]})"
        parent_ts = message_ts(
            _slack_call_with_retry(client.chat_postMessage, channel=channel, text=title)
        )

    for uid in uids:
        text = msgs[uid]

        if args.post:
            kwargs = {"channel": channel, "text": text}
            if parent_ts:
                kwargs["thread_ts"] = parent_ts
            _slack_call_with_retry(client.chat_postMessage, **kwargs)

        if args.dm:
            dm_chan = dm_channel_id(
                _slack_call_with_retry(client.conversations_open, users=uid)
            )
            if dm_chan:
                _slack_call_with_retry(client.chat_postMessage, channel=dm_chan, text=text)


if __name__ == "__main__":
    main()
