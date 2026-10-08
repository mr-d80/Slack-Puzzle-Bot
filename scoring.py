"""scoring.py

Winner computation, award tracking, and summary formatting for daily results.

Two scoring eras (see awards.py): days before MEDAL_SCORING_START keep the
trophy/necktie rules; later days award a 3/2/1 point podium with Olympic ties.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from awards import (
    TROPHY, NECKTIE, GOLD, MAX_MEDAL_PLACE,
    points_for_place, uses_medal_scoring, unpack_awards, render_tally, render_podium,
    compact_legacy,
)
from game_registry import game_registry
from parser import normalize_game, canonical_user_id, _parse_tiebreak
from score_metrics import metric_sort_value, record_is_dnf, metric_unit


# ---------------------------------------------------------------------------
# Award helpers
# ---------------------------------------------------------------------------
def _inc_award(awards: Dict[str, Dict[str, int]], uid: str, wins: int = 0, ties: int = 0) -> None:
    if uid not in awards:
        awards[uid] = {"wins": 0, "ties": 0}
    awards[uid]["wins"] += wins
    awards[uid]["ties"] += ties


def _inc_medal(awards: Dict[str, Dict[str, int]], uid: str, place: int) -> None:
    if uid not in awards:
        awards[uid] = {"gold": 0, "silver": 0, "bronze": 0, "points": 0}
    awards[uid][("gold", "silver", "bronze")[place - 1]] += 1
    awards[uid]["points"] += points_for_place(place)


def compact_awards(wins: int, ties: int, show_zeros: bool = False) -> str:
    """
    Render legacy awards as Slack emoji multipliers.

    Examples:
      - wins=6, ties=2 -> ":trophy:x6, :necktie:x2"
      - wins=6, ties=0 -> ":trophy:x6"  (unless show_zeros=True)
      - wins=0, ties=0 -> ":trophy:x0"  (or ":trophy:x0, :necktie:x0" if show_zeros=True)

    Medal-era awards render through awards.render_tally.
    """
    return compact_legacy(wins, ties, show_zeros=show_zeros)


# ---------------------------------------------------------------------------
# Score record helpers
# ---------------------------------------------------------------------------
def _metric_value(rec: Dict[str, Any]) -> Optional[int]:
    raw = rec.get("metric_value")
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def _display_metric_value(rec: Dict[str, Any], value: int) -> str:
    """Keep parser display text, adding an explicit unit to bare point scores."""
    display = str(rec.get("display") or "").strip()
    metric_type = str(rec.get("metric_type") or "time").strip() or "time"
    if metric_type == "points" and not any(token in display.lower() for token in ("pt", "point")):
        return f"{display or value} {metric_unit(metric_type)}"
    return display or str(value)


def _tiebreak_value(rec: Dict[str, Any]) -> Optional[int]:
    """Read tiebreak_value from a score record (None if absent/empty).

    Falls back to parsing raw_text for scores stored before the
    tiebreak_value column existed.
    """
    raw = rec.get("tiebreak_value")
    if raw is not None and str(raw).strip() != "":
        try:
            return int(str(raw).strip())
        except Exception:
            return None
    # Fallback: derive from raw_text for pre-migration scores
    game = normalize_game(str(rec.get("game") or ""))
    raw_text = str(rec.get("raw_text") or "")
    if raw_text:
        return _parse_tiebreak(game, raw_text)
    return None


def _group_by_game(records: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_game: Dict[str, List[Dict[str, Any]]] = {g: [] for g in game_registry.game_names()}
    for r in records:
        g = normalize_game(str(r.get("game") or ""))
        if g in by_game:
            by_game[g].append(r)
    return by_game


def _day_of_records(records: List[Dict[str, Any]]) -> Optional[str]:
    """The score day the records belong to, or None if they don't agree on one."""
    days = {str(r.get("day") or "").strip() for r in records}
    days.discard("")
    return days.pop() if len(days) == 1 else None


# ---------------------------------------------------------------------------
# Daily winner computation
# ---------------------------------------------------------------------------
def compute_daily_winners(
    records: List[Dict[str, Any]],
    day: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]], Dict[str, Any]]:
    """
    Uses Scores-sheet fields:
      - game
      - metric_value (stringified int)
      - display (pretty string)

    Lower metrics win for time and guesses; higher metrics win for points.

    The scoring rules depend on the score day. *day* says which; when omitted it
    is read from the records, and records that don't name a single day are
    scored with the legacy rules.

      legacy  one winner per game gets a trophy; a tie for first gives each tied
              player a necktie (see _compute_legacy_winners).
      medals  a podium per game, 3/2/1 points for 1st/2nd/3rd (see
              _compute_medal_results).

    Returns (winners_by_game, awards_by_user, best_display).
    """
    if day is None:
        day = _day_of_records(records)
    if uses_medal_scoring(day):
        return _compute_medal_results(records)
    return _compute_legacy_winners(records)


