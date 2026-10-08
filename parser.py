"""Regex-based extraction of puzzle scores from Slack messages."""

import re
from datetime import date, datetime
from dataclasses import dataclass
from typing import Dict, Optional, Union

from game_registry import game_registry


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Some users post the time on the next line (e.g., "Zip #316\n0:13 :checkered_flag:").
TIME_LINE_RE = re.compile(r"(?m)^\s*(\d+):(\d+)\b")

PINPOINT_GUESSLINE_RE = re.compile(
    r"(?mi)^\s*:(one|two|three|four|five|six|seven|eight|nine|ten):\s*\|"
)

PINPOINT_DNF_PENALTY = 100

# A bare "pinpoint fail" (a player conceding without a shareable score card)
# is scored as the worst possible Pinpoint result: all guesses used and unsolved.
# Pinpoint gives 5 guesses, so metric_value == 5 + PINPOINT_DNF_PENALTY.
PINPOINT_MAX_GUESSES = 5

# Matches a message whose sole intent is to concede Pinpoint, e.g.
#   "pinpoint fail", "Pinpoint failed", "pinpoint - fail", "pinpoint #42 fail".
# An optional "#<id>" lets a player pin the concession to a specific puzzle;
# when omitted the day's primary Pinpoint puzzle is used. Anchored to the whole
# (stripped) message so ordinary chatter mentioning "fail" doesn't trigger it.
PINPOINT_FAIL_RE = re.compile(
    r"^\s*pinpoint\b\s*(?:#\s*(\d+)\b)?\s*[-:|]?\s*fail(?:ed|s)?\s*[.!]*\s*$",
    re.IGNORECASE,
)

# Games that support tiebreak via flawless / backtracks / redraws.
# Maps canonical game name -> tiebreak detection strategy.
_TIEBREAK_GAMES: Dict[str, str] = {
    "Crossclimb": "flawless",
    "Tango": "flawless",
    "Mini Sudoku": "flawless",
    "Zip": "flawless_or_backtracks",
    "Patches": "redraws",
}

_FLAWLESS_RE = re.compile(r"\bflawless\b", re.IGNORECASE)
_BACKTRACKS_RE = re.compile(r"\b(\d+)\s*backtracks?\b", re.IGNORECASE)
_NO_BACKTRACKS_RE = re.compile(r"\bno\s+backtracks?\b", re.IGNORECASE)
_REDRAWS_RE = re.compile(r"\b(\d+)\s*redraws?\b", re.IGNORECASE)
_NO_REDRAWS_RE = re.compile(r"\bno\s+redraws?\b", re.IGNORECASE)

# Slack mrkdwn can wrap tokens with *, _, ~, or `.
_MRKDWN_WRAP_CHARS = "*_~`"

# Matches <@U123ABC>, <@U123ABC|whatever>, including when wrapped by mrkdwn.
_SLACK_USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")

