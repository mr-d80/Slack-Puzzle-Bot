"""day_utils.py

Day-key arithmetic, puzzle-id filtering, and expected-player logic.

All date boundaries use SCORE_DAY_TZ (default America/Vancouver) so that a "day"
corresponds to the scoring window, not the local wall clock.
"""

import re
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List, Optional, Set, Tuple

from config import SCORE_DAY_TZ, DAY_FMT, PLAYERS_EXPECTED
from game_registry import game_registry
from parser import (
    ParsedScore, normalize_game, canonical_user_id,
    parse_pinpoint_fail, make_pinpoint_fail_score,
)


# ---------------------------------------------------------------------------
# Day-key helpers
# ---------------------------------------------------------------------------
def day_key_from_ts(ts: str) -> str:
    """Bucket scores by day using SCORE_DAY_TZ (default: America/Vancouver)."""
    sec = float(ts)
    dt_local = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(SCORE_DAY_TZ)
    return dt_local.strftime(DAY_FMT)


def day_end_utc_from_day_key(day: str) -> datetime:
    """Return the (exclusive) end timestamp in UTC for a SCORE_DAY_TZ day key."""
    d = datetime.strptime(day, DAY_FMT).date()
    end_local = datetime(d.year, d.month, d.day, 0, 0, tzinfo=SCORE_DAY_TZ) + timedelta(days=1)
    return end_local.astimezone(timezone.utc)


def is_day_closed(day: str, now_utc: Optional[datetime] = None) -> bool:
    """A day is closed once we've passed midnight in SCORE_DAY_TZ (default: America/Vancouver)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return now_utc >= day_end_utc_from_day_key(day)


def _parse_day_key(day: str):
    return datetime.strptime(day, DAY_FMT).date()


def _parse_day_key_loose(day: str) -> Optional[date]:
    """Parse day keys from sheets that may not be strictly zero-padded.

    Accepts:
      - YYYY-MM-DD
      - YYYY-M-D
      - YYYY-MM-DD <anything...>  (we take the first token)
    """
    s = (day or "").strip()
    if not s:
        return None
    s0 = s.split()[0]
    try:
        return datetime.strptime(s0, DAY_FMT).date()
    except Exception:
        pass

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s0)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def prev_day_key(day: str) -> str:
    return (_parse_day_key(day) - timedelta(days=1)).isoformat()


def next_day_key(day: str) -> str:
    return (_parse_day_key(day) + timedelta(days=1)).isoformat()


def month_range_for_day_key(day: str) -> Tuple[str, str]:
    """Given a day key YYYY-MM-DD, return (start_day, end_day) for that month."""
    d = datetime.strptime(day, DAY_FMT).date()
    start = d.replace(day=1)
    if start.month == 12:
        next_month = datetime(start.year + 1, 1, 1).date()
    else:
        next_month = datetime(start.year, start.month + 1, 1).date()
    end = next_month - timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Month-key helpers
#
# A month key is "YYYY-MM". Like day keys, month boundaries follow SCORE_DAY_TZ,
# so a month is only closed once its last score day has closed.
# ---------------------------------------------------------------------------
MONTH_FMT = "%Y-%m"


def month_key_for_day(day: str) -> str:
    """Given a day key YYYY-MM-DD, return its month key YYYY-MM."""
    d = _parse_day_key_loose(day)
    if d is None:
        raise ValueError(f"Unparseable day key: {day!r}")
    return d.strftime(MONTH_FMT)


def _parse_month_key(month: str) -> date:
    """Parse a YYYY-MM month key into the first day of that month."""
    s = (month or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})$", s)
    if not m:
        raise ValueError(f"Unparseable month key: {month!r}")
    return date(int(m.group(1)), int(m.group(2)), 1)


def month_bounds(month: str) -> Tuple[str, str]:
    """Given a month key YYYY-MM, return (first_day, last_day) as day keys."""
    start = _parse_month_key(month)
    return month_range_for_day_key(start.isoformat())


def prev_month_key(month: str) -> str:
    """The month key immediately before *month*."""
    start = _parse_month_key(month)
    return (start - timedelta(days=1)).strftime(MONTH_FMT)


def is_month_closed(month: str, now_utc: Optional[datetime] = None) -> bool:
    """A month is closed once its final score day has closed (SCORE_DAY_TZ)."""
    _start, end = month_bounds(month)
    return is_day_closed(end, now_utc)


# ---------------------------------------------------------------------------
# Puzzle-id filtering
# ---------------------------------------------------------------------------
def choose_primary_puzzle_ids(records: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Within a day, players in earlier timezones can post the *next day's* puzzle before the America/Vancouver cutoff.
    For each game, select the puzzle_id with the most unique players. Tie-break: smallest puzzle_id.
    """
    counts: Dict[str, Dict[int, Set[str]]] = {}
    for rec in records:
        game = normalize_game(str(rec.get('game') or '').strip())
        uid = canonical_user_id(rec.get('user_id') or '')
        raw_pid = (rec.get('puzzle_id') or '').strip()
        if not game or not uid or not raw_pid:
            continue
        try:
            pid = int(raw_pid)
        except Exception:
            continue
        counts.setdefault(game, {}).setdefault(pid, set()).add(uid)

    primary: Dict[str, int] = {}
    for game, by_pid in counts.items():
        best_pid = None
        best_uniq = -1
        for pid, uids in by_pid.items():
            uniq = len(uids)
            if (uniq > best_uniq) or (uniq == best_uniq and (best_pid is None or pid < best_pid)):
                best_uniq = uniq
                best_pid = pid
        if best_pid is not None:
            primary[game] = best_pid

    return primary


