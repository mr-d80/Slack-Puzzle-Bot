#!/usr/bin/env python3
"""monthly_summary.py

Month-end summaries: final standings, per-game champions, month records, and
AI commentary, plus a per-player breakdown for everyone who played.

A month is wrapped as soon as it is *final* — see ``month_is_final`` — which is
normally the moment its last day finalizes, not the moment the clock rolls over.

The month ledger lives in the ``MonthlyResults`` worksheet. A month is
finalized at most once; that row is what makes the rollover hook in
``finalization.py`` idempotent, since the hook re-runs on every score message.

Aggregation reads ``DailyResults.summary_json`` — the same source of truth as
Totals and the weekly summaries — so a month can be rebuilt at any time without
touching the raw Scores sheet.

Can also be run directly to inspect or backfill a month:

    python monthly_summary.py --month 2026-06            # dry run, prints
    python monthly_summary.py --month 2026-06 --post     # post to the channel
    python monthly_summary.py --post                     # most recent finished month
"""

from __future__ import annotations

import argparse
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from awards import (
    AwardTally, unpack_awards, uses_medal_scoring, medal_scoring_start, scoring_era, render_tally,
    compact_medals, format_points,
)
from config import (
    POST_MONTHLY_SUMMARY, MONTHLY_SUMMARY_IN_THREAD, POST_MONTHLY_PLAYER_BREAKDOWNS,
    MONTHLY_SUMMARY_MAX_AGE_DAYS, SCORE_DAY_TZ,
)
from day_utils import (
    month_key_for_day, month_bounds, prev_month_key, is_month_closed,
    _parse_day_key_loose, _parse_month_key,
)
from insights import build_monthly_recap_text, skunk_exclude_games, medal_scoring_facts
from scoring import compact_awards
from score_metrics import metric_sort_value, record_is_dnf, metric_unit
from slack_safe import message_ts


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------
def _month_label(month: str) -> str:
    """'2026-06' -> 'June 2026'."""
    return _parse_month_key(month).strftime("%B %Y")


def _winner_uids(outcome: Any) -> List[str]:
    """Pull the user ids out of a winners_by_game outcome."""
    if not isinstance(outcome, dict):
        return []
    uids: List[str] = []
    metric_type = _winner_metric_type(outcome)
    best_value = _safe_int(outcome.get("best_value"))
    for w in outcome.get("winners") or []:
        if isinstance(w, dict):
            record = dict(w)
            record.setdefault("metric_type", metric_type)
            if best_value is not None:
                record.setdefault("metric_value", best_value)
            if record_is_dnf(record):
                continue
            uid = str(w.get("user_id") or "").strip()
        else:
            if best_value is not None and record_is_dnf({
                "metric_type": metric_type, "metric_value": best_value,
            }):
                continue
            uid = str(w or "").strip()
        if uid:
            uids.append(uid)
    return uids


def _winner_metric_type(outcome: Any) -> str:
    if isinstance(outcome, dict):
        mt = str(outcome.get("metric_type") or "").strip()
        if mt:
            return mt
    for w in (outcome or {}).get("winners") or []:
        if isinstance(w, dict):
            mt = str(w.get("metric_type") or "").strip()
            if mt:
                return mt
    return "time"


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(str(x).strip())
    except Exception:
        return None


class _PlayerMonth:
    """Mutable per-player accumulator; converted to a plain dict for the facts."""

    def __init__(self) -> None:
        self.tally = AwardTally()
        self.days_played = 0
        self.skunks = 0
        self.best_day = ""
        self.best_day_tally = AwardTally()
        # Legacy months: outright wins and shared firsts per game.
        self.wins_by_game: Dict[str, int] = {}
        self.ties_by_game: Dict[str, int] = {}
        # Medal months: first-place finishes per game (a shared first counts for each).
        self.gold_by_game: Dict[str, int] = {}