def _compute_legacy_winners(records: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]], Dict[str, Any]]:
    """Trophy/necktie rules, for score days before the medal cut-over.

    Ties: each tied winner gets a :necktie: for that game.
    """
    required_games = game_registry.game_names()
    by_game = _group_by_game(records)

    winners_by_game: Dict[str, Any] = {}
    awards_by_user: Dict[str, Dict[str, int]] = {}
    best_display: Dict[str, Any] = {}

    for game in required_games:
        rows = by_game.get(game, [])
        parsed: List[Tuple[int, Dict[str, Any]]] = []
        for r in rows:
            v = _metric_value(r)
            if v is None or record_is_dnf(r):
                continue
            parsed.append((v, r))

        if not parsed:
            continue

        metric_type = str(parsed[0][1].get("metric_type") or "time").strip() or "time"
        best_val = min((v for v, _ in parsed), key=lambda v: metric_sort_value(v, metric_type))
        winners = [r for v, r in parsed if v == best_val]

        # Tiebreak: when 2+ players share the best metric_value, use
        # tiebreak_value (lower is better) to try to resolve.
        # Only applied when ALL tied players have tiebreak data; if any
        # player is missing it we can't compare fairly, so the tie stands.
        tiebreak_used = False
        if len(winners) >= 2:
            tb_vals = [(_tiebreak_value(w), w) for w in winners]
            if all(t is not None for t, _ in tb_vals):
                best_tb = min(t for t, _ in tb_vals)
                narrowed = [w for t, w in tb_vals if t == best_tb]
                if len(narrowed) < len(winners):
                    winners = narrowed
                    tiebreak_used = True

        # display: prefer the first winner's display cell
        best_row = winners[0]
        best_display[game] = _display_metric_value(best_row, best_val)

        if len(winners) >= 2:
            for w in winners:
                uid = canonical_user_id(w.get("user_id") or "")
                if uid:
                    _inc_award(awards_by_user, uid, ties=1)
            winners_by_game[game] = {"result": "tie", "icon": NECKTIE, "winners": winners, "best_value": best_val}
        else:
            uid = canonical_user_id(best_row.get("user_id") or "")
            if uid:
                _inc_award(awards_by_user, uid, wins=1)
            winners_by_game[game] = {
                "result": "win", "icon": TROPHY, "winners": winners, "best_value": best_val,
                "tiebreak": tiebreak_used,
            }

    return winners_by_game, awards_by_user, best_display


def _is_dnf(rec: Dict[str, Any], value: Optional[int] = None) -> bool:
    """Compatibility wrapper for the parser's status-aware DNF check."""
    if value is None:
        return record_is_dnf(rec)
    candidate = dict(rec)
    candidate.setdefault("metric_value", value)
    return record_is_dnf(candidate)