def filter_records_to_primary_puzzles(records: List[Dict[str, Any]], primary: Dict[str, int]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rec in records:
        game = normalize_game(str(rec.get('game') or '').strip())
        if not game:
            continue
        if game not in primary:
            continue
        try:
            pid = int(str(rec.get('puzzle_id') or '').strip())
        except Exception:
            continue
        if pid == primary[game]:
            out.append(rec)
    return out


def move_future_puzzle_scores(
    day: str, records: List[Dict[str, Any]], primary: Dict[str, int], store_obj: Any,
) -> bool:
    """Move scores with puzzle_id > primary to the next day bucket.

    Returns True if any rows were moved.
    """
    moved_any = False
    nd = next_day_key(day)
    for rec in records:
        game = normalize_game(str(rec.get("game") or "").strip())
        if not game or game not in primary:
            continue
        uid = canonical_user_id(rec.get("user_id") or "")
        if not uid:
            continue
        try:
            pid = int(str(rec.get("puzzle_id") or "").strip())
        except Exception:
            continue
        if pid > int(primary[game]):
            slack_ts = (rec.get("slack_ts") or "").strip() or None
            moved_any = store_obj.move_score_day(day, nd, uid, game, pid, slack_ts=slack_ts) or moved_any
    return moved_any


# ---------------------------------------------------------------------------
# Expected player logic (v2.1)
# ---------------------------------------------------------------------------
def expected_players_for_day(day: str, store_obj: Optional[Any] = None) -> int:
    """
    Determine how many players to expect today.

    Rule: use the number of unique players who posted *any* score yesterday
    (using yesterday's primary puzzle_id per game). If we have no prior day
    data, fall back to PLAYERS_EXPECTED.
    """
    if not store_obj:
        return PLAYERS_EXPECTED

    prev = prev_day_key(day)
    prev_records = store_obj.load_scores_for_day(prev)
    if not prev_records:
        return PLAYERS_EXPECTED

    prev_primary = choose_primary_puzzle_ids(prev_records)
    prev_primary_records = filter_records_to_primary_puzzles(prev_records, prev_primary)
    players = {
        canonical_user_id(r.get("user_id") or "")
        for r in prev_primary_records
        if canonical_user_id(r.get("user_id") or "")
    }
    return len(players) if players else PLAYERS_EXPECTED


def count_complete_players(records: List[Dict[str, Any]], day: Optional[str] = None) -> int:
    """Count players who have posted all tracked games (within the given record set).

    When *day* is given, only games whose effective_date <= day are required,
    so a newly-added game won't retroactively break older days.
    """
    if day:
        required_games = {g for g, _ in game_registry.games_for_day(day)}
    else:
        required_games = {g for g, _ in game_registry.games_as_tuples()}
    by_user: Dict[str, Set[str]] = {}
    for rec in records:
        uid = canonical_user_id(rec.get("user_id") or "")
        game = normalize_game(str(rec.get("game") or "").strip())
        if not uid or not game:
            continue
        by_user.setdefault(uid, set()).add(game)

    complete = 0
    for games in by_user.values():
        if required_games.issubset(games):
            complete += 1
    return complete


def primary_puzzle_id_for_day_game(day: str, game: str, store_obj: Optional[Any] = None) -> Optional[int]:
    """Best-effort dominant puzzle_id for (day, game).

    If the day has no records for this game yet, we try prev_day_primary + 1.
    """
    if not store_obj:
        return None

    g = normalize_game(game)
    records = store_obj.load_scores_for_day(day)
    if records:
        primary = choose_primary_puzzle_ids(records)
        if g in primary:
            try:
                return int(primary[g])
            except Exception:
                return None

    prev = prev_day_key(day)
    prev_records = store_obj.load_scores_for_day(prev)
    if prev_records:
        prev_primary = choose_primary_puzzle_ids(prev_records)
        if g in prev_primary:
            try:
                return int(prev_primary[g]) + 1
            except Exception:
                return None

    return None


def resolve_pinpoint_fail_score(
    text: str, day: str, store_obj: Optional[Any] = None,
) -> Optional[ParsedScore]:
    """Turn a 'pinpoint fail' message into a scoreable DNF for *day*.

    Returns a ParsedScore (worst-case Pinpoint DNF) when *text* is a Pinpoint
    concession AND we can determine the day's Pinpoint puzzle id, else None.
    The player may pin an explicit "#<id>"; otherwise we use the day's primary
    Pinpoint puzzle. If the puzzle id can't be resolved (no Pinpoint activity
    yet today or yesterday), we return None so the message is left unscored
    rather than filed under a guessed puzzle.
    """
    fail = parse_pinpoint_fail(text)
    if fail is None:
        return None

    puzzle_id = fail.puzzle_id
    if puzzle_id is None:
        puzzle_id = primary_puzzle_id_for_day_game(day, "Pinpoint", store_obj=store_obj)
    if puzzle_id is None:
        return None

    return make_pinpoint_fail_score(int(puzzle_id))


def bump_day_if_future_puzzle(day: str, parsed: Any, store_obj: Optional[Any] = None) -> str:
    """
    If the target day has already been posted, we must not allow a future puzzle_id
    to be written into that day (it would never get rebucketed).

    We detect this by comparing the incoming puzzle_id to the dominant puzzle_id
    for that (day, game) and bumping forward until it fits.
    """
    if not store_obj:
        return day

    try:
        incoming_pid = int(parsed.puzzle_id)
    except Exception:
        return day

    cur = day
    posted_days = store_obj.get_posted_days_snapshot(force_refresh=False)
    while cur in posted_days:
        p_today = primary_puzzle_id_for_day_game(cur, parsed.game, store_obj=store_obj)
        if p_today is None:
            break
        if incoming_pid > int(p_today):
            cur = next_day_key(cur)
            continue
        break
    return cur