_MONTHS = (
    "January February March April May June July August September October November December"
).split()
_MONTH_ALTERNATION = "|".join(
    sorted({month for full in _MONTHS for month in (full, full[:3])}, key=len, reverse=True)
)
_ENGLISH_DATE_RE = re.compile(
    rf"(?i)\b(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?[,]?\s+)?"
    rf"(?P<month>{_MONTH_ALTERNATION})\.?\s+"
    rf"(?P<day>\d{{1,2}})(?:,?\s+(?P<year>\d{{4}}))?\b"
)
_ISO_DATE_RE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_EMOJI_ROW_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]|"
    r":(?:red|orange|yellow|green|blue|purple|white|black)_square:|:star:|:star2:|"
    r":large_blue_diamond:|:large_orange_diamond:|:diamond_shape_with_a_dot_inside:|"
    r":white_medium_star:|:sparkles:",
    re.IGNORECASE,
)
_ARCHIVE_LINK_RE = re.compile(r"(?i)#[^\s<>)]*(?:[?&])?(?:p|d|b)=")
_RESCUE_RE = re.compile(r"(?:🛟|:life_buoy:)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedScore:
    game: str
    puzzle_id: int
    metric_type: str  # "time", "guesses", or "points"
    metric_value: int  # seconds, guesses, or points
    display: str
    tiebreak_value: Optional[int] = None  # lower is better; 0 = flawless, N = backtracks/redraws
    status: str = "solved"
    score_day: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def strip_mrkdwn_wrap(s: str) -> str:
    '''Strip common Slack mrkdwn wrapper characters from both ends.'''
    return (s or "").strip().strip(_MRKDWN_WRAP_CHARS)


def canonical_user_id(token: str) -> str:
    '''
    Normalize a user identifier that may appear as:
      - U123ABC
      - <@U123ABC>
      - <@U123ABC|name>
      - *<@U123ABC>* (or other mrkdwn wrappers)
    Returns a best-effort raw user id string.
    '''
    t = strip_mrkdwn_wrap(token or "")
    m = _SLACK_USER_MENTION_RE.search(t)
    if m:
        return m.group(1)

    # Fallback: find a bare Slack-style user id token
    m2 = re.search(r"\b([UW][A-Z0-9]{8,})\b", t)
    if m2:
        return m2.group(1)

    return t.strip()


def normalize_game(name: str) -> str:
    raw = strip_mrkdwn_wrap(name or "")
    resolved = game_registry.canonical_name(raw)
    if resolved != raw.strip():
        return resolved
    n = raw.strip().lower().replace("×", "x")
    for g, _ in game_registry.games_as_tuples():
        if g.lower().replace("×", "x") == n:
            return g
    return raw.strip()


def _reference_date(reference_day: Optional[object]) -> date:
    if reference_day is None:
        return date.today()
    if isinstance(reference_day, datetime):
        return reference_day.date()
    if isinstance(reference_day, date):
        return reference_day
    if isinstance(reference_day, str):
        try:
            return date.fromisoformat(reference_day.strip())
        except ValueError as exc:
            raise ValueError("reference_day must be a date or ISO date string") from exc
    raise TypeError("reference_day must be a date or ISO date string")


def _nearest_date(month: int, day: int, reference_day: date) -> date:
    candidates = []
    for year in (reference_day.year - 1, reference_day.year, reference_day.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise ValueError("invalid month/day")
    return min(candidates, key=lambda candidate: (abs((candidate - reference_day).days), candidate))


def _date_from_text(text: str, reference_day: date) -> Optional[date]:
    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            return date(int(iso.group("year")), int(iso.group("month")), int(iso.group("day")))
        except ValueError:
            return None

    english = _ENGLISH_DATE_RE.search(text)
    if not english:
        return None
    month_text = english.group("month").lower()
    month = next(i for i, full in enumerate(_MONTHS, 1) if full.lower().startswith(month_text))
    day = int(english.group("day"))
    year = english.group("year")
    try:
        return date(int(year), month, day) if year else _nearest_date(month, day, reference_day)
    except ValueError:
        return None


def _emoji_rows(text: str) -> int:
    return sum(1 for line in text.splitlines() if len(_EMOJI_ROW_RE.findall(line)) >= 3)


def _registered(game: str, metric_type: str) -> bool:
    return game_registry.is_registered(game, metric_type)


def _parse_wordle(text: str) -> Optional[ParsedScore]:
    if not _registered("Wordle", "guesses"):
        return None
    match = re.search(
        r"(?im)^\s*Wordle\s*#?\s*(\d{1,3}(?:,\d{3})*|\d{4,7})\s+"
        r"(?:[-–—]\s*)?([1-6x])\s*/\s*6(?:\*)?\s*$",
        text,
    )
    if not match:
        return None
    puzzle_id = int(match.group(1).replace(",", ""))
    result = match.group(2).lower()
    failed = result == "x"
    value = 106 if failed else int(result)
    return ParsedScore(
        game="Wordle",
        puzzle_id=puzzle_id,
        metric_type="guesses",
        metric_value=value,
        display="DNF(6)" if failed else f"{value}/6",
        status="failed" if failed else "solved",
    )


def _has_game_marker(text: str, game: str) -> bool:
    if game == "4x6":
        return bool(re.search(r"(?i)\b4\s*[x×]\s*6\b|hankgreen\.com/4x6\b", text))
    if game == "4x3":
        return bool(
            re.search(r"(?i)\b4\s*[x×]\s*3\b|4x3\.fun\b|hankgreen\.com/fourbythree\b", text)
        )
    return False


def _parse_four_by_six(text: str, reference_day: date) -> Optional[ParsedScore]:
    if not _registered("4x6", "points") or not _has_game_marker(text, "4x6"):
        return None
    if _ARCHIVE_LINK_RE.search(text):
        return None
    header = re.search(
        rf"(?im)^\s*4\s*[x×]\s*6\s*[·|–—-]\s*"
        rf"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?[,]?\s+)?"
        rf"(?:{_MONTH_ALTERNATION})\.?\s+\d{{1,2}}(?:,?\s+\d{{4}})?\s*$",
        text,
    )
    day = _date_from_text(text, reference_day)
    if not header or day is None or _emoji_rows(text) != 6:
        return None
    footer = re.search(
        r"(?i)\b\d+\s*/\s*\d+\s+par?\s*moves?\b[^\r\n]*?"
        r"\b(\d+)\s+pts?\b[^\r\n]*",
        text,
    )
    if not footer:
        # Official shares sometimes omit the word "par" while retaining the
        # moves ratio; keep the surrounding footer requirement strict.
        footer = re.search(
            r"(?i)\b\d+\s*/\s*\d+\s+moves?\b[^\r\n]*?\b(\d+)\s+pts?\b",
            text,
        )
    if not footer:
        return None
    score = int(footer.group(1))
    failed = bool(_RESCUE_RE.search(text))
    return ParsedScore(
        game="4x6",
        puzzle_id=day.toordinal(),
        metric_type="points",
        metric_value=score,
        display=f"{score} pts",
        status="failed" if failed else "solved",
        score_day=day.isoformat(),
    )


def _parse_four_by_three(text: str, reference_day: date) -> Optional[ParsedScore]:
    if not _registered("4x3", "points") or not _has_game_marker(text, "4x3"):
        return None
    if _ARCHIVE_LINK_RE.search(text):
        return None
    day = _date_from_text(text, reference_day)
    if day is None:
        return None
    out_of_guesses = bool(re.search(r"(?i)\bout\s+of\s+guesses\b", text))
    wrong_hub = bool(re.search(r"(?i)\bcalled\s+the\s+wrong\s+hub\b", text))
    rule_breaker = bool(re.search(r"(?i)\brule\s+breaker\b", text))
    failed = not rule_breaker and (out_of_guesses or wrong_hub)
    points_match = re.search(r"(?i)(?<!\w)(-?\d+)\s+points?\b", text)
    emoji_rows = _emoji_rows(text)
    # Shares show up to five three-tile guesses. Explicit failures can have no
    # rows when a wrong hub call ends the board immediately.
    if emoji_rows > 5 or (not failed and emoji_rows < 1):
        return None
    if not points_match and not failed:
        return None
    score = int(points_match.group(1)) if points_match else 0
    return ParsedScore(
        game="4x3",
        puzzle_id=day.toordinal(),
        metric_type="points",
        metric_value=score,
        display=f"{score} pts" if not failed else "DNF",
        status="failed" if failed else "solved",
        score_day=day.isoformat(),
    )


def _parse_maptap_date(text: str, reference_day: date) -> Optional[date]:
    parsed = _date_from_text(text, reference_day)
    if parsed:
        return parsed

    # Slack can flatten the native opening "October 8" and the first round's
    # "100" into "October 8100". Split only when there is exactly one valid
    # day + first-round-score interpretation.
    match = re.search(rf"(?i)\b(?P<month>{_MONTH_ALTERNATION})\.?\s+(?P<digits>\d{{3,5}})", text)
    if not match:
        return None
    month_text = match.group("month").lower()
    month = next(i for i, full in enumerate(_MONTHS, 1) if full.lower().startswith(month_text))
    digits = match.group("digits")
    interpretations = []
    for split_at in (1, 2):
        if split_at >= len(digits):
            continue
        day = int(digits[:split_at])
        first_round_score = int(digits[split_at:])
        if 1 <= day <= 31 and 0 <= first_round_score <= 100:
            try:
                interpretations.append(_nearest_date(month, day, reference_day))
            except ValueError:
                pass
    unique = {candidate.isoformat(): candidate for candidate in interpretations}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _parse_maptap(text: str, reference_day: date) -> Optional[ParsedScore]:
    if not _registered("MapTap", "points") or not re.search(r"(?i)maptap\.gg", text):
        return None
    if _ARCHIVE_LINK_RE.search(text):
        return None
    if re.search(r"(?i)\b(?:practice|versus|friendly|drill|custom)\b", text):
        return None

    day = _parse_maptap_date(text, reference_day)
    if day is None:
        return None
    score_match = re.search(r"(?i)\b(?:final\s+score|score)\s*:\s*(\d{1,4})\b", text)
    if not score_match:
        ratio = re.search(r"(?i)\b(\d{1,4})\s*/\s*1000\b", text)
        score_match = ratio
    if not score_match:
        return None
    score_value = int(score_match.group(1))
    return ParsedScore(
        game="MapTap",
        puzzle_id=day.toordinal(),
        metric_type="points",
        metric_value=score_value,
        display=f"{score_value} pts",
        score_day=day.isoformat() if day else None,
    )


# ---------------------------------------------------------------------------
# Tiebreak parsing
# ---------------------------------------------------------------------------
def _parse_tiebreak(game: str, text: str) -> Optional[int]:
    """Extract a tiebreak value from score text for supported games.

    Lower is better.  For the flawless_or_backtracks strategy (Zip):
      0 = flawless (no hints, no backtracks)
      1 = no backtracks but not flawless (used hints)
      N+1 = N backtracks

    For flawless-only games: 0 = flawless, None = not flawless.
    For redraws games: 0 = no redraws, N = N redraws.
    Returns None when the game doesn't support tiebreaks or no indicator found.
    """
    strategy = _TIEBREAK_GAMES.get(game)
    if strategy is None:
        return None

    if strategy == "flawless":
        if _FLAWLESS_RE.search(text):
            return 0
        return None

    if strategy == "flawless_or_backtracks":
        is_flawless = bool(_FLAWLESS_RE.search(text))
        no_backtracks = bool(_NO_BACKTRACKS_RE.search(text))
        m = _BACKTRACKS_RE.search(text)
        if is_flawless:
            return 0
        if no_backtracks:
            return 1
        if m:
            return int(m.group(1)) + 1
        return None

    if strategy == "redraws":
        if _NO_REDRAWS_RE.search(text):
            return 0
        m = _REDRAWS_RE.search(text)
        if m:
            return int(m.group(1))
        return None

    return None


# ---------------------------------------------------------------------------
# Pinpoint "fail" concession (no shareable score card)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PinpointFail:
    """A detected 'pinpoint fail' concession.

    ``puzzle_id`` is set only when the player named an explicit "#<id>";
    otherwise it is None and the caller must resolve the day's primary puzzle.
    """
    puzzle_id: Optional[int] = None


def parse_pinpoint_fail(text: str) -> Optional[PinpointFail]:
    """Detect a bare 'pinpoint fail' message.

    Returns a PinpointFail (carrying the explicit puzzle id if one was given)
    or None if the text is not a Pinpoint concession. Purely textual: it does
    not know the day's puzzle id, so a concession without "#<id>" leaves
    ``puzzle_id`` None for the caller to fill via primary-puzzle resolution.
    """
    m = PINPOINT_FAIL_RE.match(strip_mrkdwn_wrap(text or ""))
    if not m:
        return None
    pid = int(m.group(1)) if m.group(1) else None
    return PinpointFail(puzzle_id=pid)


def make_pinpoint_fail_score(puzzle_id: int) -> ParsedScore:
    """Build the ParsedScore for a conceded Pinpoint (worst-case DNF)."""
    return ParsedScore(
        game="Pinpoint",
        puzzle_id=int(puzzle_id),
        metric_type="guesses",
        metric_value=PINPOINT_MAX_GUESSES + PINPOINT_DNF_PENALTY,
        display=f"DNF({PINPOINT_MAX_GUESSES})",
        status="failed",
    )


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------
def parse_score(
    text: str, reference_day: Optional[Union[date, str]] = None
) -> Optional[ParsedScore]:
    reference = _reference_date(reference_day)

    # Native shares are deliberately gated by the active GameRegistry entry.
    # They don't appear in _DEFAULT_GAMES, so groups opt in with /jw-update.
    for native_parser in (_parse_wordle,):
        parsed = native_parser(text)
        if parsed:
            return parsed
    for native_parser in (_parse_four_by_six, _parse_four_by_three, _parse_maptap):
        parsed = native_parser(text, reference)
        if parsed:
            return parsed

    # Generic points format for games registered with metric_type=points.
    m = game_registry.points_header_re.search(text)
    if m:
        game = normalize_game(m.group("game"))
        return ParsedScore(
            game=game,
            puzzle_id=int(m.group("puzzle_id")),
            metric_type="points",
            metric_value=int(m.group("score")),
            display=f"{m.group('score')} pts",
        )

    # Time-based games support two common post formats:
    #  1) "GameName #316 | 0:13"
    #  2) "GameName #316" then "0:13 :checkered_flag:" on the next line.
    m = game_registry.time_header_re.search(text)
    if m:
        game = normalize_game(m.group(1))
        puzzle_id = int(m.group(2))

        mm: Optional[int] = None
        ss: Optional[int] = None

        # Same-line time (after a pipe)
        if m.group(3) and m.group(4):
            mm = int(m.group(3))
            ss = int(m.group(4))
        else:
            # Next-line time (or within the next few lines)
            tail = text[m.end():]
            tail_snip = "\n".join(tail.splitlines()[:6])
            m2 = TIME_LINE_RE.search(tail_snip)
            if m2:
                mm = int(m2.group(1))
                ss = int(m2.group(2))

        if mm is not None and ss is not None:
            seconds = mm * 60 + ss
            return ParsedScore(
                game=game,
                puzzle_id=puzzle_id,
                metric_type="time",
                metric_value=seconds,
                display=f"{mm}:{ss:02d}",
                tiebreak_value=_parse_tiebreak(game, text),
            )

    # Guesses-based games (Pinpoint and any future guesses games).
    m = game_registry.guesses_header_re.search(text)
    if m:
        game = normalize_game(m.group(1))
        puzzle_id = int(m.group(2))
        header_guesses = m.group(3)

        guesses: Optional[int] = None

        if header_guesses:
            guesses = int(header_guesses)
        elif game == "Pinpoint":
            # Pinpoint-specific fallback: infer guesses from emoji lines
            lines = PINPOINT_GUESSLINE_RE.findall(text)
            if lines:
                guesses = len(lines)

        if guesses is None:
            return None  # can't score what we can't parse

        # DNF detection: Pinpoint uses :pushpin: presence to indicate solved
        dnf = False
        if game == "Pinpoint":
            dnf = ":pushpin:" not in text.lower()

        metric_value = guesses + (PINPOINT_DNF_PENALTY if dnf else 0)
        display = f"DNF({guesses})" if dnf else str(guesses)

        return ParsedScore(
            game=game,
            puzzle_id=puzzle_id,
            metric_type="guesses",
            metric_value=metric_value,
            display=display,
            status="failed" if dnf else "solved",
        )

    return None


def parse_score_for_day(text: str, day: Union[date, str]) -> Optional[ParsedScore]:
    """Parse a score using its Slack score-day as the year reference."""
    return parse_score(text, reference_day=day)