def _split_by_tiebreak(tied: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Order players who share a result using tiebreak_value (lower is better).

    Returns groups best first. Players still level on the tiebreak share a
    group. As in the legacy rules the tiebreak applies only when *every* tied
    player has tiebreak data; if any is missing we can't compare fairly, so the
    whole group stays together.
    """
    if len(tied) < 2:
        return [tied]
    keyed = [(_tiebreak_value(r), r) for r in tied]
    if any(t is None for t, _ in keyed):
        return [tied]
    by_tb: Dict[int, List[Dict[str, Any]]] = {}
    for t, r in keyed:
        by_tb.setdefault(t, []).append(r)  # type: ignore[arg-type]
    return [by_tb[t] for t in sorted(by_tb)]


def rank_finishers(
    parsed: List[Tuple[int, Dict[str, Any]]], metric_type: str = "time"
) -> List[Dict[str, Any]]:
    """Group one game's finishers into places using Olympic ranking.

    *parsed* is (metric_value, record) pairs; direction follows metric_type.
    ``metric_type`` defaults to time for existing public callers. Returns one dict
    per place group, best first::

        {"place": int, "records": [record, ...], "value": int, "tiebreak": bool}

    Players with the same result share a place, and the places they occupy are
    skipped: two players tied for first are followed by third place, not second.
    The tiebreak splits players who share a result; ``tiebreak`` is True on
    every group that came out of such a split.
    """
    by_value: Dict[int, List[Dict[str, Any]]] = {}
    for value, rec in parsed:
        by_value.setdefault(value, []).append(rec)

    groups: List[Dict[str, Any]] = []
    place = 1
    for value in sorted(by_value, key=lambda v: metric_sort_value(v, metric_type)):
        subgroups = _split_by_tiebreak(by_value[value])
        split = len(subgroups) > 1
        for sub in subgroups:
            groups.append({"place": place, "records": sub, "value": value, "tiebreak": split})
            place += len(sub)
    return groups


def _compute_medal_results(records: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, int]], Dict[str, Any]]:
    """Podium rules, for score days from the medal cut-over on.

    Per game, 1st/2nd/3rd place are worth 3/2/1 points. Players with the same
    result share a place and the places they occupy are skipped (Olympic ranking),
    so two firsts each get 3 points and the next player is third with 1. The
    tiebreak applies to every tied group, not just the top one.

    A conceded puzzle (a Pinpoint DNF) is not a finish and can't take a medal,
    consistent with a DNF never being able to win.

    ``winners_by_game[game]`` keeps the shape the legacy readers expect: its
    ``winners`` and ``best_value`` describe first place, and ``result`` is 'tie'
    when first place is shared. The full podium is under ``podium``.
    """
    by_game = _group_by_game(records)

    winners_by_game: Dict[str, Any] = {}
    awards_by_user: Dict[str, Dict[str, int]] = {}
    best_display: Dict[str, Any] = {}

    for game in game_registry.game_names():
        finishers: List[Tuple[int, Dict[str, Any]]] = []
        for r in by_game.get(game, []):
            v = _metric_value(r)
            if v is None or _is_dnf(r, v):
                continue
            finishers.append((v, r))

        if not finishers:
            continue

        metric_type = str(finishers[0][1].get("metric_type") or "time").strip() or "time"
        groups = rank_finishers(finishers, metric_type=metric_type)

        podium: List[Dict[str, Any]] = []
        for g in groups:
            if g["place"] > MAX_MEDAL_PLACE:
                break
            uids: List[str] = []
            for r in g["records"]:
                uid = canonical_user_id(r.get("user_id") or "")
                if uid:
                    uids.append(uid)
                    _inc_medal(awards_by_user, uid, g["place"])
            if not uids:
                continue
            podium.append({
                "place": g["place"],
                "points": points_for_place(g["place"]),
                "user_ids": uids,
                "value": g["value"],
                "display": _display_metric_value(g["records"][0], g["value"]),
                "tiebreak": g["tiebreak"],
            })

        first = groups[0]
        winners = first["records"]
        best_display[game] = _display_metric_value(winners[0], first["value"])
        winners_by_game[game] = {
            "result": "tie" if len(winners) >= 2 else "win",
            "icon": GOLD,
            "winners": winners,
            "best_value": first["value"],
            "tiebreak": first["tiebreak"],
            "podium": podium,
        }

    return winners_by_game, awards_by_user, best_display


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------
def format_summary(
    day: str,
    winners_by_game: Dict[str, Any],
    awards_by_user: Dict[str, Any],
    totals_map: Dict[str, Any],
    best_display: Dict[str, Any],
    players: Optional[List[str]] = None,
) -> str:
    """
    Message layout:
      1) Winners by game (the podium, from the medal cut-over on)
      2) Today's awards by player
      3) Running totals (this month)

    Days before the cut-over keep the trophy and necktie layout; later days show
    each game's medals and every player's points.
    """
    medals = uses_medal_scoring(day)
    empty_era = "medals" if medals else "legacy"

    lines: List[str] = []
    lines.append(f"*Daily results for {day}*")

    # --- By game ---
    for game in game_registry.game_names():
        outcome = winners_by_game.get(game)
        if not outcome:
            continue

        if medals and outcome.get("podium"):
            podium_line = render_podium(outcome["podium"])
            if podium_line:
                lines.append(f"- {game}: {podium_line}")
            continue

        icon = outcome.get("icon", TROPHY)
        winners = outcome.get("winners", [])

        name_bits: List[str] = []
        for w in winners:
            uid = canonical_user_id(w.get("user_id") or "")
            if uid:
                name_bits.append(f"<@{uid}>")

        best_val = best_display.get(game, "")
        if name_bits:
            if outcome.get("result") == "tie":
                lines.append(f"- {game}: {icon} tie between {', '.join(name_bits)} (best: {best_val})")
            elif outcome.get("tiebreak"):
                lines.append(f"- {game}: {icon} {name_bits[0]} (best: {best_val}, tiebreak)")
            else:
                lines.append(f"- {game}: {icon} {name_bits[0]} (best: {best_val})")

    # --- By player (today) ---
    lines.append("")
    lines.append("*Today's awards*")

    # Determine player list (prefer actual participants for the day)
    if players is None:
        inferred: Set[str] = set()
        for uid in (awards_by_user or {}).keys():
            if uid:
                inferred.add(uid)
        for uid in (totals_map or {}).keys():
            if uid:
                inferred.add(uid)
        players = sorted(inferred)
    else:
        players = [p for p in players if p]

    today = {uid: unpack_awards((awards_by_user or {}).get(uid)) for uid in players}

    # Sort best first (points, then gold/silver/bronze; legacy: wins then ties), then user id
    for uid in sorted(players, key=lambda u: today[u].sort_key(u)):
        lines.append(f"- <@{uid}> {render_tally(today[uid], empty_era=empty_era, show_zeros=True)}")

    # --- Running totals ---
    lines.append("")
    lines.append("*Running totals (this month)*")

    totals = {uid: unpack_awards(info) for uid, info in (totals_map or {}).items()}
    for uid in sorted(totals, key=lambda u: totals[u].sort_key(u)):
        lines.append(f"- <@{uid}> {render_tally(totals[uid], empty_era=empty_era, show_zeros=False)}")

    return "\n".join(lines)