def _accumulate(payloads: Dict[str, Dict[str, Any]]) -> Dict[str, _PlayerMonth]:
    """Fold a {day: summary_json} map into per-player month totals."""
    players: Dict[str, _PlayerMonth] = {}
    skunk_exclude = skunk_exclude_games()

    def get(uid: str) -> _PlayerMonth:
        return players.setdefault(uid, _PlayerMonth())

    for day in sorted(payloads):
        payload = payloads[day] or {}
        recap_facts = payload.get("recap_facts") or {}

        # Participation. recap_facts.players is the filtered participant list;
        # fall back to whoever appears in the award map for older ledger rows.
        day_players = recap_facts.get("players")
        if not isinstance(day_players, list) or not day_players:
            day_players = list((payload.get("awards_by_user") or {}).keys())
        for uid in day_players:
            uid = str(uid or "").strip()
            if uid:
                get(uid).days_played += 1

        # Awards, and best-single-day tracking. The best day is the one with the
        # highest tally (points for medal days, wins then ties for legacy days);
        # the earliest wins a tie.
        for uid, v in (payload.get("awards_by_user") or {}).items():
            uid = str(uid or "").strip()
            if not uid:
                continue
            day_tally = unpack_awards(v)
            p = get(uid)
            p.tally = p.tally + day_tally
            if day_tally.rank_key > p.best_day_tally.rank_key:
                p.best_day, p.best_day_tally = day, day_tally

        # Per-game breakdown.
        for game, outcome in (payload.get("winners_by_game") or {}).items():
            if not isinstance(outcome, dict):
                continue
            podium = outcome.get("podium")
            if isinstance(podium, list):
                # Medal day: count first places from the podium.
                valid_winners = set(_winner_uids(outcome))
                for entry in podium:
                    if isinstance(entry, dict) and entry.get("place") == 1:
                        for uid in entry.get("user_ids") or []:
                            uid = str(uid or "").strip()
                            if uid and uid in valid_winners:
                                g = get(uid).gold_by_game
                                g[game] = g.get(game, 0) + 1
                continue
            tied = str(outcome.get("result") or "") == "tie"
            for uid in _winner_uids(outcome):
                p = get(uid)
                if tied:
                    p.ties_by_game[game] = p.ties_by_game.get(game, 0) + 1
                else:
                    p.wins_by_game[game] = p.wins_by_game.get(game, 0) + 1

        # Skunk of the day -> skunk tally. Filtered here as well as at detection
        # time because days finalized before a game joined the exclude list keep
        # their original skunk in the ledger; without this the month would still
        # count them. The day's runner-up skunk isn't recoverable (only the top
        # one is stored), so an excluded day simply contributes nothing.
        skunk = recap_facts.get("skunk")
        if isinstance(skunk, dict) and str(skunk.get("game") or "") not in skunk_exclude:
            for uid in skunk.get("last_uids") or []:
                uid = str(uid or "").strip()
                if uid:
                    get(uid).skunks += 1

    return players


def _rank_standings(players: Dict[str, _PlayerMonth]) -> List[str]:
    """User ids best first (stable + deterministic).

    Medal months rank by points, then count-back on gold, silver, bronze. Legacy
    months rank by wins then ties. Either way the user id is the last tiebreak.
    """
    return sorted(players, key=lambda uid: players[uid].tally.sort_key(uid))


