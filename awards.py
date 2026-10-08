"""awards.py

The award model shared by scoring, the DailyResults readers, and every renderer.

There are two scoring eras, split by *score day* (never by when a day happens to
be finalized, so a day that closes after midnight keeps its own rules):

  legacy  Days before MEDAL_SCORING_START. Each game has one winner, who gets a
          trophy; a tie for first gives each tied player a necktie instead.
  medals  MEDAL_SCORING_START and later. Each game awards a podium: 1st, 2nd and
          3rd place are worth 3, 2 and 1 points (gold, silver and bronze medals).
          Ties use Olympic ranking: players with the same result share a place,
          and the places they occupy are skipped, so two firsts are followed by
          a third.

DailyResults rows are never rewritten, so both shapes live in the ledger side by
side: ``{"wins": n, "ties": n}`` for legacy days and
``{"gold": n, "silver": n, "bronze": n, "points": n}`` for medal days.
``AwardTally`` reads either, and adds across them.

This module is a leaf on purpose. insights, nl_query, weekly_summaries and
recap_commands avoid importing config (and its required environment variables),
so the shared pieces cannot live there.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Iterable, Optional, Tuple


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Emoji
# ---------------------------------------------------------------------------
# Legacy era.
TROPHY = ":trophy:"
NECKTIE = ":necktie:"

# Medal era. These are Slack's built-in short codes. GitHub spells the same
# emoji 1st_place_medal / 2nd_place_medal / 3rd_place_medal, which Slack does not
# recognise, so those would post as literal text. Change them here if the
# workspace defines its own.
GOLD = ":first_place_medal:"
SILVER = ":second_place_medal:"
BRONZE = ":third_place_medal:"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
POINTS_BY_PLACE: Dict[int, int] = {1: 3, 2: 2, 3: 1}
MEDAL_BY_PLACE: Dict[int, str] = {1: GOLD, 2: SILVER, 3: BRONZE}
MAX_MEDAL_PLACE = max(POINTS_BY_PLACE)

DEFAULT_MEDAL_SCORING_START = "2026-10-01"
MEDAL_SCORING_START_ENV = "MEDAL_SCORING_START"

_warned_bad_start: set = set()


def medal_scoring_start() -> str:
    """First score day (YYYY-MM-DD) scored with medals.

    Read from the environment on every call rather than at import: config.py
    loads .env on import, and modules that never import config would otherwise
    capture the default before it is applied.
    """
    raw = (os.environ.get(MEDAL_SCORING_START_ENV) or "").strip()
    if not raw:
        return DEFAULT_MEDAL_SCORING_START
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        if raw not in _warned_bad_start:
            _warned_bad_start.add(raw)
            logger.warning(
                "%s=%r is not a YYYY-MM-DD date; using %s",
                MEDAL_SCORING_START_ENV, raw, DEFAULT_MEDAL_SCORING_START,
            )
        return DEFAULT_MEDAL_SCORING_START


def _parse_day(day: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(day or "").strip()[:10])
    except ValueError:
        return None


def uses_medal_scoring(day: Any) -> bool:
    """True when *day* (a score-day key or a date) is scored with medals.

    A missing or unparseable day is treated as legacy: callers that cannot say
    which day they mean get the behaviour that existed before the cut-over.
    """
    d = _parse_day(day)
    return d is not None and d >= date.fromisoformat(medal_scoring_start())


def scoring_era(days: Iterable[Any]) -> str:
    """'medals', 'legacy', or 'mixed' for a set of score days ('legacy' if empty)."""
    flags = {uses_medal_scoring(d) for d in days}
    if flags == {True}:
        return "medals"
    if True in flags:
        return "mixed"
    return "legacy"


def points_for_place(place: int) -> int:
    return POINTS_BY_PLACE.get(int(place), 0)


# ---------------------------------------------------------------------------
# Tally
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AwardTally:
    """What a player earned over some set of games or days.

    A window that crosses the cut-over carries both families, which is why they
    sit side by side rather than being folded together: a trophy and a gold medal
    are both "a first place", but only the medals family has points.
    """

    wins: int = 0     # legacy: games won outright (trophy)
    ties: int = 0     # legacy: games tied for first (necktie)
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    points: int = 0

    def __add__(self, other: Any) -> "AwardTally":
        if not isinstance(other, AwardTally):
            return NotImplemented
        return AwardTally(
            self.wins + other.wins, self.ties + other.ties,
            self.gold + other.gold, self.silver + other.silver, self.bronze + other.bronze,
            self.points + other.points,
        )

    @property
    def has_legacy(self) -> bool:
        return bool(self.wins or self.ties)

    @property
    def has_medals(self) -> bool:
        return bool(self.gold or self.silver or self.bronze or self.points)

    @property
    def firsts(self) -> int:
        """First-place finishes: outright trophies plus gold medals."""
        return self.wins + self.gold

    @property
    def rank_key(self) -> Tuple[int, ...]:
        """Higher is better. Points decide, then count-back on gold, silver, bronze.

        For a legacy-only tally the leading four entries are zero, so this orders
        by (wins, ties) exactly as the standings always have.
        """
        return (self.points, self.gold, self.silver, self.bronze, self.wins, self.ties)

    @property
    def win_key(self) -> Tuple[int, ...]:
        """Higher is better. First-place finishes decide, then points.

        Trophies and gold medals are both "a win", so this is the one ordering that
        means the same thing across the cut-over: it ranks a long history of
        trophies above a week of medals, where ``rank_key`` would not. For a
        legacy-only tally it orders by (wins, ties), like ``rank_key``.
        """
        return (self.firsts, self.points, self.silver, self.bronze, self.ties)

    def sort_key(self, tiebreak: str = "", *, by_wins: bool = False) -> Tuple[Tuple[int, ...], str]:
        """Best-first sort key with a stable tiebreak (usually the user id).

        Ranks by ``rank_key`` (the standings: points first) or, with *by_wins*,
        by ``win_key`` (first-place finishes first).
        """
        key = self.win_key if by_wins else self.rank_key
        return (tuple(-n for n in key), tiebreak)

    def as_dict(self) -> Dict[str, int]:
        return {
            "wins": self.wins, "ties": self.ties,
            "gold": self.gold, "silver": self.silver, "bronze": self.bronze,
            "points": self.points,
        }


def medal_tally(place: int) -> AwardTally:
    """The tally for one finish in *place*: its medal and its points (empty past third)."""
    place = int(place)
    if place not in POINTS_BY_PLACE:
        return AwardTally()
    return AwardTally(
        gold=int(place == 1), silver=int(place == 2), bronze=int(place == 3),
        points=POINTS_BY_PLACE[place],
    )


def _as_int(x: Any) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def unpack_awards(v: Any) -> AwardTally:
    """Read any stored award value into a tally.

    Accepts a bare int (the oldest ledger rows stored just a trophy count), a
    legacy ``{"wins"|"trophies", "ties"}`` dict, a medal dict, or a totals-map
    entry carrying all six fields. Anything else is an empty tally.
    """
    if isinstance(v, AwardTally):
        return v
    if isinstance(v, bool):
        return AwardTally()
    if isinstance(v, int):
        return AwardTally(wins=v)
    if isinstance(v, dict):
        return AwardTally(
            wins=_as_int(v.get("wins", v.get("trophies"))),
            ties=_as_int(v.get("ties")),
            gold=_as_int(v.get("gold")),
            silver=_as_int(v.get("silver")),
            bronze=_as_int(v.get("bronze")),
            points=_as_int(v.get("points")),
        )
    return AwardTally()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def compact_legacy(wins: int, ties: int, show_zeros: bool = False, sep: str = ", ") -> str:
    """Trophy/necktie chips, e.g. ':trophy:x6, :necktie:x2'.

    wins=0, ties=0 renders ':trophy:x0' (or both chips with show_zeros).
    """
    wins = int(wins or 0)
    ties = int(ties or 0)
    parts = []
    if show_zeros or wins > 0:
        parts.append(f"{TROPHY}x{wins}")
    if show_zeros or ties > 0:
        parts.append(f"{NECKTIE}x{ties}")
    return sep.join(parts) if parts else f"{TROPHY}x0"


def compact_medals(gold: int, silver: int, bronze: int, show_zeros: bool = False, sep: str = ", ") -> str:
    """Medal chips, e.g. ':first_place_medal:x6, :second_place_medal:x2'.

    Empty when nothing was won, unless show_zeros.
    """
    parts = []
    for emoji, n in ((GOLD, gold), (SILVER, silver), (BRONZE, bronze)):
        n = int(n or 0)
        if show_zeros or n > 0:
            parts.append(f"{emoji}x{n}")
    return sep.join(parts)


def format_points(points: int) -> str:
    points = int(points or 0)
    return f"{points} pt" if points == 1 else f"{points} pts"


def render_tally(
    t: AwardTally,
    *,
    empty_era: str = "legacy",
    show_zeros: bool = False,
    legacy_sep: str = ", ",
) -> str:
    """Render a tally in whichever era(s) it contains.

      legacy  ':trophy:x6, :necktie:x2'
      medals  '14 pts (:first_place_medal:x4, :second_place_medal:x1, :third_place_medal:x0)'
      mixed   both of the above joined by ' | '

    *empty_era* picks the zero rendering for a tally with nothing in it ('legacy'
    or 'medals'), so a player with no awards on a medal day reads '0 pts' rather
    than '0 trophies'. *legacy_sep* separates the trophy and necktie chips: the
    natural-language answers have always joined those with two spaces.
    """
    parts = []
    if t.has_legacy or (not t.has_medals and empty_era != "medals"):
        parts.append(compact_legacy(t.wins, t.ties, show_zeros=show_zeros, sep=legacy_sep))
    if t.has_medals or (not t.has_legacy and empty_era == "medals"):
        chips = compact_medals(t.gold, t.silver, t.bronze, show_zeros=show_zeros)
        parts.append(f"{format_points(t.points)} ({chips})" if chips else format_points(t.points))
    return " | ".join(parts)


def _slack_mention(uid: str) -> str:
    return f"<@{uid}>"


def render_podium(podium: Any, *, mention: Callable[[str], str] = _slack_mention) -> str:
    """One game's medal winners on one line.

    ':first_place_medal: <@U1>, <@U2> (0:30) | :third_place_medal: <@U3> (0:41)'

    A tie shares a medal and the skipped place has no entry, so the line above
    goes straight from gold to bronze. A group whose equal results were separated
    by the tiebreak says so.
    """
    groups = []
    for entry in podium or []:
        if not isinstance(entry, dict):
            continue
        medal = MEDAL_BY_PLACE.get(_as_int(entry.get("place")))
        uids = [str(u).strip() for u in entry.get("user_ids") or [] if str(u).strip()]
        if not medal or not uids:
            continue
        detail = [d for d in (str(entry.get("display") or "").strip(),
                              "tiebreak" if entry.get("tiebreak") else "") if d]
        text = f"{medal} {', '.join(mention(u) for u in uids)}"
        if detail:
            text += f" ({', '.join(detail)})"
        groups.append(text)
    return " | ".join(groups)