def _collect_records(payloads: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Best single result per game for the month.

    The metric's scoring direction chooses the month record: lowest time/guesses
    or highest points. Ties on value keep the earliest day.
    """
    records: Dict[str, Dict[str, Any]] = {}

    for day in sorted(payloads):
        payload = payloads[day] or {}
        best_display = payload.get("best_display") or {}
        for game, outcome in (payload.get("winners_by_game") or {}).items():
            if not isinstance(outcome, dict):
                continue
            val = _safe_int(outcome.get("best_value"))
            if val is None:
                continue
            user_ids = _winner_uids(outcome)
            if not user_ids:
                continue
            metric_type = _winner_metric_type(outcome)
            current = records.get(game)
            if current is not None and metric_sort_value(val, metric_type) >= metric_sort_value(
                int(current["value"]), str(current.get("metric_type") or "time")
            ):
                continue
            display = str(best_display.get(game) or val)
            if metric_type == "points" and not any(
                token in display.lower() for token in ("pt", "point")
            ):
                display = f"{display} {metric_unit(metric_type)}"
            records[game] = {
                "game": game,
                "value": val,
                "display": display,
                "day": day,
                "user_ids": user_ids,
                "metric_type": metric_type,
            }

    return records


def _extremes(payloads: Dict[str, Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Tightest race and biggest blowout across the whole month."""
    tightest: Optional[Dict[str, Any]] = None
    blowout: Optional[Dict[str, Any]] = None

    for day in sorted(payloads):
        recap_facts = (payloads[day] or {}).get("recap_facts") or {}

        t = recap_facts.get("tightest_race")
        if (isinstance(t, dict) and t.get("margin") is not None
                and str(t.get("metric_type") or "time").strip().lower() == "time"):
            if tightest is None or int(t["margin"]) < int(tightest["margin"]):
                tightest = dict(t, day=day)

        b = recap_facts.get("blowout")
        if (isinstance(b, dict) and b.get("margin") is not None
                and str(b.get("metric_type") or "time").strip().lower() == "time"):
            if blowout is None or int(b["margin"]) > int(blowout["margin"]):
                blowout = dict(b, day=day)

    return tightest, blowout


def _top_by(counts: Dict[str, int]) -> Tuple[List[str], int]:
    """All keys sharing the maximum count, plus that count. ([], 0) if empty."""
    if not counts:
        return [], 0
    best = max(counts.values())
    if best <= 0:
        return [], 0
    return sorted(k for k, v in counts.items() if v == best), best


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
def build_monthly_facts(
    month: str,
    payloads: Dict[str, Dict[str, Any]],
    prev_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the deterministic fact bundle for a month.

    *payloads* maps day key -> DailyResults.summary_json for the month;
    *prev_payloads* does the same for the previous month and drives the
    month-over-month deltas. Both are already filtered to their own month.
    """
    start_day, end_day = month_bounds(month)

    # A month is scored one way throughout: the cut-over falls on a month
    # boundary, because standings reset monthly. Read the era off the days that
    # were actually posted; the month's first day is only the answer for a month
    # with nothing posted yet.
    era = scoring_era(payloads) if payloads else ("medals" if uses_medal_scoring(start_day) else "legacy")
    medals = era != "legacy"
    if era == "mixed":
        logger.warning(
            "%s mixes trophy-scored and medal-scored days (MEDAL_SCORING_START=%s is inside the month); "
            "the standings assume the cut-over is on a month boundary and will under-count the trophy days.",
            month, medal_scoring_start(),
        )
    if prev_payloads and scoring_era(prev_payloads) != era:
        # The cut-over month. Last month's wins and this month's points are not
        # the same quantity, so a month-over-month delta would compare unlike
        # things; drop the comparison rather than report a meaningless mover.
        prev_payloads = None

    players = _accumulate(payloads)
    prev_players = _accumulate(prev_payloads or {})

    def prev_tally(uid: str) -> AwardTally:
        return prev_players[uid].tally if uid in prev_players else AwardTally()

    ranked = _rank_standings(players)
    days_counted = len(payloads)

    standings: List[Dict[str, Any]] = []
    place = 0
    prev_key: Optional[Tuple[int, ...]] = None
    for i, uid in enumerate(ranked):
        p = players[uid]
        t = p.tally
        key = t.rank_key
        # Equal tallies share a place; the next distinct one skips ahead.
        place = place if key == prev_key else i + 1
        prev_key = key

        pt = prev_tally(uid)
        if medals:
            top_game, top_game_wins = _top_by(p.gold_by_game)
            standings.append({
                "user_id": uid,
                "place": place,
                "points": t.points,
                "gold": t.gold,
                "silver": t.silver,
                "bronze": t.bronze,
                "days_played": p.days_played,
                "days_counted": days_counted,
                "skunks": p.skunks,
                "best_day": p.best_day,
                "best_day_points": p.best_day_tally.points,
                "gold_by_game": dict(sorted(p.gold_by_game.items())),
                "top_game": top_game[0] if top_game else "",
                "top_game_wins": top_game_wins,
                "prev_points": pt.points,
                "delta_points": t.points - pt.points,
            })
            continue

        top_game, top_game_wins = _top_by(p.wins_by_game)
        standings.append({
            "user_id": uid,
            "place": place,
            "wins": t.wins,
            "ties": t.ties,
            "days_played": p.days_played,
            "days_counted": days_counted,
            "skunks": p.skunks,
            "best_day": p.best_day,
            "best_day_wins": p.best_day_tally.wins,
            "best_day_ties": p.best_day_tally.ties,
            "wins_by_game": dict(sorted(p.wins_by_game.items())),
            "ties_by_game": dict(sorted(p.ties_by_game.items())),
            "top_game": top_game[0] if top_game else "",
            "top_game_wins": top_game_wins,
            "prev_wins": pt.wins,
            "delta_wins": t.wins - pt.wins,
        })

    champion: Optional[Dict[str, Any]] = None
    runner_up: Optional[Dict[str, Any]] = None
    if standings:
        top = [s for s in standings if s["place"] == 1]
        chasers = [s for s in standings if s["place"] != 1]
        if medals:
            # Margin is in points. A champion level on points but ahead on the
            # medal count-back has a margin of 0, which the wrap reports as
            # "level on points".
            champion = {
                "user_ids": [s["user_id"] for s in top],
                "points": top[0]["points"],
                "gold": top[0]["gold"],
                "silver": top[0]["silver"],
                "bronze": top[0]["bronze"],
                "shared": len(top) > 1,
                "margin": top[0]["points"] - chasers[0]["points"] if chasers else 0,
            }
        else:
            champion = {
                "user_ids": [s["user_id"] for s in top],
                "wins": top[0]["wins"],
                "ties": top[0]["ties"],
                "shared": len(top) > 1,
                "margin": top[0]["wins"] - chasers[0]["wins"] if chasers else 0,
            }
        if chasers:
            second_place = min(s["place"] for s in chasers)
            second = [s for s in chasers if s["place"] == second_place]
            if medals:
                runner_up = {
                    "user_ids": [s["user_id"] for s in second],
                    "points": second[0]["points"],
                    "gold": second[0]["gold"],
                    "silver": second[0]["silver"],
                    "bronze": second[0]["bronze"],
                }
            else:
                runner_up = {
                    "user_ids": [s["user_id"] for s in second],
                    "wins": second[0]["wins"],
                    "ties": second[0]["ties"],
                }

    # Per-game champions: most first places in that game over the month. Legacy
    # months count outright wins only (a shared first was a necktie); medal
    # months count every first place, since a shared first is a gold for each.
    wins_by_game_by_user: Dict[str, Dict[str, int]] = {}
    for uid, p in players.items():
        for game, n in (p.gold_by_game if medals else p.wins_by_game).items():
            wins_by_game_by_user.setdefault(game, {})[uid] = n

    game_champions: Dict[str, Dict[str, Any]] = {}
    for game, counts in sorted(wins_by_game_by_user.items()):
        uids, n = _top_by(counts)
        if uids:
            game_champions[game] = (
                {"game": game, "user_ids": uids, "gold": n} if medals
                else {"game": game, "user_ids": uids, "wins": n}
            )

    biggest_mover: Optional[Dict[str, Any]] = None
    if medals:
        movers = {uid: players[uid].tally.points - prev_tally(uid).points for uid in players}
        mover_uids, mover_delta = _top_by(movers)
        if mover_uids and mover_delta > 0 and prev_payloads:
            biggest_mover = {
                "user_ids": mover_uids,
                "delta_points": mover_delta,
                "points": players[mover_uids[0]].tally.points,
                "prev_points": prev_tally(mover_uids[0]).points,
            }
    else:
        movers = {uid: players[uid].tally.wins - prev_tally(uid).wins for uid in players}
        mover_uids, mover_delta = _top_by(movers)
        if mover_uids and mover_delta > 0 and prev_payloads:
            biggest_mover = {
                "user_ids": mover_uids,
                "delta_wins": mover_delta,
                "wins": players[mover_uids[0]].tally.wins,
                "prev_wins": prev_tally(mover_uids[0]).wins,
            }

    skunk_uids, skunk_n = _top_by({uid: p.skunks for uid, p in players.items()})
    skunk_king = {"user_ids": skunk_uids, "count": skunk_n} if skunk_uids else None

    # Best single day: most points (medal months) or most wins (legacy months).
    best_day_uids, best_day_n = _top_by({
        uid: (p.best_day_tally.points if medals else p.best_day_tally.wins)
        for uid, p in players.items()
    })
    best_day: Optional[Dict[str, Any]] = None
    if best_day_uids and best_day_n > 0:
        best_day = {
            "user_ids": best_day_uids,
            ("points" if medals else "wins"): best_day_n,
            "day": players[best_day_uids[0]].best_day,
        }

    tightest, blowout = _extremes(payloads)

    facts = {
        "month": month,
        "month_label": _month_label(month),
        "start_day": start_day,
        "end_day": end_day,
        "prev_month": prev_month_key(month) if prev_payloads else None,
        "prev_month_label": _month_label(prev_month_key(month)) if prev_payloads else None,
        "days_counted": days_counted,
        "players": sorted(players),
        "standings": standings,
        "champion": champion,
        "runner_up": runner_up,
        "game_champions": game_champions,
        "records": _collect_records(payloads),
        "biggest_mover": biggest_mover,
        "skunk_king": skunk_king,
        "best_day": best_day,
        "perfect_attendance": sorted(
            uid for uid, p in players.items() if days_counted and p.days_played == days_counted
        ),
        "tightest_race": tightest,
        "blowout": blowout,
    }
    if medals:
        # Absent on legacy months, so a bundle without the key reads as legacy.
        facts.update(medal_scoring_facts())
    return facts


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _tags(uids: List[str]) -> str:
    return ", ".join(f"<@{u}>" for u in uids)


def _is_medal_facts(facts: Dict[str, Any]) -> bool:
    """True for a fact bundle built from medal-era days (legacy bundles lack the key)."""
    return facts.get("scoring") == "medals"


def render_monthly_standings_text(facts: Dict[str, Any]) -> str:
    """The parent channel post: standings, game champions, records, participation."""
    label = facts.get("month_label") or facts.get("month") or ""
    days_counted = int(facts.get("days_counted") or 0)
    standings = facts.get("standings") or []

    lines: List[str] = []
    lines.append(f"*Monthly results for {label}*")
    lines.append(f"_{facts.get('start_day')} to {facts.get('end_day')}, {days_counted} scored days_")

    medals = _is_medal_facts(facts)

    lines.append("")
    lines.append("*Final standings*")
    for s in standings:
        if medals:
            earned = render_tally(unpack_awards(s), empty_era="medals", show_zeros=True)
        else:
            earned = compact_awards(s['wins'], s['ties'], show_zeros=True)
        lines.append(f"{s['place']}. <@{s['user_id']}> {earned}")

    game_champions = facts.get("game_champions") or {}
    if game_champions:
        lines.append("")
        lines.append("*Game champions*")
        for game in sorted(game_champions):
            gc = game_champions[game]
            if medals:
                earned = compact_medals(gc["gold"], 0, 0)
            else:
                earned = compact_awards(gc["wins"], 0)
            lines.append(f"- {game}: {_tags(gc['user_ids'])} ({earned})")

    records = facts.get("records") or {}
    if records:
        lines.append("")
        lines.append("*Month records*")
        for game in sorted(records):
            rec = records[game]
            holders = _tags(rec.get("user_ids") or [])
            if holders:
                lines.append(f"- {game}: {rec['display']} by {holders} ({rec['day']})")

    if days_counted:
        lines.append("")
        lines.append("*Participation*")
        for s in sorted(standings, key=lambda x: (-x["days_played"], x["user_id"])):
            lines.append(f"- <@{s['user_id']}> {s['days_played']}/{days_counted} days")

    return "\n".join(lines)


def render_player_month_text(entry: Dict[str, Any], facts: Dict[str, Any]) -> str:
    """One player's month breakdown, posted in the summary thread."""
    uid = entry["user_id"]
    label = facts.get("month_label") or facts.get("month") or ""
    total_players = len(facts.get("standings") or [])
    days_counted = int(facts.get("days_counted") or 0)

    medals = _is_medal_facts(facts)

    lines: List[str] = []
    lines.append(f"*{label} for <@{uid}>*")
    if medals:
        finish = render_tally(unpack_awards(entry), empty_era="medals")
        lines.append(f"- Finish: {_ordinal(entry['place'])} of {total_players}, {finish}")
    else:
        lines.append(
            f"- Finish: {_ordinal(entry['place'])} of {total_players} "
            f"({compact_awards(entry['wins'], entry['ties'])})"
        )

    if facts.get("prev_month"):
        prev_label = facts.get("prev_month_label") or _month_label(facts["prev_month"])
        if medals:
            delta = int(entry.get("delta_points") or 0)
            lines.append(
                f"- vs {prev_label}: {delta:+d} pts (was {entry.get('prev_points', 0)})"
            )
        else:
            delta = int(entry.get("delta_wins") or 0)
            lines.append(
                f"- vs {prev_label}: {delta:+d} trophies (was {entry.get('prev_wins', 0)})"
            )

    if days_counted:
        lines.append(f"- Participation: {entry['days_played']}/{days_counted} days")

    if entry.get("top_game"):
        earned = (
            compact_medals(entry["top_game_wins"], 0, 0) if medals
            else compact_awards(entry["top_game_wins"], 0)
        )
        lines.append(f"- Best game: {entry['top_game']} ({earned})")

    if entry.get("best_day"):
        if medals:
            earned = format_points(entry.get("best_day_points") or 0)
        else:
            earned = compact_awards(entry['best_day_wins'], entry['best_day_ties'])
        lines.append(f"- Best day: {entry['best_day']} ({earned})")

    skunks = int(entry.get("skunks") or 0)
    if skunks:
        lines.append(f"- Skunked: {skunks}x")

    return "\n".join(lines)


def _ordinal(n: int) -> str:
    n = int(n or 0)
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# ---------------------------------------------------------------------------
# Month selection
# ---------------------------------------------------------------------------
def _scan_daily_ledger(store: Any) -> Tuple[List[str], Set[str]]:
    """One pass over DailyResults -> (months with posted days, posted day keys)."""
    rows = store.daily.get_all_values()
    if len(rows) <= 1:
        return [], set()
    header = rows[0]
    if "day" not in header or "summary_json" not in header:
        return [], set()
    day_i = header.index("day")
    sum_i = header.index("summary_json")

    months: Set[str] = set()
    days: Set[str] = set()
    for r in rows[1:]:
        if len(r) <= max(day_i, sum_i):
            continue
        d = _parse_day_key_loose((r[day_i] or "").strip())
        if not d or not (r[sum_i] or "").strip():
            continue
        key = d.isoformat()
        days.add(key)
        months.add(month_key_for_day(key))
    return sorted(months), days


def months_with_posted_days(store: Any) -> List[str]:
    """Month keys that have at least one posted day in DailyResults."""
    return _scan_daily_ledger(store)[0]


def month_is_final(store: Any, month: str, now_utc: Optional[datetime] = None) -> bool:
    """True once nothing further can land in *month*.

    Two ways that happens, and the fast one matters:

    1. The month's last score day is already finalized. A posted day never
       re-finalizes, so once the 30th is in the books June is settled even
       though the clock still says June. finalize_day posts a day as soon as
       every expected player has completed, so on a normal day this fires
       hours before the month boundary.
    2. The month has closed on the wall clock. The backstop for a final day
       that nobody played (it never finalizes, so case 1 can't fire) or that
       finalized while the bot was down.

    Case 1 is what keeps a quiet first week of the new month from delaying the
    wrap: waiting on case 1 alone would strand a month whose last day had no
    scores, and waiting on case 2 alone means waiting for the first message of
    the new month to trigger a finalization pass.
    """
    if store.day_already_posted(month_bounds(month)[1]):
        return True
    return is_month_closed(month, now_utc)


def month_age_days(month: str, now_utc: Optional[datetime] = None) -> int:
    """Score days elapsed since *month* ended. Negative while it is still running."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    end = date.fromisoformat(month_bounds(month)[1])
    return (now_utc.astimezone(SCORE_DAY_TZ).date() - end).days


def due_months(store: Any, now_utc: Optional[datetime] = None) -> List[str]:
    """Months that should be auto-posted: final, unposted, and recent enough.

    The age limit exists because MonthlyResults starts empty. Without it the
    first rollover after deploying would see every month already in
    DailyResults as unposted and post the entire back catalogue in one burst.
    Months past the limit are left alone rather than silently claimed, so
    ``monthly_summary.py --month`` can still post them deliberately.
    """
    posted_months = set(store.list_posted_months())
    months, posted_days = _scan_daily_ledger(store)

    def is_final(m: str) -> bool:
        # Same rule as month_is_final, but resolved against the day keys we
        # just scanned so a catch-up run doesn't re-read the ledger per month.
        return month_bounds(m)[1] in posted_days or is_month_closed(m, now_utc)

    def is_recent(m: str) -> bool:
        if MONTHLY_SUMMARY_MAX_AGE_DAYS <= 0:
            return True
        return month_age_days(m, now_utc) <= MONTHLY_SUMMARY_MAX_AGE_DAYS

    due = []
    for m in months:
        if m in posted_months or not is_final(m):
            continue
        if not is_recent(m):
            logger.info(
                "monthly summary: skipping %s, it ended %d days ago "
                "(MONTHLY_SUMMARY_MAX_AGE_DAYS=%d); post it with "
                "`python monthly_summary.py --month %s --post`",
                m, month_age_days(m, now_utc), MONTHLY_SUMMARY_MAX_AGE_DAYS, m,
            )
            continue
        due.append(m)
    return due


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------
def load_month_facts(store: Any, month: str, with_previous: bool = True) -> Dict[str, Any]:
    """Read the ledger once and build the fact bundle for *month*."""
    start_day, end_day = month_bounds(month)
    previous = prev_month_key(month)
    prev_start, _prev_end = month_bounds(previous)

    read_from = prev_start if with_previous else start_day
    all_payloads = store.load_daily_payloads_in_range(read_from, end_day)

    payloads = {d: p for d, p in all_payloads.items() if month_key_for_day(d) == month}
    prev_payloads = (
        {d: p for d, p in all_payloads.items() if month_key_for_day(d) == previous}
        if with_previous else {}
    )

    return build_monthly_facts(month, payloads, prev_payloads or None)


def finalize_month(
    month: str,
    channel: str,
    store: Any,
    client: Any,
    post: bool = True,
    force: bool = False,
    now_utc: Optional[datetime] = None,
) -> str:
    """Finalize one month. Returns a status string.

    Statuses: ``already_posted``, ``not_final``, ``no_days``, ``posted``.

    The MonthlyResults row is claimed *before* anything is sent to Slack, so a
    failure mid-post can't cause a duplicate summary on the next score message.
    """
    already = store.month_already_posted(month)
    if already and not force:
        return "already_posted"

    if (not force) and (not month_is_final(store, month, now_utc)):
        return "not_final"

    facts = load_month_facts(store, month)
    if not facts.get("days_counted"):
        return "no_days"

    standings_text = render_monthly_standings_text(facts)
    recap_text = build_monthly_recap_text(facts)

    summary_payload = {
        "month": month,
        "facts": facts,
        "standings_text": standings_text,
        "recap_text": recap_text,
    }

    if already:
        store.replace_month_summary(month, summary_payload)
    elif not store.mark_month_posted(month, summary_payload):
        # Another worker claimed this month between our check and our write.
        return "already_posted"

    if post:
        _post_month(month, channel, store, client, facts, standings_text, recap_text)

    return "posted"


def _post_month(
    month: str,
    channel: str,
    store: Any,
    client: Any,
    facts: Dict[str, Any],
    standings_text: str,
    recap_text: str,
) -> None:
    """Post standings, then commentary and per-player breakdowns in its thread."""
    parent_ts = message_ts(client.chat_postMessage(channel=channel, text=standings_text))

    def reply(text: str) -> str:
        kwargs: Dict[str, Any] = {"channel": channel, "text": text}
        if MONTHLY_SUMMARY_IN_THREAD and parent_ts:
            kwargs["thread_ts"] = parent_ts
        return message_ts(client.chat_postMessage(**kwargs))

    recap_ts = reply(recap_text) if recap_text.strip() else ""

    if POST_MONTHLY_PLAYER_BREAKDOWNS:
        for entry in facts.get("standings") or []:
            try:
                reply(render_player_month_text(entry, facts))
            except Exception:
                logger.exception(
                    "monthly breakdown post failed for %s (%s)", entry.get("user_id"), month
                )

    if parent_ts or recap_ts:
        store.update_month_summary(month, {
            "slack_month_results_ts": parent_ts,
            "slack_month_recap_ts": recap_ts,
        })


_finalize_months_lock = threading.Lock()


def finalize_due_months(
    channel: str,
    store: Any,
    client: Any,
    post: bool = True,
    now_utc: Optional[datetime] = None,
) -> List[str]:
    """Finalize every closed month that has scored days and no ledger row yet.

    Called from the daily finalization path, so it runs on ordinary score
    traffic and must stay cheap and non-throwing: any month that fails is
    logged and left unposted for the next attempt. Returns the months posted.
    """
    if not POST_MONTHLY_SUMMARY:
        return []

    if not _finalize_months_lock.acquire(blocking=False):
        logger.info("finalize_due_months: skipping, another monthly finalization is in progress")
        return []

    posted: List[str] = []
    try:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        for month in due_months(store, now_utc):
            try:
                if finalize_month(month, channel, store, client, post=post, now_utc=now_utc) == "posted":
                    posted.append(month)
            except Exception:
                logger.exception("finalize_month(%s) failed", month)
    except Exception:
        logger.exception("finalize_due_months failed")
    finally:
        _finalize_months_lock.release()

    return posted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_bot_module():
    """Load score-bot.py from the same folder as this script."""
    import importlib.util
    import sys

    base = Path(__file__).resolve().parent
    for name in ("score-bot.py", "score_bot.py"):
        p = base / name
        if not p.exists():
            continue
        spec = importlib.util.spec_from_file_location("score_bot_loaded", str(p))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            # Register before exec so game_registry's singleton is shared
            # (same reason reconcile_day.py does this).
            sys.modules["score_bot_loaded"] = mod
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            return mod

    raise FileNotFoundError("Could not find score-bot.py next to monthly_summary.py")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate or post a monthly summary.")
    ap.add_argument("--month", default="", help="Month key YYYY-MM (default: most recent closed month with scores)")
    ap.add_argument("--channel", default="", help="Channel ID (defaults to SCORE_CHANNEL_ID)")
    ap.add_argument("--post", action="store_true", help="Post to Slack (default is a dry run)")
    ap.add_argument("--force", action="store_true", help="Re-finalize a month that was already posted")
    ap.add_argument("--no-ai", action="store_true", help="Disable the AI rewrite for this run")
    args = ap.parse_args()

    if args.no_ai:
        import os

        os.environ["AI_REWRITE_ENABLED"] = "0"

    bot = _load_bot_module()
    store = bot.store

    month = args.month.strip()
    if not month:
        candidates = [m for m in months_with_posted_days(store) if month_is_final(store, m)]
        if not candidates:
            raise SystemExit("No finished month with posted days found in DailyResults")
        month = candidates[-1]

    if not args.post:
        facts = load_month_facts(store, month)
        if not facts.get("days_counted"):
            raise SystemExit(f"No posted days found for {month}")
        print(render_monthly_standings_text(facts))
        print()
        print(build_monthly_recap_text(facts))
        for entry in facts.get("standings") or []:
            print()
            print(render_player_month_text(entry, facts))
        return

    channel = (args.channel or getattr(bot, "SCORE_CHANNEL_ID", "") or "").strip()
    if not channel:
        raise SystemExit("No channel provided. Pass --channel or set SCORE_CHANNEL_ID in env.")

    status = finalize_month(
        month, channel, store, bot.client, post=True, force=args.force,
    )
    print(f"{month}: {status}")


if __name__ == "__main__":
    main()
