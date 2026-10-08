#!/usr/bin/env python3
"""
nl_query.py (v3.4.1)

Natural-language queries over tracker history.

Contract:
- The LLM maps natural language to a constrained analytics function call.
- The local validator and executor compute every result from Google Sheets.
- The LLM may phrase those computed facts for Slack, but does not calculate them.
- A rule translator remains available when the model is unavailable.

Fixes:
- Define _resolve_date_range (previous NameError).
- Reasoning-model Chat fallback uses max_completion_tokens and omits unsupported temperature.
- Responses JSON mode requirement: prompts include 'json' (lowercase) when using json_object.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from awards import (
    GOLD, NECKTIE, POINTS_BY_PLACE, TROPHY, AwardTally,
    medal_scoring_start, medal_tally, render_tally, unpack_awards, uses_medal_scoring,
)
from openai_compat import (
    chat_completion_token_limit_param,
    default_reasoning_effort,
    model_uses_reasoning,
)
from score_metrics import higher_is_better, metric_sort_value, record_is_dnf

__version__ = "3.4.1"

# Optional: load .env early so this module works standalone AND when imported before the caller loads .env.
# Safe: load_dotenv() is idempotent and won't override existing environment variables by default.
try:
    from dotenv import load_dotenv  # type: ignore
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)
except Exception:
    pass

# ---------------------------
# Slack-facing helpers
# ---------------------------

# Restrict Slack NL queries to explicit prefixes (`bot:`, `bot,`, or `?`) so
# ordinary conversational questions and score posts do not trigger the query
# engine. The `?` form (with or without trailing space) was added after users
# repeatedly tried it expecting it to work.
_QUERY_PREFIX_RE = re.compile(r"^\s*(?:bot\s*[:,]|\?\s*)", re.IGNORECASE)


def is_nl_query_text(text: str) -> bool:
    return bool(_QUERY_PREFIX_RE.match(text or ""))


def strip_nl_query_prefix(text: str) -> str:
    t = (text or "").strip()
    m = _QUERY_PREFIX_RE.match(t)
    if not m:
        return t
    return t[m.end() :].strip()


# ---------------------------
# Env + OpenAI transport
# ---------------------------

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_OUT = 350

_LAST_OPENAI_ERROR: Optional[str] = None


def last_openai_error() -> str:
    return _LAST_OPENAI_ERROR or ""


def _set_last_openai_error(msg: Optional[str]) -> None:
    global _LAST_OPENAI_ERROR
    _LAST_OPENAI_ERROR = msg


def _env_model() -> str:
    # score-bot imports nl_query before dotenv is loaded; read env at call time.
    m = (os.environ.get("OPENAI_NL_MODEL") or os.environ.get("OPENAI_MODEL") or "").strip()
    return m or DEFAULT_MODEL


def _env_timeout_s() -> float:
    try:
        v = float((os.environ.get("OPENAI_TIMEOUT_S") or str(DEFAULT_TIMEOUT_S)).strip())
        return v if v > 0 else DEFAULT_TIMEOUT_S
    except Exception:
        return DEFAULT_TIMEOUT_S


def _env_reasoning_effort(model: str) -> str:
    eff = (os.environ.get("OPENAI_REASONING_EFFORT") or "").strip().lower()
    return eff or default_reasoning_effort(model) or ""


def _model_supports_reasoning(model: str) -> bool:
    return model_uses_reasoning(model)


def _normalize_openai_base_url(base: str) -> str:
    b = (base or "").strip().rstrip("/")
    if not b:
        b = "https://api.openai.com/v1"
    if b.startswith("https://api.openai.com") and not b.endswith("/v1"):
        b = b + "/v1"
    return b


def _api_mode() -> str:
    # responses | chat | auto
    m = (os.environ.get("OPENAI_API_MODE") or "").strip().lower()
    if m in ("responses", "chat", "auto"):
        return m
    return "auto"


def _openai_url(mode: str) -> str:
    base = _normalize_openai_base_url(os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1")
    if mode == "chat":
        return f"{base}/chat/completions"
    return f"{base}/responses"


def _extract_json_obj(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    # remove code fences
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()

    i = t.find("{")
    j = t.rfind("}")
    if i < 0 or j < 0 or j <= i:
        return None
    blob = t[i : j + 1]
    try:
        obj = json.loads(blob)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_openai_error(http_err: urllib.error.HTTPError) -> str:
    body = ""
    try:
        body = http_err.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""

    msg = f"HTTP {getattr(http_err, 'code', '???')}"
    req_id = ""
    try:
        req_id = http_err.headers.get("x-request-id", "") or ""
    except Exception:
        req_id = ""

    err_code = ""
    try:
        j = json.loads(body) if body else {}
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or "").strip()
            if detail:
                msg += f": {detail}"
            err_type = str(err.get("type") or "").strip()
            err_code = str(err.get("code") or "").strip()
            p = str(err.get("param") or "").strip()
            extra = []
            if err_type:
                extra.append(f"type={err_type}")
            if err_code:
                extra.append(f"code={err_code}")
            if p:
                extra.append(f"param={p}")
            if extra:
                msg += " (" + ", ".join(extra) + ")"
    except Exception:
        if body:
            msg += f": {body[:200]}"

    if req_id:
        msg += f" (request_id={req_id})"
    return msg


def _call_openai_translate(mode: str, payload: Dict[str, Any], timeout_s: float, api_key: str) -> Optional[Dict[str, Any]]:
    url = _openai_url(mode)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    raw = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        _set_last_openai_error(_parse_openai_error(e))
        return None
    except Exception as e:
        _set_last_openai_error(str(e))
        return None

    try:
        data = json.loads(raw)
    except Exception:
        _set_last_openai_error("OpenAI returned non-JSON response envelope")
        return None

    text_out = ""
    if mode == "responses":
        text_out = str(data.get("output_text") or "").strip()
        if not text_out:
            out = data.get("output") or []
            parts: List[str] = []
            if isinstance(out, list):
                for item in out:
                    if not isinstance(item, dict):
                        continue
                    for c in item.get("content") or []:
                        if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                            parts.append(str(c.get("text") or ""))
            text_out = "\n".join(parts).strip()
    else:
        choices = data.get("choices") or []
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") or {}
            text_out = str(msg.get("content") or "").strip()

    if not text_out:
        _set_last_openai_error("OpenAI returned no text output")
        return None

    obj = _extract_json_obj(text_out)
    if not obj:
        _set_last_openai_error("Could not parse json object from model output")
        return None

    _set_last_openai_error(None)
    return obj


def _call_openai_envelope(mode: str, payload: Dict[str, Any], timeout_s: float, api_key: str) -> Optional[Dict[str, Any]]:
    """Send a request and return the decoded API envelope for tool/text flows."""
    url = _openai_url(mode)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        _set_last_openai_error(_parse_openai_error(e))
        return None
    except Exception as e:
        _set_last_openai_error(str(e))
        return None
    try:
        data = json.loads(raw)
    except Exception:
        _set_last_openai_error("OpenAI returned non-JSON response envelope")
        return None
    if not isinstance(data, dict):
        _set_last_openai_error("OpenAI returned an invalid response envelope")
        return None
    _set_last_openai_error(None)
    return data


def _openai_text_from_envelope(mode: str, data: Dict[str, Any]) -> str:
    if mode == "responses":
        text_out = str(data.get("output_text") or "").strip()
        if text_out:
            return text_out
        parts: List[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in ("output_text", "text"):
                    parts.append(str(content.get("text") or ""))
        return "\n".join(parts).strip()
    choices = data.get("choices") or []
    if choices and isinstance(choices, list):
        return str(((choices[0].get("message") or {}).get("content")) or "").strip()
    return ""


# ---------------------------
# Query spec + date ranges
# ---------------------------

@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive


STATS_QUERY_VERSION = "stats_query_v1"


@dataclass(frozen=True)
class ScoreFact:
    day: str
    user_id: str
    game: str
    metric_type: str
    metric_value: int
    display: str
    rank: int
    players: int
    status: str = ""


@dataclass(frozen=True)
class DailyUserFact:
    day: str
    user_id: str
    game_days_played: int
    tally: AwardTally
    finalized: bool

    @property
    def wins(self) -> int:
        """First-place finishes that day: trophies, or gold medals from the cut-over on."""
        return self.tally.firsts

    @property
    def ties(self) -> int:
        """Games tied for first on a legacy day. Medal days have no separate tie award."""
        return self.tally.ties

    @property
    def awards(self) -> int:
        return self.wins + self.ties

    @property
    def strikeout(self) -> bool:
        return self.finalized and self.game_days_played > 0 and self.wins == 0 and self.ties == 0


@dataclass(frozen=True)
class GameAwardFact:
    day: str
    game: str
    user_id: str
    tally: AwardTally

    @property
    def wins(self) -> int:
        return self.tally.firsts

    @property
    def ties(self) -> int:
        return self.tally.ties


@dataclass(frozen=True)
class StatsFacts:
    scores: List[ScoreFact]
    daily_users: List[DailyUserFact]
    game_awards: List[GameAwardFact]
    payloads: Dict[str, Dict[str, Any]]
    rows_scanned: int

    @property
    def result_scores(self) -> List[ScoreFact]:
        """Completed results only; ``scores`` also retains failed submissions for activity."""
        return [fact for fact in self.scores if not record_is_dnf(fact)]


def _parse_day_key(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _award_definitions() -> str:
    """What the awards mean, for the translator prompts. Two eras, split at the medal cut-over."""
    start = medal_scoring_start()
    first, second, third = (POINTS_BY_PLACE[p] for p in (1, 2, 3))
    return (
        f"Before {start}, a game's winner got a trophy and a tie for first gave each tied player "
        "a necktie: wins are trophies, ties are neckties. "
        f"From {start} on, each game awards gold, silver and bronze medals worth {first}/{second}/{third} points "
        "for 1st/2nd/3rd place; tied players share a place and there is no separate tie award, so a win is a "
        "first-place (gold) finish and points are what the standings rank."
    )


def _tie_rules_text() -> str:
    """The answer to 'how are ties decided', covering both scoring eras."""
    start = medal_scoring_start()
    first, second, third = (POINTS_BY_PLACE[p] for p in (1, 2, 3))
    return (
        "*Tie rules*\n"
        f"From {start}: lower scores win. Each game awards {first}/{second}/{third} points (gold, silver, bronze) "
        "for 1st/2nd/3rd place. Players with the same result share a place and the places they occupy are skipped, "
        f"so two players tied for first each get {first} points and the next player is third with {third}. "
        "Before awarding the shared place, the lower tiebreak value wins when every tied player has one. "
        "If any tied player is missing tiebreak data, the tie stands and the tied players share the place. "
        "A conceded puzzle (a Pinpoint DNF) can't take a medal.\n"
        f"Before {start}: lower scores win. If players tie on the best score, the lower tiebreak value wins "
        "when every tied player has one; otherwise each tied player receives a necktie."
    )


def _resolve_date_range(spec: Dict[str, Any], *, today: date) -> DateRange:
    """
    Supported presets include calendar and trailing ranges, plus today/yesterday.

    - this_week / last_week are Monday..Sunday (calendar weeks).
    - this_month / last_month are calendar months.
    - last_7_days / last_30_days are trailing windows ending today.

    Optional explicit overrides:
      date_range.start, date_range.end (YYYY-MM-DD)
    """
    filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
    dr = spec.get("date_range") or filters.get("date_range") or {}
    if not isinstance(dr, dict):
        dr = {}

    preset = str(dr.get("preset") or "all_time").strip().lower()
    start_s = str(dr.get("start") or "").strip()
    end_s = str(dr.get("end") or "").strip()

    # Explicit overrides
    start_d = _parse_day_key(start_s) if start_s else None
    end_d = _parse_day_key(end_s) if end_s else None

    # Normalize synonyms
    if preset in ("", "all", "alltime", "all_time"):
        preset = "all_time"
    if preset in ("last7", "last_7", "past_7_days", "trailing_7d", "last_7_days"):
        preset = "last_7_days"
    if preset in ("last30", "last_30", "past_30_days", "trailing_30d", "last_30_days"):
        preset = "last_30_days"
    if preset in ("thisweek", "this_week"):
        preset = "this_week"
    if preset in ("lastweek", "last_week", "previous_week", "prior_week"):
        preset = "last_week"
    if preset in ("thismonth", "this_month"):
        preset = "this_month"
    if preset in ("lastmonth", "last_month", "previous_month", "prior_month"):
        preset = "last_month"
    if preset in ("thisyear", "this_year"):
        preset = "this_year"

    if preset == "today":
        start = end = today
    elif preset == "yesterday":
        start = end = today - timedelta(days=1)
    elif preset == "this_month":
        start = today.replace(day=1)
        if start.month == 12:
            next_m = date(start.year + 1, 1, 1)
        else:
            next_m = date(start.year, start.month + 1, 1)
        end = next_m - timedelta(days=1)
    elif preset == "last_month":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
    elif preset == "this_week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif preset == "last_week":
        start_this = today - timedelta(days=today.weekday())
        start = start_this - timedelta(days=7)
        end = start + timedelta(days=6)
    elif preset == "last_7_days":
        end = today
        start = today - timedelta(days=6)
    elif preset == "last_30_days":
        end = today
        start = today - timedelta(days=29)
    elif preset == "this_year":
        start = date(today.year, 1, 1)
        end = date(today.year, 12, 31)
    else:  # all_time
        start = date(1970, 1, 1)
        end = today

    if start_d:
        start = start_d
    if end_d:
        end = end_d

    if end < start:
        start, end = end, start

    return DateRange(start=start, end=end)



# ---------------------------
# Deterministic translator (rule-based)
# ---------------------------

def _find_game_in_text(question: str, games: List[str]) -> str:
    q = (question or "").lower().replace("×", "x")
    for g in sorted(games, key=lambda s: len(s), reverse=True):
        gl = g.lower().replace("×", "x")
        if re.search(rf"\b{re.escape(gl)}\b", q, flags=re.IGNORECASE):
            return g
        if gl in q:
            return g
    return ""


_NUMWORDS: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fourteen": 14, "thirty": 30,
    "sixty": 60, "ninety": 90,
}

_NUMWORD_PATTERN = "|".join(_NUMWORDS.keys())

_RELATIVE_WINDOW_RE = re.compile(
    r"\b(?:over\s+(?:the\s+)?|in\s+the\s+)?(?:last|past|trailing)\s+"
    rf"(\d+|{_NUMWORD_PATTERN})\s+"
    r"(day|days|week|weeks|month|months)\b",
    re.IGNORECASE,
)

_EXPLICIT_RANGE_RE = re.compile(
    r"\b(?:(?:range|between|from)\s+)?(\d{4}-\d{2}-\d{2})\s+(?:to|and|through|-)\s+(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

_MONTH_NUMBERS = {
    name.lower(): i
    for i, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        1,
    )
}
_MONTH_NUMBERS.update({name[:3]: value for name, value in list(_MONTH_NUMBERS.items()) if len(name) > 3})

_WEEKDAY_NUMBERS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _weekdays_from_text(q: str) -> List[int]:
    text = (q or "").lower()
    return [n for name, n in _WEEKDAY_NUMBERS.items() if re.search(rf"\b{ name }s?\b", text)]


def _infer_window_from_text(q: str, today: date) -> Tuple[str, str, str]:
    """
    Returns (preset, start_iso, end_iso). preset is 'all_time' when explicit
    start/end are populated; callers should pass explicit dates through unchanged
    and let _resolve_date_range honor the override.
    """
    t = (q or "").lower()

    m = _EXPLICIT_RANGE_RE.search(t)
    if m:
        return ("all_time", m.group(1), m.group(2))

    m = _RELATIVE_WINDOW_RE.search(t)
    if m:
        n_raw = m.group(1)
        n = int(n_raw) if n_raw.isdigit() else _NUMWORDS[n_raw.lower()]
        unit = m.group(2).lower()
        if unit.startswith("day"):
            days = n
        elif unit.startswith("week"):
            days = n * 7
        else:
            days = n * 30
        end = today
        start = today - timedelta(days=max(days - 1, 0))
        return ("all_time", start.isoformat(), end.isoformat())

    if re.search(r"\btoday\b|\btoday['’]s\b", t):
        return ("all_time", today.isoformat(), today.isoformat())
    if re.search(r"\byesterday\b|\byesterday['’]s\b", t):
        yesterday = today - timedelta(days=1)
        return ("all_time", yesterday.isoformat(), yesterday.isoformat())

    month_match = re.search(
        r"\b(?:in|during|for)\s+(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|december|dec)(?:\s+(\d{4}))?\b|\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|december|dec)\s+(\d{4})\b",
        t,
    )
    if month_match:
        month_name = month_match.group(1) or month_match.group(3)
        explicit_year = month_match.group(2) or month_match.group(4)
        month_num = _MONTH_NUMBERS[month_name]
        year = int(explicit_year) if explicit_year else today.year - (month_num > today.month)
        start = date(year, month_num, 1)
        if month_num == 12:
            end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(year, month_num + 1, 1) - timedelta(days=1)
        return ("all_time", start.isoformat(), end.isoformat())

    if re.search(r"\blast\s+month\b|\bprevious\s+month\b|\bprior\s+month\b", t):
        return ("last_month", "", "")
    if re.search(r"\bthis\s+month\b|\bcurrent\s+month\b", t):
        return ("this_month", "", "")
    if re.search(r"\blast\s+week\b|\bprevious\s+week\b|\bprior\s+week\b", t):
        return ("last_week", "", "")
    if re.search(r"\bthis\s+week\b|\bcurrent\s+week\b", t):
        return ("this_week", "", "")
    if re.search(r"\bthis\s+year\b|\bcurrent\s+year\b", t):
        return ("this_year", "", "")
    return ("all_time", "", "")


def _infer_preset_from_text(q: str) -> str:
    """Backwards-compatible wrapper used by tests/legacy callers."""
    preset, _start, _end = _infer_window_from_text(q, date.today())
    return preset


def _dr(preset: str, start: str, end: str, *, default_when_all_time: Optional[str] = None) -> Dict[str, str]:
    """
    Build a date_range dict. If preset is 'all_time' and a default is supplied,
    use the default preset instead (preserves legacy behavior of intents like
    all_games_awards_leaderboard which default to last_month when nothing is said).
    Explicit start/end always take precedence in _resolve_date_range.
    """
    if preset == "all_time" and default_when_all_time and not (start or end):
        preset = default_when_all_time
    return {"preset": preset, "start": start or "", "end": end or ""}


def _unsupported_spec(reason: str = "") -> Dict[str, Any]:
    return {
        "schema_version": STATS_QUERY_VERSION,
        "subject": "",
        "user": "",
        "game": "",
        "measure": "unsupported",
        "aggregation": "",
        "date_range": _dr("all_time", "", ""),
        "filters": {"date_range": _dr("all_time", "", "")},
        "group_by": "",
        "output": "unsupported",
        "limit": 5,
        "unsupported_reason": reason or "unsupported",
    }


def _stats_spec(
    *,
    subject: str = "user",
    user: str = "me",
    game: str = "",
    measure: str,
    aggregation: str,
    date_range: Optional[Dict[str, str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    group_by: str = "",
    output: str = "scalar",
    limit: int = 5,
    stat: str = "",
) -> Dict[str, Any]:
    f = dict(filters or {})
    if date_range is not None:
        f["date_range"] = date_range
    else:
        f.setdefault("date_range", _dr("all_time", "", ""))
    if user:
        f.setdefault("user", user)
    if game:
        f.setdefault("game", game)
    spec = {
        "schema_version": STATS_QUERY_VERSION,
        "subject": subject,
        "user": user or "",
        "game": game or "",
        "measure": measure,
        "aggregation": aggregation,
        "date_range": f["date_range"],
        "filters": f,
        "group_by": group_by or "",
        "output": output,
        "limit": limit,
    }
    if stat:
        # Compatibility field for legacy logging/tests; score_value aggregation is authoritative.
        spec["stat"] = stat
    return spec


_FIRST_PERSON_RE = re.compile(r"\b(?:i|me|my|i['’]m|i\s+am)\b", re.IGNORECASE)

_NAMED_TARGET_RE = re.compile(
    r"\b(?:did|does|has|had)\s+@?([A-Za-z][A-Za-z0-9_.-]*)\s+"
    r"(?=(?:play|played|complete|completed|do|did|win|wins|won|lose|lost|score|scored)\b)",
    re.IGNORECASE,
)


def _extract_named_target(question: str) -> str:
    match = _NAMED_TARGET_RE.search(question or "")
    if not match:
        return ""
    name = match.group(1).strip(" .,'\"!?;:")
    if name.lower() in {"i", "me", "you", "we", "they", "he", "she"}:
        return ""
    return name


def _resolve_named_target(question: str, resolver: Optional[Callable[[str], str]]) -> Tuple[str, str]:
    name = _extract_named_target(question)
    if not name:
        return "", ""
    uid = ""
    if resolver:
        try:
            uid = str(resolver(name) or "").strip()
        except Exception:
            uid = ""
    return uid, name


def _resolve_target_user(question: str) -> str:
    """
    Returns the target user the question is asking about, for user-centric intents.
    Precedence: an explicit Slack <@mention> beats first-person markers, because if
    the user typed a mention they meant that specific person. Multiple mentions
    return ''  (ambiguous — fall through to LLM).
    """
    mentions = _extract_mentioned_user_ids(question or "")
    if len(mentions) == 1:
        return mentions[0]
    if mentions:  # 2+ — ambiguous
        return ""
    if _FIRST_PERSON_RE.search(question or ""):
        return "me"
    return ""


def _rule_based_translate(
    question: str,
    games: List[str],
    *,
    today: Optional[date] = None,
    resolve_user_name: Optional[Callable[[str], str]] = None,
) -> Optional[Dict[str, Any]]:
    q = (question or "").strip()
    if not q:
        return None

    preset, win_start, win_end = _infer_window_from_text(q, today or date.today())

    # Resolved target user for user-centric intents. '' for leaderboard / consistency.
    target_user = _resolve_target_user(q)
    named_uid, named_name = _resolve_named_target(q, resolve_user_name)
    if named_name:
        if not named_uid:
            return _unsupported_spec(f"unresolved_user:{named_name}")
        target_user = named_uid
    dr = _dr(preset, win_start, win_end)

    # Static rules that can be answered directly without model translation.
    if re.search(r"\bhow\s+are\s+ties?\s+decided\b|\btie[- ]breaking\s+rules?\b", q, re.IGNORECASE):
        return _stats_spec(subject="", user="", measure="tie_rules", aggregation="summary", date_range=dr, output="summary")

    if re.search(r"\bclean\s+sweep\b", q, re.IGNORECASE):
        return _stats_spec(subject="all_users", user="", measure="clean_sweep", aggregation="summary", date_range=dr, output="summary")

    if (
        re.search(r"\bhow\s+many\s+months?\b", q, re.IGNORECASE)
        and re.search(r"\b(?:win|won|champion|monthly\s+totals?)\b", q, re.IGNORECASE)
        and target_user
    ):
        return _stats_spec(subject="user", user=target_user, measure="monthly_titles", aggregation="count", date_range=dr, output="scalar")

    # The MVP uses outright trophy count first; the daily record is the maximum
    # trophy total by one player on any finalized day.
    if re.search(r"\b(?:daily|single[- ]day|in\s+one\s+day|per\s+day)\b", q, re.IGNORECASE) and re.search(
        r"\b(?:record|most|highest|maximum|mvp|troph(?:y|ies)|wins?|won)\b", q, re.IGNORECASE
    ):
        return _stats_spec(subject="all_users", user="", measure="wins", aggregation="max", date_range=dr, group_by="day", output="scalar")

    if re.search(r"\b(?:report|recap)\b", q, re.IGNORECASE) and re.search(r"\b(?:games?|today|yesterday)\b", q, re.IGNORECASE):
        return _stats_spec(subject="all_users", user="", measure="daily_report", aggregation="summary", date_range=dr, output="summary")

    weekdays = _weekdays_from_text(q)
    if weekdays and re.search(r"\b(?:win|wins|won|winners?)\b", q, re.IGNORECASE) and (
        re.search(r"\bwho\b", q, re.IGNORECASE) or re.search(r"\bmost\b", q, re.IGNORECASE)
    ):
        multi_weekday = len(weekdays) > 1 or re.search(r"\b(?:break\s+down|by\s+day\s+of\s+the\s+week)\b", q, re.IGNORECASE)
        return _stats_spec(
            subject="all_users", user="", measure="wins", aggregation="leaderboard", date_range=dr,
            filters={"weekdays": weekdays}, group_by="weekday" if multi_weekday else "user",
            output="leaderboard", limit=5,
        )

    if re.search(r"\bwho\b", q, re.IGNORECASE) and re.search(r"\b(?:has\s+played|played)\b", q, re.IGNORECASE) and re.search(r"\bgames?\b", q, re.IGNORECASE):
        return _stats_spec(
            subject="all_users", user="", measure="game_days_played", aggregation="leaderboard",
            date_range=dr, group_by="user", output="leaderboard", limit=10,
        )

    # ---- General stat layer: broad questions over the derived analytics model ----
    if re.search(r"\b(?:struck\s+out|strikeouts?|skunked)\b", q, re.IGNORECASE):
        filters: Dict[str, Any] = {"wins_eq": 0, "ties_eq": 0}
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            measure="strikeouts",
            aggregation="count",
            date_range=dr,
            filters=filters,
            output="scalar",
        )

    if (
        re.search(r"\bhow\s+many\b", q, re.IGNORECASE)
        and re.search(r"\bgames?\b", q, re.IGNORECASE)
        and re.search(r"\b(?:play|played|do|done|complete|completed)\b", q, re.IGNORECASE)
    ):
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            game=_find_game_in_text(q, games),
            measure="game_days_played",
            aggregation="count",
            date_range=dr,
            output="scalar",
        )

    if re.search(r"\bwin\s+record\b|\brecord\s+of\s+wins\b|\bmy\s+record\b", q, re.IGNORECASE):
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            game=_find_game_in_text(q, games),
            measure="awards",
            aggregation="summary",
            date_range=dr,
            output="summary",
        )

    if re.search(r"\bhow\s+many\b", q, re.IGNORECASE) and re.search(r"\bties?\b|\bneckties?\b", q, re.IGNORECASE):
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            game=_find_game_in_text(q, games),
            measure="ties",
            aggregation="sum",
            date_range=dr,
            output="scalar",
        )

    # ---- Synonym layer: natural phrasings that map to user_game_awards ----
    # "how'd I do in patches", "how did <@user> do in patches the last 14 days",
    # "what are my results in Patches", "<@user>'s win frequency on patches",
    # "how am I doing in patches", "my performance in Patches".
    # NB: '\b' doesn't fire before '<' (both non-word), so mention-prefixed patterns
    # use '(?:^|\s)' to anchor at start-of-string or whitespace instead.
    if (
        re.search(
            r"\bhow(?:\s+(?:did|are|is|am)|['’]?d|['’]?s)\s+\S+\s+(?:do(?:ing)?|done|perform(?:ing|ed)?)\b",
            q,
            re.IGNORECASE,
        )
        or re.search(
            r"(?:^|\s)(?:my|<@[A-Z0-9]+>(?:\|[^>]+)?['’]?s?)\s+(?:results?|performance)\b",
            q,
            re.IGNORECASE,
        )
        or re.search(r"\bwhat\s+(?:are|is|was)\s+\S+\s+(?:results?|performance)\b", q, re.IGNORECASE)
        or re.search(r"\bwin(?:s)?\s+frequency\b", q, re.IGNORECASE)
    ):
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="user",
                user=target_user or "me",
                game=g,
                measure="awards",
                aggregation="summary",
                date_range=dr,
                output="summary",
            )

    # "Who wins Zip most often?"
    if re.search(r"\bwho\s+wins?\b", q, re.IGNORECASE) and re.search(r"\bmost\s+often\b", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="all_users",
                user="",
                game=g,
                measure="wins",
                aggregation="leaderboard",
                date_range=dr,
                group_by="user",
                output="leaderboard",
                limit=5,
            )

    # "Who's the all time leader for zip?" / "Most Zip wins?"
    if re.search(r"\bmost\b", q, re.IGNORECASE) and re.search(r"\bwins?\b", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="all_users",
                user="",
                game=g,
                measure="wins",
                aggregation="leaderboard",
                date_range=dr,
                group_by="user",
                output="leaderboard",
                limit=5,
            )

    # "Who won the most trophies last month?" / "Who has the most points?"
    # Points and medals only exist from the scoring cut-over, so without a window
    # they mean this month and rank by points (measure=awards); trophies keep their
    # long-standing last-month default and rank by wins.
    if (
        re.search(r"\bmost\s+(trophies|awards|points|medals)\b", q, re.IGNORECASE)
        or re.search(r"\b(?:awards|points|medals)\s+leaderboard\b", q, re.IGNORECASE)
    ):
        about_points = bool(re.search(r"\b(?:points|medals)\b", q, re.IGNORECASE))
        return _stats_spec(
            subject="all_users",
            user="",
            game=_find_game_in_text(q, games),
            measure="awards" if about_points else "wins",
            aggregation="leaderboard",
            date_range=_dr(
                preset, win_start, win_end,
                default_when_all_time="this_month" if about_points else "last_month",
            ),
            group_by="user",
            output="leaderboard",
            limit=5,
        )

    # "How many points do I have?" / "How many medals does <@user> have?"
    if re.search(r"\bhow\s+many\s+(?:points|medals)\b", q, re.IGNORECASE) and (target_user or _FIRST_PERSON_RE.search(q)):
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            game=_find_game_in_text(q, games),
            measure="awards",
            aggregation="summary",
            date_range=dr,
            output="summary",
        )

    # "How many Zip games did I win last week?" / "How many Zip wins for <@user>?"
    if re.search(r"\bhow\s+many\b", q, re.IGNORECASE) and re.search(r"\bwins?\b", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="user",
                user=target_user or "me",
                game=g,
                measure="wins",
                aggregation="sum",
                date_range=dr,
                output="scalar",
            )
        if target_user or _FIRST_PERSON_RE.search(q):
            return _stats_spec(
                subject="user",
                user=target_user or "me",
                measure="wins",
                aggregation="sum",
                date_range=dr,
                output="scalar",
            )

    # "What's my median Tango time this month?" / "What's <@user>'s median Tango time?"
    m = re.search(
        r"(?:^|\s)(?:my|<@[A-Z0-9]+>(?:\|[^>]+)?['’]?s?)\s+(median|mean|average|min|max|count)\b",
        q,
        re.IGNORECASE,
    )
    if m:
        stat = m.group(1).lower()
        stat = "mean" if stat == "average" else stat
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="user",
                user=target_user or "me",
                game=g,
                measure="score_value",
                aggregation=stat,
                date_range=dr,
                output="scalar",
                stat=stat,
            )

    # "Show my best week." / "Show <@user>'s best week."
    if re.search(r"\bbest\s+week\b", q, re.IGNORECASE) and target_user:
        return _stats_spec(
            subject="user",
            user=target_user,
            measure="awards",
            aggregation="max",
            date_range=dr,
            group_by="week",
            output="breakdown",
        )

    # "What are my best/worst scores?" / "<@user>'s best scores for Pinpoint?"
    if re.search(
        r"(?:^|\s)(?:my|<@[A-Z0-9]+>(?:\|[^>]+)?['’]?s?)\s+(?:best|worst)\s+scores?\b",
        q,
        re.IGNORECASE,
    ) or re.search(
        r"\bwhat\s+are\s+(?:my|<@[A-Z0-9]+>(?:\|[^>]+)?['’]?s?)\s+(?:best|worst)\s+scores?\b",
        q,
        re.IGNORECASE,
    ):
        requested_stat = "worst" if re.search(r"\bworst\b", q, re.IGNORECASE) else "best"
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="user",
                user=target_user or "me",
                game=g,
                measure="score_value",
                aggregation=requested_stat,
                date_range=dr,
                output="scalar",
                stat=requested_stat,
            )
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            measure="score_value",
            aggregation=requested_stat,
            date_range=dr,
            group_by="game",
            output="breakdown",
            stat=requested_stat,
        )

    # "All-time fastest scores for each game" / "All-time fastest scores for all games"
    if (re.search(r"\b(all[-\s]*time|ever)\b", q, re.IGNORECASE)
        and re.search(r"\b(fastest|best|worst|slowest)\b", q, re.IGNORECASE)
        and re.search(r"\b(each|all)\s+games?\b", q, re.IGNORECASE)):
        requested_stat = "worst" if re.search(r"\b(worst|slowest)\b", q, re.IGNORECASE) else "best"
        return _stats_spec(
            subject="all_users",
            user="",
            measure="score_value",
            aggregation=requested_stat,
            date_range=dr,
            group_by="game",
            output="breakdown",
            stat=requested_stat,
        )

    # "Fastest time for Zip" / "Best score ever for Zip" / "Zip record" / "<@user>'s fastest Zip"
    if re.search(r"\b(fastest|best|record|worst|slowest)\b", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        if g and re.search(r"\b(time|score|ever|all[-\s]*time|record)\b", q, re.IGNORECASE):
            requested_stat = "worst" if re.search(r"\b(worst|slowest)\b", q, re.IGNORECASE) else "best"
            if target_user:
                return _stats_spec(
                    subject="user",
                    user=target_user,
                    game=g,
                    measure="score_value",
                    aggregation=requested_stat,
                    date_range=dr,
                    output="scalar",
                    stat=requested_stat,
                )
            return _stats_spec(
                subject="all_users",
                user="",
                game=g,
                measure="score_value",
                aggregation=requested_stat,
                date_range=dr,
                group_by="user",
                output="leaderboard",
                limit=5,
                stat=requested_stat,
            )

    # "Show me a distribution curve for my Zip performance" / "<@user>'s Zip distribution"
    if re.search(r"\bdistribution\b|\bhistogram\b|\bcurve\b", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        if g:
            return _stats_spec(
                subject="user",
                user=target_user or "me",
                game=g,
                measure="score_value",
                aggregation="distribution",
                date_range=dr,
                output="distribution",
            )

    # "placement distribution" / "my finishes for Zip" / "<@user>'s placement breakdown"
    if re.search(r"\bplacement\b|\bfinish(?:es|ing)?\s+(?:distribution|breakdown|position)", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            game=g,
            measure="placement",
            aggregation="distribution",
            date_range=dr,
            output="distribution",
        )

    # "consistency" / "who's most consistent?" / "<@user>'s consistency"
    if re.search(r"\bconsisten\w*\b|\breliab\w*\b|\bvolatil\w*\b|\bplays?\s+it\s+safe\b|\bstandard\s+deviation\b|\bstdev\b|\btop.half\b", q, re.IGNORECASE):
        g = _find_game_in_text(q, games)
        # If asking about a specific user's consistency, treat as placement_distribution
        if target_user:
            return _stats_spec(
                subject="user",
                user=target_user,
                game=g,
                measure="placement",
                aggregation="distribution",
                date_range=dr,
                output="distribution",
            )
        return _stats_spec(
            subject="all_users",
            user="",
            game=g,
            measure="placement",
            aggregation="leaderboard",
            date_range=dr,
            group_by="user",
            output="leaderboard",
        )

    # "all-time stats" / "full stats" / "player stats" / "stats overview"
    if re.search(r"\ball[- ]?time\s+stats\b|\bfull\s+stats\b|\bplayer\s+stats\b|\bstats\s+overview\b", q, re.IGNORECASE):
        return _stats_spec(
            subject="user",
            user=target_user or "me",
            measure="awards",
            aggregation="summary",
            date_range=dr,
            output="summary",
        )

    # Media requests are unsupported (avoid returning random stats).
    if re.search(r"\bgif\b|\bmeme\b|\bimage\b|\bchart\b|\bplot\b", q, re.IGNORECASE):
        return _unsupported_spec("media request")

    return None



# ---------------------------
# LLM translator (OpenAI)
# ---------------------------

def _openai_translate(
    question: str,
    games: List[str],
    today_iso: str,
    *,
    mentioned_user_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    _set_last_openai_error(None)

    api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY") or "").strip()
    if not api_key:
        _set_last_openai_error("OPENAI_API_KEY (or OPENAI_KEY) is not set")
        return None

    model = _env_model()
    timeout_s = _env_timeout_s()
    reasoning_effort = _env_reasoning_effort(model)

    # IMPORTANT: must contain 'json' in lowercase when using json_object formatting.
    instructions = (
        "Translate the user's question into a STRICT json object (lowercase 'json'). "
        "Output json only, no markdown, no commentary. "
        "Use schema_version='stats_query_v1'. If unsupported, set measure='unsupported' "
        "and include unsupported_reason."
    )

    schema_hint = {
        "schema_version": "stats_query_v1",
        "subject": "user | all_users | game",
        "user": "'me' or Slack user_id like U123ABC or empty",
        "game": "Known game name or empty",
        "measure": "game_days_played | active_days | wins | ties | losses | awards | strikeouts | score_value | placement | daily_report | clean_sweep | monthly_titles | tie_rules | unsupported",
        "aggregation": "count | sum | mean | median | min | max | best | worst | distribution | leaderboard | summary",
        "stat": "median | mean | min | max | best | worst | count | null",
        "filters": {
            "date_range": {
                "preset": "all_time | this_month | last_month | this_week | last_week | last_7_days | last_30_days | this_year",
                "start": "YYYY-MM-DD optional",
                "end": "YYYY-MM-DD optional",
            },
            "user": "optional; repeat user when present",
            "game": "optional; repeat game when present",
            "wins_eq": "optional integer",
            "ties_eq": "optional integer",
            "weekdays": "optional list of weekday numbers; Monday=0 through Sunday=6",
            "primary_only": "optional boolean, default true",
        },
        "group_by": "empty | user | game | day | weekday | week | month",
        "output": "scalar | summary | leaderboard | distribution | breakdown | unsupported",
        "limit": "integer optional for leaderboards",
        "unsupported_reason": "optional string",
    }

    allowed = ", ".join(games)
    mentions = ", ".join(mentioned_user_ids or [])
    user_prompt = (
        f"today={today_iso}. known_games=[{allowed}]. "
        f"mentioned_user_ids=[{mentions}]. "
        "Data model notes: "
        "- Scores has primary-puzzle score rows by day, user, game, metric_type, metric_value. Higher points are better; lower time and guesses are better. "
        "- For score_value best/worst requests, use stat and aggregation best/worst so metric_type controls direction. Use min/max only when the user explicitly asks for the numeric minimum/maximum. "
        f"- DailyResults has finalized day awards. {_award_definitions()} "
        "- game_days_played and active_days count primary submissions, including failed submissions; six games on one day counts as six game-days. "
        "- Numeric score stats and placements use completed results only. "
        "- strikeouts counts finalized user-days with at least one submission and zero wins and zero ties. "
        "- A failed submission is a loss on a finalized day even when that game has no recorded winner; ties are not losses. "
        "- Use measure=wins aggregation=leaderboard group_by=user output=leaderboard for 'who wins <game> most often'. "
        "- Use measure=game_days_played aggregation=count output=scalar for 'how many games have I played'. "
        "- Use measure=wins aggregation=sum output=scalar for 'how many wins do I have'. "
        "- Use measure=awards aggregation=summary output=summary for 'what is my win record' or 'all-time stats'. "
        "- Use measure=strikeouts aggregation=count with filters wins_eq=0 and ties_eq=0 for 'struck out with 0 wins and 0 ties'. "
        "- Use measure=score_value aggregation=median/min/max/mean/count for score stats like 'my median Tango time'. "
        "- Use measure=score_value aggregation=distribution output=distribution for score distributions. "
        "- Use measure=placement aggregation=distribution for a user's placement breakdown. "
        "- Use measure=placement aggregation=leaderboard subject=all_users for consistency comparisons. "
        "- Use measure=daily_report output=summary for a report about today's or yesterday's games. "
        "- Use measure=clean_sweep output=summary for a day where one player won every game with a recorded result. "
        "- Use measure=wins aggregation=max group_by=day output=scalar for the highest single-day trophy record. "
        "- Use filters.weekdays as weekday numbers where Monday=0 and Sunday=6 for weekday win leaderboards. "
        "- Use measure=monthly_titles aggregation=count for a user's recorded monthly championships. "
        "- Use measure=tie_rules output=summary for the configured score and tiebreak rules. "
        "- For relative windows like 'last N days', 'past N weeks', 'last two weeks', or explicit 'range YYYY-MM-DD to YYYY-MM-DD' / 'between YYYY-MM-DD and YYYY-MM-DD', set date_range.start and date_range.end as explicit YYYY-MM-DD using today, and leave preset='all_time'. "
        "- If the question includes a Slack mention like <@U123ABC>, set user to that id unless the question clearly asks for 'me'. "
        "Return json matching schema: " + json.dumps(schema_hint) + "\n"
        "Question: " + question
    )

    mode = _api_mode()
    modes_to_try: List[str]
    if mode == "auto":
        modes_to_try = ["responses", "chat"]
    else:
        modes_to_try = [mode]

    for m in modes_to_try:
        if m == "responses":
            payload: Dict[str, Any] = {
                "model": model,
                "instructions": instructions,
                "input": user_prompt,
                "text": {"format": {"type": "json_object"}},
                "max_output_tokens": DEFAULT_MAX_OUT,
            }
            if _model_supports_reasoning(model) and reasoning_effort:
                payload["reasoning"] = {"effort": reasoning_effort}

            obj = _call_openai_translate("responses", payload, timeout_s, api_key)
            if obj is not None:
                return obj

            # If responses endpoint not supported by a proxy, fall through to chat.
            err = (last_openai_error() or "").lower()
            if "http 404" in err or "http 405" in err:
                continue
            return None

        # chat fallback
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        payload[chat_completion_token_limit_param(model)] = DEFAULT_MAX_OUT
        if not model_uses_reasoning(model):
            payload["temperature"] = 0

        if _model_supports_reasoning(model) and reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort

        obj = _call_openai_translate("chat", payload, timeout_s, api_key)
        if obj is not None:
            return obj

        return None

    return None


def _query_stats_tool() -> Dict[str, Any]:
    """The model-facing query contract: flexible language, bounded operations."""
    string_or_null = ["string", "null"]
    integer_or_null = ["integer", "null"]
    date_range_schema = {
        "type": "object",
        "properties": {
            "preset": {"type": "string", "enum": ["all_time", "today", "yesterday", "this_week", "last_week", "last_7_days", "last_30_days", "this_month", "last_month", "this_year"]},
            "start": {"type": string_or_null},
            "end": {"type": string_or_null},
        },
        "required": ["preset", "start", "end"],
        "additionalProperties": False,
    }
    filters_schema = {
        "type": "object",
        "properties": {
            "user": {"type": string_or_null},
            "game": {"type": string_or_null},
            "wins_eq": {"type": integer_or_null},
            "ties_eq": {"type": integer_or_null},
            "weekdays": {"type": "array", "items": {"type": "integer"}},
            "primary_only": {"type": "boolean"},
            "date_range": date_range_schema,
        },
        "required": ["user", "game", "wins_eq", "ties_eq", "weekdays", "primary_only", "date_range"],
        "additionalProperties": False,
    }
    parameters = {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": [STATS_QUERY_VERSION]},
            "subject": {"type": "string", "enum": ["user", "all_users", "game", ""]},
            "user": {"type": string_or_null},
            "game": {"type": string_or_null},
            "measure": {"type": "string", "enum": sorted(_ALLOWED_MEASURES)},
            "aggregation": {"type": "string", "enum": sorted(_ALLOWED_AGGREGATIONS)},
            "date_range": date_range_schema,
            "filters": filters_schema,
            "group_by": {"type": "string", "enum": sorted(_ALLOWED_GROUP_BY)},
            "output": {"type": "string", "enum": sorted(_ALLOWED_OUTPUTS)},
            "limit": {"type": "integer"},
            "stat": {"type": string_or_null, "enum": ["median", "mean", "min", "max", "best", "worst", "count", "", None]},
            "unsupported_reason": {"type": string_or_null},
        },
        "required": [
            "schema_version", "subject", "user", "game", "measure", "aggregation", "date_range",
            "filters", "group_by", "output", "limit", "stat", "unsupported_reason",
        ],
        "additionalProperties": False,
    }
    return {
        "name": "query_puzzle_stats",
        "description": (
            "Query the puzzle tracker ledger. Choose the measure, aggregation, filters, date range, and grouping "
            "that answer the user's question. Use only this function for questions about recorded puzzle scores, "
            "participation, wins, ties, losses, awards, or standings. If a name is not a Slack ID, pass the exact "
            "name in user for local directory resolution. Return measure=unsupported only for unrelated questions "
            "or data the ledger cannot establish."
        ),
        "parameters": parameters,
        "strict": True,
    }


def _openai_plan(
    question: str,
    games: List[str],
    today_iso: str,
    *,
    mentioned_user_ids: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Ask the model to call the constrained analytics query function."""
    _set_last_openai_error(None)
    api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY") or "").strip()
    if not api_key:
        _set_last_openai_error("OPENAI_API_KEY (or OPENAI_KEY) is not set")
        return None
    model = _env_model()
    timeout_s = _env_timeout_s()
    effort = _env_reasoning_effort(model)
    tool = _query_stats_tool()
    instruction = (
        "You translate Slack questions into one query_puzzle_stats function call. Do not answer the question yourself. "
        "Use only the supplied ledger definitions and known game names. Interpret ordinary language, paraphrases, "
        "typos, relative dates, calendar dates, weekday names, comparisons, and requests for rankings. "
        f"{_award_definitions()} "
        "Score rows are puzzle submissions. Higher points are better; lower time and guesses are better. "
        "For score_value best/worst requests, use stat and aggregation best/worst so metric_type controls direction. Use min/max only when the user explicitly asks for the numeric minimum/maximum. "
        "Awards are finalized only in DailyResults. game_days_played and active_days count primary submissions, including failures; numeric score stats and placements exclude failed results. "
        "For a player's losses, count finalized game submissions where another player won; failed submissions on finalized days are losses even when there is no recorded winner. Ties are not losses. "
        "For daily records, group wins by day and compare the largest value. Use all_users for 'who' leaderboards. "
        "Resolve 'me' from the asker at execution time. Preserve Slack IDs from mentions. If the question is ambiguous, "
        "prefer a plan that answers the clear part. If a missing detail prevents a useful query, set measure=unsupported "
        "and put one concise clarifying question in unsupported_reason. For unrelated questions or unsupported data, "
        "use a short explanation there instead."
    )
    user_input = (
        f"Today is {today_iso}. Known games: {', '.join(games)}. "
        f"Slack user mentions in the question: {', '.join(mentioned_user_ids or []) or '(none)'}.\n"
        f"Question: {question}"
    )
    requested = _api_mode()
    modes = ["responses", "chat"] if requested == "auto" else [requested]
    for mode in modes:
        if mode == "responses":
            payload: Dict[str, Any] = {
                "model": model,
                "instructions": instruction,
                "input": user_input,
                "tools": [{"type": "function", **tool}],
                "tool_choice": {"type": "function", "name": tool["name"]},
                "parallel_tool_calls": False,
                "max_output_tokens": DEFAULT_MAX_OUT,
            }
            if model_uses_reasoning(model) and effort:
                payload["reasoning"] = {"effort": effort}
        else:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": user_input},
                ],
                "tools": [{
                    "type": "function",
                    "function": {key: value for key, value in tool.items() if key != "name"} | {"name": tool["name"]},
                }],
                "tool_choice": "required",
                "parallel_tool_calls": False,
            }
            payload[chat_completion_token_limit_param(model)] = DEFAULT_MAX_OUT
            if not model_uses_reasoning(model):
                payload["temperature"] = 0
            if model_uses_reasoning(model) and effort:
                payload["reasoning_effort"] = effort

        data = _call_openai_envelope(mode, payload, timeout_s, api_key)
        if data is None:
            err = (last_openai_error() or "").lower()
            if mode == "responses" and requested == "auto" and ("http 404" in err or "http 405" in err):
                continue
            return None

        if mode == "responses":
            calls = [item for item in data.get("output") or [] if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == tool["name"]]
            arguments = calls[0].get("arguments") if calls else None
        else:
            choices = data.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            calls = message.get("tool_calls") or []
            call = next((c for c in calls if (c.get("function") or {}).get("name") == tool["name"]), None)
            arguments = ((call or {}).get("function") or {}).get("arguments")
        try:
            plan = json.loads(arguments) if isinstance(arguments, str) else arguments
        except Exception:
            plan = None
        if isinstance(plan, dict):
            _set_last_openai_error(None)
            return plan
        _set_last_openai_error("OpenAI did not return a query_puzzle_stats function call")
        return None
    return None


def _openai_answer(question: str, spec: Dict[str, Any], evidence: str) -> Optional[str]:
    """Write a natural Slack answer from facts already computed by the ledger executor."""
    api_key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY") or "").strip()
    if not api_key:
        return None
    model = _env_model()
    timeout_s = _env_timeout_s()
    effort = _env_reasoning_effort(model)
    instructions = (
        "Answer the user's puzzle-stat question using only the deterministic ledger result below. "
        "Do not invent facts, players, dates, or numbers. Preserve all counts and Slack mentions exactly. "
        "Be direct, natural, and concise for Slack; use a small amount of color only when the question invites it. "
        "If the evidence says no rows were found, say so plainly. Do not mention the query plan or internal data format."
    )
    user_input = json.dumps({"question": question, "validated_query": spec, "ledger_result": evidence}, ensure_ascii=False)
    requested = _api_mode()
    modes = ["responses", "chat"] if requested == "auto" else [requested]
    for mode in modes:
        if mode == "responses":
            payload: Dict[str, Any] = {
                "model": model,
                "instructions": instructions,
                "input": user_input,
                "max_output_tokens": DEFAULT_MAX_OUT,
            }
            if model_uses_reasoning(model) and effort:
                payload["reasoning"] = {"effort": effort}
        else:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": user_input},
                ],
            }
            payload[chat_completion_token_limit_param(model)] = DEFAULT_MAX_OUT
            if not model_uses_reasoning(model):
                payload["temperature"] = 0
            if model_uses_reasoning(model) and effort:
                payload["reasoning_effort"] = effort
        data = _call_openai_envelope(mode, payload, timeout_s, api_key)
        if data is None:
            err = (last_openai_error() or "").lower()
            if mode == "responses" and requested == "auto" and ("http 404" in err or "http 405" in err):
                continue
            return None
        text_out = _openai_text_from_envelope(mode, data)
        return text_out or None
    return None



# ---------------------------
# Spec validation + helpers
# ---------------------------

# Matches <@U123ABC>, <@U123ABC|whatever>, including when wrapped by mrkdwn.
_SLACK_USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")

# Slack mrkdwn can wrap tokens with *, _, ~, or `.
_MRKDWN_WRAP_CHARS = "*_~`"


def _strip_mrkdwn_wrap(s: str) -> str:
    return (s or "").strip().strip(_MRKDWN_WRAP_CHARS)


def _extract_mentioned_user_ids(text: str) -> List[str]:
    t = text or ""
    ids: List[str] = []
    for m in _SLACK_USER_MENTION_RE.finditer(t):
        uid = (m.group(1) or "").strip()
        if uid and uid not in ids:
            ids.append(uid)
    # Also accept bare IDs if someone pasted U123...
    for m in re.finditer(r"\b([UW][A-Z0-9]{8,})\b", t):
        uid = (m.group(1) or "").strip()
        if uid and uid not in ids:
            ids.append(uid)
    return ids


def _canonicalize_user_ref(token: str) -> str:
    """
    Accepts:
      - me
      - U123ABC... (Slack user id)
      - <@U123ABC> or <@U123ABC|name>
      - *<@U123ABC>* (mrkdwn-wrapped)
    Returns canonical 'me' or the raw Slack user id, or '' if not recognizable.
    """
    t = _strip_mrkdwn_wrap(token or "").strip()
    if not t:
        return ""
    # Strip possessive: "<@U...>'s" or "U...'s"
    t = re.sub(r"(?:\s*['’]s)\b", "", t).strip()
    if t.lower() == "me":
        return "me"
    m = _SLACK_USER_MENTION_RE.search(t)
    if m:
        return (m.group(1) or "").strip()
    m2 = re.search(r"\b([UW][A-Z0-9]{8,})\b", t)
    if m2:
        return (m2.group(1) or "").strip()
    return ""


_ALLOWED_SUBJECTS = {"user", "all_users", "game", ""}
_ALLOWED_MEASURES = {
    "game_days_played",
    "active_days",
    "wins",
    "ties",
    "losses",
    "awards",
    "strikeouts",
    "score_value",
    "placement",
    "daily_report",
    "clean_sweep",
    "monthly_titles",
    "tie_rules",
    "unsupported",
}
_ALLOWED_AGGREGATIONS = {
    "count",
    "sum",
    "mean",
    "median",
    "min",
    "max",
    "best",
    "worst",
    "distribution",
    "leaderboard",
    "summary",
    "",
}
_ALLOWED_GROUP_BY = {"", "user", "game", "day", "weekday", "week", "month"}
_ALLOWED_OUTPUTS = {"scalar", "summary", "leaderboard", "distribution", "breakdown", "unsupported", ""}


def _legacy_intent_to_stats_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    intent = str(spec.get("intent") or "").strip()
    game = str(spec.get("game") or "").strip()
    user = str(spec.get("user") or "").strip()
    stat = str(spec.get("stat") or "").strip().lower()
    dr = spec.get("date_range") if isinstance(spec.get("date_range"), dict) else _dr("all_time", "", "")
    limit = int(spec.get("limit") or 5) if str(spec.get("limit") or "").strip().isdigit() else 5

    if intent == "unsupported":
        return _unsupported_spec("unsupported")
    if intent == "wins_leaderboard":
        return _stats_spec(subject="all_users", user="", game=game, measure="wins", aggregation="leaderboard", date_range=dr, group_by="user", output="leaderboard", limit=limit)
    if intent == "all_games_awards_leaderboard":
        return _stats_spec(subject="all_users", user="", measure="wins", aggregation="leaderboard", date_range=dr, group_by="user", output="leaderboard", limit=limit)
    if intent == "user_game_awards":
        return _stats_spec(subject="user", user=user or "me", game=game, measure="awards", aggregation="summary", date_range=dr, output="summary")
    if intent == "user_stat":
        return _stats_spec(subject="user", user=user or "me", game=game, measure="score_value", aggregation=stat or "median", date_range=dr, output="scalar", stat=stat or "median")
    if intent == "best_week":
        return _stats_spec(subject="user", user=user or "me", measure="awards", aggregation="max", date_range=dr, group_by="week", output="breakdown")
    if intent == "personal_bests_by_game":
        return _stats_spec(subject="user", user=user or "me", measure="score_value", aggregation="best", date_range=dr, group_by="game", output="breakdown", stat="best")
    if intent == "global_bests_by_game":
        return _stats_spec(subject="all_users", user="", measure="score_value", aggregation="best", date_range=dr, group_by="game", output="breakdown", stat="best")
    if intent == "game_record":
        return _stats_spec(subject="all_users", user="", game=game, measure="score_value", aggregation="best", date_range=dr, group_by="user", output="leaderboard", limit=limit, stat="best")
    if intent == "distribution":
        return _stats_spec(subject="user", user=user or "me", game=game, measure="score_value", aggregation="distribution", date_range=dr, output="distribution")
    if intent == "placement_distribution":
        return _stats_spec(subject="user", user=user or "me", game=game, measure="placement", aggregation="distribution", date_range=dr, output="distribution")
    if intent == "consistency":
        return _stats_spec(subject="all_users", user="", game=game, measure="placement", aggregation="leaderboard", date_range=dr, group_by="user", output="leaderboard")
    if intent == "all_time_stats":
        return _stats_spec(subject="user", user=user or "me", measure="awards", aggregation="summary", date_range=dr, output="summary")
    return _unsupported_spec("invalid legacy intent")


def _validate_spec(spec: Dict[str, Any], games: List[str]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")

    if str(spec.get("schema_version") or "").strip() != STATS_QUERY_VERSION and "intent" in spec:
        spec = _legacy_intent_to_stats_spec(spec)

    spec["schema_version"] = STATS_QUERY_VERSION

    measure = str(spec.get("measure") or "").strip().lower()
    output = str(spec.get("output") or "").strip().lower()
    if measure == "unsupported" or output == "unsupported":
        out = _unsupported_spec(str(spec.get("unsupported_reason") or "unsupported"))
        filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
        if isinstance(filters.get("date_range"), dict):
            out["filters"]["date_range"] = filters["date_range"]
        elif isinstance(spec.get("date_range"), dict):
            out["filters"]["date_range"] = spec["date_range"]
        return out

    subject = str(spec.get("subject") or "user").strip().lower()
    if subject not in _ALLOWED_SUBJECTS:
        raise ValueError("invalid subject")
    spec["subject"] = subject or "user"

    if measure not in _ALLOWED_MEASURES:
        raise ValueError("invalid measure")
    spec["measure"] = measure

    aggregation = str(spec.get("aggregation") or "").strip().lower()
    if aggregation not in _ALLOWED_AGGREGATIONS:
        raise ValueError("invalid aggregation")
    spec["aggregation"] = aggregation

    group_by = str(spec.get("group_by") or "").strip().lower()
    if group_by == "empty":
        group_by = ""
    if group_by not in _ALLOWED_GROUP_BY:
        raise ValueError("invalid group_by")
    spec["group_by"] = group_by

    if output not in _ALLOWED_OUTPUTS:
        raise ValueError("invalid output")
    spec["output"] = output or "scalar"

    game = str(spec.get("game") or "").strip()
    filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
    filter_game = str(filters.get("game") or "").strip()
    game = game or filter_game
    if game:
        game_key = lambda value: " ".join(value.lower().replace("×", "x").split())
        g_map = {game_key(g): g for g in games}
        game_norm = g_map.get(game_key(game))
        if not game_norm:
            raise ValueError(f"unknown game '{game}'")
        spec["game"] = game_norm
        filters["game"] = game_norm
    else:
        spec["game"] = ""

    stat = str(spec.get("stat") or "").strip().lower()
    if measure == "score_value":
        if not stat and aggregation in ("median", "mean", "min", "max", "count", "best", "worst"):
            stat = aggregation
        if stat and stat not in ("median", "mean", "min", "max", "count", "best", "worst"):
            raise ValueError("invalid stat")
    else:
        stat = ""
    spec["stat"] = stat

    user_field = str(spec.get("user") or "").strip()
    filter_user = str(filters.get("user") or "").strip()
    user_field = user_field or filter_user
    if user_field:
        canon = _canonicalize_user_ref(user_field)
        if not canon:
            raise ValueError("invalid user")
        spec["user"] = canon
        filters["user"] = canon
    elif spec["subject"] == "user":
        spec["user"] = "me"
        filters["user"] = "me"
    else:
        spec["user"] = ""

    limit = spec.get("limit", 5)
    try:
        limit_i = int(limit)
    except Exception:
        limit_i = 5
    limit_i = max(3, min(25, limit_i))
    spec["limit"] = limit_i

    dr = spec.get("date_range") or filters.get("date_range") or {}
    if not isinstance(dr, dict):
        dr = {}
    preset = str(dr.get("preset") or "all_time").strip()
    dr.setdefault("preset", preset)
    dr.setdefault("start", str(dr.get("start") or ""))
    dr.setdefault("end", str(dr.get("end") or ""))
    filters["date_range"] = dr
    filters["primary_only"] = bool(filters.get("primary_only", True))
    for k in ("wins_eq", "ties_eq"):
        if k in filters and filters[k] not in ("", None):
            try:
                filters[k] = int(filters[k])
            except Exception:
                raise ValueError(f"invalid {k}")
    if "weekdays" in filters:
        raw_weekdays = filters.get("weekdays")
        if not isinstance(raw_weekdays, (list, tuple, set)):
            raw_weekdays = [raw_weekdays]
        weekdays: List[int] = []
        for value in raw_weekdays:
            try:
                day_num = int(value)
            except Exception:
                raise ValueError("invalid weekdays")
            if not 0 <= day_num <= 6:
                raise ValueError("invalid weekdays")
            if day_num not in weekdays:
                weekdays.append(day_num)
        filters["weekdays"] = sorted(weekdays)
    spec["filters"] = filters
    spec["date_range"] = dr

    return spec


def _plan_to_stats_spec(
    plan: Dict[str, Any],
    games: List[str],
    *,
    question: str,
    mentioned_user_ids: List[str],
    resolve_user_name: Optional[Callable[[str], str]],
) -> Dict[str, Any]:
    """Normalize model tool arguments, resolve player references, then validate locally."""
    spec = dict(plan)
    spec["schema_version"] = STATS_QUERY_VERSION
    if str(spec.get("measure") or "").strip().lower() == "unsupported":
        return _unsupported_spec(str(spec.get("unsupported_reason") or "unsupported"))
    filters = dict(spec.get("filters") or {})
    date_range = dict(spec.get("date_range") or filters.get("date_range") or {})
    for key in ("start", "end"):
        date_range[key] = str(date_range.get(key) or "")
    date_range["preset"] = str(date_range.get("preset") or "all_time")
    spec["date_range"] = date_range
    filters["date_range"] = date_range

    # Explicit mentions win over first-person wording. Plain names are resolved
    # through Slack's cached directory; unknown names never become the asker.
    raw_user = str(spec.get("user") or filters.get("user") or "").strip()
    named_target = _extract_named_target(question)
    if named_target:
        resolved = ""
        if resolve_user_name:
            try:
                resolved = str(resolve_user_name(named_target) or "").strip()
            except Exception:
                resolved = ""
        if not resolved:
            return _unsupported_spec(f"unresolved_user:{named_target}")
        raw_user = resolved
    elif mentioned_user_ids and (not raw_user or not _canonicalize_user_ref(raw_user)):
        if len(mentioned_user_ids) == 1:
            raw_user = mentioned_user_ids[0]
    elif raw_user and not _canonicalize_user_ref(raw_user):
        resolved = ""
        if resolve_user_name:
            try:
                resolved = str(resolve_user_name(raw_user) or "").strip()
            except Exception:
                resolved = ""
        if not resolved:
            return _unsupported_spec(f"unresolved_user:{raw_user}")
        raw_user = resolved
    elif not raw_user and str(spec.get("subject") or "") == "user":
        if _FIRST_PERSON_RE.search(question):
            raw_user = "me"
        else:
            return _unsupported_spec("Which player should I look up? Mention them or say 'me'.")
    spec["user"] = raw_user
    filters["user"] = raw_user or None

    if filters.get("game") is None:
        filters.pop("game", None)
    if filters.get("user") is None:
        filters.pop("user", None)

    # The model may express a user's "best" or "worst" as a numeric min/max.
    # Keep explicit min/max requests literal, but let score direction decide the
    # meaning of best/worst (higher points, lower time or guesses).
    if str(spec.get("measure") or "").strip().lower() == "score_value":
        question_lower = (question or "").lower()
        has_explicit_extremum = bool(re.search(r"\b(?:min(?:imum)?|max(?:imum)?)\b", question_lower))
        if not has_explicit_extremum:
            requested_stat = ""
            if re.search(r"\b(?:worst|slowest)\b", question_lower):
                requested_stat = "worst"
            elif re.search(r"\b(?:best|fastest)\b", question_lower):
                requested_stat = "best"
            if requested_stat:
                spec["stat"] = requested_stat
                spec["aggregation"] = requested_stat
    spec["filters"] = filters
    return _validate_spec(spec, games)



# ---------------------------
# Sheet loading helpers
# ---------------------------

def _load_scores_records(store) -> List[Dict[str, str]]:
    rows = store.scores.get_all_values()
    if len(rows) <= 1:
        return []
    header = rows[0]
    out: List[Dict[str, str]] = []
    for r in rows[1:]:
        if not r:
            continue
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append(rec)
    return out


def _load_daily_payloads(store) -> Dict[str, Dict[str, Any]]:
    rows = store.daily.get_all_values()
    if len(rows) <= 1:
        return {}
    header = rows[0]
    if "day" not in header or "summary_json" not in header:
        return {}
    day_i = header.index("day")
    sum_i = header.index("summary_json")
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows[1:]:
        if len(r) <= max(day_i, sum_i):
            continue
        d = (r[day_i] or "").strip()
        raw = (r[sum_i] or "").strip()
        if not d or not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            out[d] = payload
    return out


def _fmt_mmss(seconds: float) -> str:
    try:
        s = int(round(float(seconds)))
    except Exception:
        return str(seconds)
    if s < 0:
        s = 0
    m = s // 60
    sec = s % 60
    return f"{m}:{sec:02d}"


# ---------------------------
# Deterministic executors
# ---------------------------

def _wins_leaderboard(payloads: Dict[str, Dict[str, Any]], *, game: str, dr: DateRange, limit: int) -> str:
    wins: Dict[str, int] = {}
    ties: Dict[str, int] = {}
    days_considered = 0
    days_with_game = 0

    for day_key, p in payloads.items():
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        days_considered += 1
        wbg = p.get("winners_by_game") or {}
        outcome = wbg.get(game)
        if not isinstance(outcome, dict):
            continue
        days_with_game += 1
        res = str(outcome.get("result") or "")
        winners = outcome.get("winners") or []
        if not isinstance(winners, list):
            continue

        if res == "tie":
            for w in winners:
                if isinstance(w, dict):
                    uid = str(w.get("user_id") or "").strip()
                    if uid:
                        ties[uid] = ties.get(uid, 0) + 1
        else:
            # treat anything else as a trophy win (including legacy "win")
            if winners and isinstance(winners[0], dict):
                uid = str(winners[0].get("user_id") or "").strip()
                if uid:
                    wins[uid] = wins.get(uid, 0) + 1

    users = sorted(set(list(wins.keys()) + list(ties.keys())))
    if not users or days_with_game == 0:
        return f"*{game} wins leaderboard*\nNo finalized results found in that date range."

    ranked = sorted(users, key=lambda u: (-wins.get(u, 0), -ties.get(u, 0), u))
    top = ranked[:limit]

    lines: List[str] = []
    lines.append(f"*{game} wins leaderboard*")
    lines.append(
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} "
        f"(finalized days scanned: {days_considered}, days with {game}: {days_with_game})"
    )
    for i, uid in enumerate(top, 1):
        lines.append(f"{i}. <@{uid}>  :trophy:x{wins.get(uid, 0)}  :necktie:x{ties.get(uid, 0)}")
    return "\n".join(lines)


def _fmt_metric(metric_type: str, v: float, display: str = "") -> str:
    mt = (metric_type or "").strip().lower()
    if mt == "time":
        return _fmt_mmss(v)
    if mt == "guesses":
        # Mean/median can be non-integer.
        if abs(v - round(v)) < 1e-9:
            n = int(round(v))
            return f"{n} guess" + ("" if n == 1 else "es")
        return f"{v:.2f} guesses"
    if mt == "points":
        if abs(v - round(v)) < 1e-9:
            n = int(round(v))
            return f"{n} point" + ("" if n == 1 else "s")
        return f"{v:.2f} points"
    # Fallback
    if display:
        return display
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


def _user_stat(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    asker_uid: str,
    game: str,
    stat: str,
    dr: DateRange,
) -> str:
    by_day: Dict[str, List[Dict[str, str]]] = {}
    for r in scores:
        day_key = str(r.get("day") or "").strip()
        if not day_key:
            continue
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        by_day.setdefault(day_key, []).append(r)

    vals: List[int] = []
    samples: List[Tuple[str, int, str, str]] = []  # day, value, display, metric_type

    for day_key, rows in by_day.items():
        # Choose primary puzzle_id for that day+game by most unique players.
        counts: Dict[int, set] = {}
        for r in rows:
            if normalize_game(str(r.get("game") or "").strip()) != game:
                continue
            if record_is_dnf(r):
                continue
            uid = str(r.get("user_id") or "").strip()
            pid_s = str(r.get("puzzle_id") or "").strip()
            if not uid or not pid_s:
                continue
            try:
                pid = int(pid_s)
            except Exception:
                continue
            counts.setdefault(pid, set()).add(uid)

        if not counts:
            continue
        primary_pid = sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]

        # Pull asker's value for that day+game+primary puzzle_id
        for r in rows:
            if normalize_game(str(r.get("game") or "").strip()) != game:
                continue
            uid = str(r.get("user_id") or "").strip()
            if uid != asker_uid:
                continue
            pid_s = str(r.get("puzzle_id") or "").strip()
            try:
                pid = int(pid_s)
            except Exception:
                continue
            if pid != primary_pid:
                continue
            if record_is_dnf(r):
                continue
            mv_s = str(r.get("metric_value") or "").strip()
            try:
                mv = int(mv_s)
            except Exception:
                continue
            vals.append(mv)
            disp = str(r.get("display") or "").strip()
            mtype = str(r.get("metric_type") or "").strip()
            samples.append((day_key, mv, disp, mtype))
            break

    if not vals:
        return f"*{game} {stat}* for <@{asker_uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    # Metric type is stable per game; pick the most common in samples.
    types = [s[3] for s in samples if s[3]]
    metric_type = types[0] if types else "time"

    if stat == "count":
        out_val = str(len(vals))
    elif stat == "mean":
        out_val = _fmt_metric(metric_type, float(statistics.mean(vals)))
    elif stat == "median":
        out_val = _fmt_metric(metric_type, float(statistics.median(vals)))
    elif stat == "min":
        out_val = _fmt_metric(metric_type, float(min(vals)))
    elif stat == "max":
        out_val = _fmt_metric(metric_type, float(max(vals)))
    else:
        out_val = _fmt_metric(metric_type, float(statistics.median(vals)))

    higher = higher_is_better(metric_type)
    best = sorted(samples, key=lambda x: x[1], reverse=higher)[:3]
    worst = sorted(samples, key=lambda x: x[1], reverse=not higher)[:3]

    lines: List[str] = []
    lines.append(f"*{game} {stat}* for <@{asker_uid}>")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}")
    lines.append(f"- N={len(vals)}  {stat}={out_val}")
    if stat != "count":
        lines.append("- Best:")
        for d, v, disp, mt in best:
            lines.append(f"  - {d}: {_fmt_metric(mt or metric_type, float(v), disp)} ({disp or _fmt_metric(mt or metric_type, float(v))})")
        lines.append("- Worst:")
        for d, v, disp, mt in worst:
            lines.append(f"  - {d}: {_fmt_metric(mt or metric_type, float(v), disp)} ({disp or _fmt_metric(mt or metric_type, float(v))})")
    return "\n".join(lines)




def _best_week(payloads: Dict[str, Dict[str, Any]], *, asker_uid: str, dr: DateRange) -> str:
    # Week starts Monday
    wk: Dict[date, Tuple[int, int]] = {}

    for day_key, p in payloads.items():
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        awards = (p.get("awards_by_user") or {}).get(asker_uid)
        if awards is None:
            continue
        if isinstance(awards, int):
            trophies, ties = int(awards), 0
        elif isinstance(awards, dict):
            trophies = int(awards.get("wins", awards.get("trophies", 0)) or 0)
            ties = int(awards.get("ties", 0) or 0)
        else:
            continue

        ws = d - timedelta(days=d.weekday())
        cur = wk.get(ws, (0, 0))
        wk[ws] = (cur[0] + trophies, cur[1] + ties)

    if not wk:
        return f"*Best week* for <@{asker_uid}>\nNo finalized awards found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    best_start, (bw, bt) = max(wk.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0].toordinal()))
    best_end = best_start + timedelta(days=6)

    lines: List[str] = []
    lines.append(f"*Best week* for <@{asker_uid}>")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (finalized days)")
    lines.append(f"- Week: {best_start.isoformat()} to {best_end.isoformat()}")
    lines.append(f"- Awards: :trophy:x{bw}  :necktie:x{bt}")
    return "\n".join(lines)


def _iter_primary_day_game_rows(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    dr: DateRange,
    include_failures: bool = False,
) -> Dict[Tuple[str, str], List[Dict[str, str]]]:
    """
    Returns mapping (day_key, game_norm) -> rows belonging to that day/game's PRIMARY puzzle_id,
    where "primary" is defined as the puzzle_id with the most unique players that day.
    """
    by_day: Dict[str, List[Dict[str, str]]] = {}
    for r in scores:
        day_key = str(r.get("day") or "").strip()
        if not day_key:
            continue
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        by_day.setdefault(day_key, []).append(r)

    out: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for day_key, rows in by_day.items():
        # game -> pid -> set(uids)
        counts: Dict[str, Dict[int, set]] = {}
        for r in rows:
            g = normalize_game(str(r.get("game") or "").strip())
            if not g:
                continue
            uid = str(r.get("user_id") or "").strip()
            pid_s = str(r.get("puzzle_id") or "").strip()
            if not uid or not pid_s:
                continue
            try:
                pid = int(pid_s)
            except Exception:
                continue
            counts.setdefault(g, {}).setdefault(pid, set()).add(uid)

        for g, pid_map in counts.items():
            if not pid_map:
                continue
            primary_pid = sorted(pid_map.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
            key = (day_key, g)
            out[key] = [
                r for r in rows
                if normalize_game(str(r.get("game") or "").strip()) == g
                and str(r.get("puzzle_id") or "").strip().isdigit()
                and int(str(r.get("puzzle_id") or "").strip()) == primary_pid
                and (include_failures or not record_is_dnf(r))
            ]
    return out


def _personal_bests_by_game(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    uid: str,
    games: List[str],
    dr: DateRange,
) -> str:
    primary = _iter_primary_day_game_rows(scores, normalize_game=normalize_game, dr=dr)
    best_by_game: Dict[str, Tuple[int, str, str, str]] = {}  # game -> (mv, day, display, metric_type)

    for (day_key, g), rows in primary.items():
        if g not in games:
            continue
        for r in rows:
            if str(r.get("user_id") or "").strip() != uid:
                continue
            if record_is_dnf(r):
                continue
            mv_s = str(r.get("metric_value") or "").strip()
            try:
                mv = int(mv_s)
            except Exception:
                continue
            disp = str(r.get("display") or "").strip()
            mtype = str(r.get("metric_type") or "").strip()
            cur = best_by_game.get(g)
            if cur is None or metric_sort_value(mv, mtype) < metric_sort_value(cur[0], cur[3]):
                best_by_game[g] = (mv, day_key, disp, mtype)
            break

    if not best_by_game:
        return f"*Personal bests (by game)* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    lines: List[str] = []
    lines.append(f"*Personal bests (by game)* for <@{uid}>")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)")
    for g in games:
        if g not in best_by_game:
            continue
        mv, day_key, disp, mt = best_by_game[g]
        v = _fmt_metric(mt, float(mv), disp)
        lines.append(f"- {g}: {v} on {day_key} (<@{uid}>)")
    return "\n".join(lines)


def _global_bests_by_game(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    games: List[str],
    dr: DateRange,
) -> str:
    primary = _iter_primary_day_game_rows(scores, normalize_game=normalize_game, dr=dr)
    best_by_game: Dict[str, Tuple[int, str, str, str, str]] = {}  # game -> (mv, day, uid, display, metric_type)

    for (day_key, g), rows in primary.items():
        if g not in games:
            continue
        for r in rows:
            mv_s = str(r.get("metric_value") or "").strip()
            uid = str(r.get("user_id") or "").strip()
            if not uid:
                continue
            if record_is_dnf(r):
                continue
            try:
                mv = int(mv_s)
            except Exception:
                continue
            disp = str(r.get("display") or "").strip()
            mt = str(r.get("metric_type") or "").strip()
            cur = best_by_game.get(g)
            if cur is None or metric_sort_value(mv, mt) < metric_sort_value(cur[0], cur[4]):
                best_by_game[g] = (mv, day_key, uid, disp, mt)

    if not best_by_game:
        return f"*All-time best scores (by game)*\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    lines: List[str] = []
    lines.append("*All-time best scores (by game)*")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)")
    for g in games:
        if g not in best_by_game:
            continue
        mv, day_key, uid, disp, mt = best_by_game[g]
        v = _fmt_metric(mt, float(mv), disp)
        lines.append(f"- {g}: {v} by <@{uid}> on {day_key} ({disp or v})")
    return "\n".join(lines)


def _game_record(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    game: str,
    dr: DateRange,
    limit: int,
) -> str:
    primary = _iter_primary_day_game_rows(scores, normalize_game=normalize_game, dr=dr)
    entries: List[Tuple[int, str, str, str, str]] = []  # mv, day, uid, display, metric_type

    for (day_key, g), rows in primary.items():
        if g != game:
            continue
        for r in rows:
            uid = str(r.get("user_id") or "").strip()
            mv_s = str(r.get("metric_value") or "").strip()
            if not uid:
                continue
            if record_is_dnf(r):
                continue
            try:
                mv = int(mv_s)
            except Exception:
                continue
            disp = str(r.get("display") or "").strip()
            mt = str(r.get("metric_type") or "").strip()
            entries.append((mv, day_key, uid, disp, mt))

    if not entries:
        return f"*{game} record*\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    entries.sort(key=lambda x: (metric_sort_value(x[0], x[4]), x[1], x[2]))
    top = entries[: max(3, min(25, limit))]

    best_mv, best_day, best_uid, best_disp, best_mt = top[0]
    best_v = _fmt_metric(best_mt, float(best_mv), best_disp)

    lines: List[str] = []
    lines.append(f"*{game} record*")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)")
    lines.append(f"- Best: {best_v} by <@{best_uid}> on {best_day} ({best_disp or best_v})")
    lines.append("- Top:")
    for i, (mv, day_key, uid, disp, mt) in enumerate(top, 1):
        v = _fmt_metric(mt, float(mv), disp)
        lines.append(f"  {i}. {v} by <@{uid}> on {day_key}")
    return "\n".join(lines)


def _distribution_summary(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    uid: str,
    game: str,
    dr: DateRange,
) -> str:
    primary = _iter_primary_day_game_rows(scores, normalize_game=normalize_game, dr=dr)
    vals: List[int] = []
    mt = "time"
    for (day_key, g), rows in primary.items():
        if g != game:
            continue
        for r in rows:
            if str(r.get("user_id") or "").strip() != uid:
                continue
            mv_s = str(r.get("metric_value") or "").strip()
            try:
                mv = int(mv_s)
            except Exception:
                continue
            vals.append(mv)
            mt = str(r.get("metric_type") or mt).strip() or mt
            break

    if not vals:
        return f"*{game} distribution* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    vals_sorted = sorted(vals)
    n = len(vals_sorted)

    def q(p: float) -> float:
        if n == 1:
            return float(vals_sorted[0])
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(n - 1, lo + 1)
        frac = idx - lo
        return vals_sorted[lo] * (1 - frac) + vals_sorted[hi] * frac

    p10, p25, p50, p75, p90 = q(0.10), q(0.25), q(0.50), q(0.75), q(0.90)
    vmin, vmax = float(vals_sorted[0]), float(vals_sorted[-1])

    lines: List[str] = []
    lines.append(f"*{game} distribution* for <@{uid}>")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)")
    lines.append(f"- N={n}")
    lines.append(f"- min={_fmt_metric(mt, vmin)}  p10={_fmt_metric(mt, p10)}  p25={_fmt_metric(mt, p25)}  median={_fmt_metric(mt, p50)}  p75={_fmt_metric(mt, p75)}  p90={_fmt_metric(mt, p90)}  max={_fmt_metric(mt, vmax)}")
    if mt.lower() == "guesses":
        # Simple bar by guess count (usually 1..5)
        counts: Dict[int, int] = {}
        for v in vals_sorted:
            counts[int(v)] = counts.get(int(v), 0) + 1
        lines.append("- Histogram:")
        for k in sorted(counts.keys()):
            lines.append(f"  - {k}: " + ("█" * counts[k]) + f" ({counts[k]})")
    return "\n".join(lines)


def _all_games_awards_leaderboard(payloads: Dict[str, Dict[str, Any]], *, dr: DateRange, limit: int) -> str:
    wins: Dict[str, int] = {}
    ties: Dict[str, int] = {}
    days_scanned = 0
    days_with_awards = 0

    for day_key, p in payloads.items():
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        days_scanned += 1
        awards = p.get("awards_by_user") or {}
        if not isinstance(awards, dict) or not awards:
            continue
        days_with_awards += 1
        for uid, v in awards.items():
            if isinstance(v, int):
                wins[uid] = wins.get(uid, 0) + int(v)
            elif isinstance(v, dict):
                wins[uid] = wins.get(uid, 0) + int(v.get("wins", v.get("trophies", 0)) or 0)
                ties[uid] = ties.get(uid, 0) + int(v.get("ties", 0) or 0)

    users = sorted(set(list(wins.keys()) + list(ties.keys())))
    if not users:
        return "*All-games awards leaderboard*\nNo finalized awards found in that date range."

    ranked = sorted(users, key=lambda u: (-wins.get(u, 0), -ties.get(u, 0), u))
    top = ranked[:limit]

    lines: List[str] = []
    lines.append("*All-games awards leaderboard*")
    lines.append(
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} "
        f"(finalized days scanned: {days_scanned}, days with awards: {days_with_awards})"
    )
    for i, uid in enumerate(top, 1):
        lines.append(f"{i}. <@{uid}>  :trophy:x{wins.get(uid, 0)}  :necktie:x{ties.get(uid, 0)}")
    return "\n".join(lines)


def _user_game_awards(payloads: Dict[str, Dict[str, Any]], *, game: str, uid: str, dr: DateRange) -> str:
    wins = 0
    ties = 0
    days_scanned = 0
    days_with_game = 0

    for day_key, p in payloads.items():
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        days_scanned += 1
        wbg = p.get("winners_by_game") or {}
        outcome = wbg.get(game)
        if not isinstance(outcome, dict):
            continue
        days_with_game += 1
        res = str(outcome.get("result") or "")
        winners = outcome.get("winners") or []
        if not isinstance(winners, list):
            continue

        if res == "tie":
            for w in winners:
                if isinstance(w, dict) and str(w.get("user_id") or "").strip() == uid:
                    ties += 1
                    break
        else:
            if winners and isinstance(winners[0], dict) and str(winners[0].get("user_id") or "").strip() == uid:
                wins += 1

    lines: List[str] = []
    lines.append(f"*{game} awards* for <@{uid}>")
    lines.append(
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} "
        f"(finalized days scanned: {days_scanned}, days with {game}: {days_with_game})"
    )
    lines.append(f"- :trophy:x{wins}  :necktie:x{ties}")
    return "\n".join(lines)


# ---------------------------
# Placement / consistency / all-time stats helpers
# ---------------------------

def _compute_placements(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    games: List[str],
    dr: DateRange,
) -> Dict[str, Dict[str, List[int]]]:
    """Compute per-player placement ranks for each game across all days.

    Returns {user_id: {game: [list of rank ints]}}.
    Ranks use 1224 (standard competition) ranking: ties get the same rank.
    """
    primary = _iter_primary_day_game_rows(scores, normalize_game=normalize_game, dr=dr)
    placements: Dict[str, Dict[str, List[int]]] = {}

    for (day_key, game), rows in primary.items():
        if game not in games:
            continue

        # Parse metric values for each participant
        parsed: List[Tuple[int, str, str]] = []  # (metric_value, uid, metric_type)
        for r in rows:
            uid = str(r.get("user_id") or "").strip()
            mv_s = str(r.get("metric_value") or "").strip()
            if not uid or not mv_s:
                continue
            if record_is_dnf(r):
                continue
            try:
                mv = int(mv_s)
            except Exception:
                continue
            metric_type = str(r.get("metric_type") or "").strip()
            parsed.append((mv, uid, metric_type))

        if len(parsed) < 2:
            continue  # need at least 2 players for meaningful placement

        # Time and guesses are lower-is-better; native point scores are higher-is-better.
        metric_type = parsed[0][2]
        parsed.sort(key=lambda t: metric_sort_value(t[0], t[2] or metric_type))

        # Assign ranks (1224 competition ranking)
        ranks: Dict[str, int] = {}
        rank = 1
        i = 0
        while i < len(parsed):
            j = i
            while j < len(parsed) and parsed[j][0] == parsed[i][0]:
                j += 1
            for k in range(i, j):
                ranks[parsed[k][1]] = rank
            rank = j + 1
            i = j

        for uid, r in ranks.items():
            placements.setdefault(uid, {}).setdefault(game, []).append(r)

    return placements


def _placement_distribution(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    uid: str,
    game: str,
    games: List[str],
    dr: DateRange,
) -> str:
    """Show placement distribution for a single player, for one game or all games."""
    all_placements = _compute_placements(scores, normalize_game=normalize_game, games=games, dr=dr)

    user_data = all_placements.get(uid)
    if not user_data:
        return f"*Placement distribution* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    target_games = [game] if game else games
    all_ranks: List[int] = []
    per_game_lines: List[str] = []

    for g in target_games:
        ranks = user_data.get(g)
        if not ranks:
            continue
        all_ranks.extend(ranks)

        # Build histogram for this game
        counts: Dict[int, int] = {}
        for r in ranks:
            counts[r] = counts.get(r, 0) + 1
        n = len(ranks)
        top_half = sum(1 for r in ranks if r <= max(1, len(ranks) // 2 + 1))

        if game:
            # Single-game mode: show detailed histogram
            lines: List[str] = []
            title = f"*{g} placement distribution* for <@{uid}>"
            lines.append(title)
            lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (N={n} days played)")
            for rank_val in sorted(counts.keys()):
                c = counts[rank_val]
                pct = c * 100.0 / n
                bar = "\u2588" * max(1, round(c * 20.0 / n))
                ordinal = _ordinal(rank_val)
                lines.append(f"- {ordinal}: {c} ({pct:.0f}%) {bar}")

            top_half_count = sum(1 for r in ranks if r <= 2)
            mean_rank = statistics.mean(ranks)
            stdev_rank = statistics.pstdev(ranks) if len(ranks) > 1 else 0.0
            lines.append(f"Top-half: {top_half_count}/{n} ({top_half_count*100.0/n:.0f}%) | Mean rank: {mean_rank:.2f} | StDev: {stdev_rank:.2f}")
            return "\n".join(lines)

    # All-games mode
    if not all_ranks:
        return f"*Placement distribution* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    n = len(all_ranks)
    counts: Dict[int, int] = {}
    for r in all_ranks:
        counts[r] = counts.get(r, 0) + 1

    lines = []
    lines.append(f"*Placement distribution (all games)* for <@{uid}>")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (N={n} game-days)")
    for rank_val in sorted(counts.keys()):
        c = counts[rank_val]
        pct = c * 100.0 / n
        bar = "\u2588" * max(1, round(c * 20.0 / n))
        ordinal = _ordinal(rank_val)
        lines.append(f"- {ordinal}: {c} ({pct:.0f}%) {bar}")

    top_half_count = sum(1 for r in all_ranks if r <= 2)
    mean_rank = statistics.mean(all_ranks)
    stdev_rank = statistics.pstdev(all_ranks) if len(all_ranks) > 1 else 0.0
    lines.append(f"Top-half: {top_half_count}/{n} ({top_half_count*100.0/n:.0f}%) | Mean rank: {mean_rank:.2f} | StDev: {stdev_rank:.2f}")
    return "\n".join(lines)


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{('th','st','nd','rd','th','th','th','th','th','th')[n % 10]}"


def _consistency_compare(
    scores: List[Dict[str, str]],
    *,
    normalize_game: Callable[[str], str],
    game: str,
    games: List[str],
    dr: DateRange,
) -> str:
    """Compare all players' placement consistency head-to-head."""
    target_games = [game] if game else games
    all_placements = _compute_placements(scores, normalize_game=normalize_game, games=target_games, dr=dr)

    if not all_placements:
        title = f"*{game} consistency*" if game else "*Placement consistency (all games)*"
        return f"{title}\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    # Aggregate all ranks per user across target games
    user_stats: List[Tuple[str, int, Dict[int, int], float, float]] = []  # uid, n, rank_counts, top_half_pct, stdev

    for uid, game_ranks in all_placements.items():
        all_ranks: List[int] = []
        for g in target_games:
            all_ranks.extend(game_ranks.get(g, []))
        if not all_ranks:
            continue

        n = len(all_ranks)
        counts: Dict[int, int] = {}
        for r in all_ranks:
            counts[r] = counts.get(r, 0) + 1

        top_half_count = sum(1 for r in all_ranks if r <= 2)
        top_half_pct = top_half_count * 100.0 / n
        stdev = statistics.pstdev(all_ranks) if n > 1 else 0.0

        user_stats.append((uid, n, counts, top_half_pct, stdev))

    if not user_stats:
        title = f"*{game} consistency*" if game else "*Placement consistency (all games)*"
        return f"{title}\nNo data found."

    # Sort by top-half% desc, then stdev asc
    user_stats.sort(key=lambda t: (-t[3], t[4], t[0]))

    # Find max rank to determine columns
    max_rank = max(r for _, _, counts, _, _ in user_stats for r in counts.keys())

    lines: List[str] = []
    title = f"*{game} consistency*" if game else "*Placement consistency (all games)*"
    lines.append(title)
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}")
    lines.append("")

    for uid, n, counts, top_half_pct, stdev in user_stats:
        rank_parts: List[str] = []
        for r in range(1, max_rank + 1):
            c = counts.get(r, 0)
            pct = c * 100.0 / n
            ordinal = _ordinal(r)
            rank_parts.append(f"{ordinal}={pct:.0f}%")
        rank_str = "  ".join(rank_parts)
        lines.append(f"<@{uid}> (N={n})")
        lines.append(f"  {rank_str}")
        lines.append(f"  Top-half: {top_half_pct:.0f}% | StDev: {stdev:.2f}")

    return "\n".join(lines)


def _all_time_stats(
    scores: List[Dict[str, str]],
    payloads: Dict[str, Dict[str, Any]],
    *,
    normalize_game: Callable[[str], str],
    uid: str,
    games: List[str],
    dr: DateRange,
) -> str:
    """Comprehensive all-time stats dashboard for a single player."""
    # --- Trophies + ties from DailyResults ---
    total_wins = 0
    total_ties = 0
    days_played = 0

    for day_key, p in payloads.items():
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        awards = (p.get("awards_by_user") or {}).get(uid)
        if awards is None:
            continue
        days_played += 1
        if isinstance(awards, int):
            total_wins += int(awards)
        elif isinstance(awards, dict):
            total_wins += int(awards.get("wins", awards.get("trophies", 0)) or 0)
            total_ties += int(awards.get("ties", 0) or 0)

    # --- Per-game score stats ---
    primary = _iter_primary_day_game_rows(scores, normalize_game=normalize_game, dr=dr)
    game_vals: Dict[str, List[int]] = {}
    game_mt: Dict[str, str] = {}

    for (day_key, g), rows in primary.items():
        if g not in games:
            continue
        for r in rows:
            if str(r.get("user_id") or "").strip() != uid:
                continue
            mv_s = str(r.get("metric_value") or "").strip()
            try:
                mv = int(mv_s)
            except Exception:
                continue
            game_vals.setdefault(g, []).append(mv)
            mt = str(r.get("metric_type") or "time").strip()
            game_mt[g] = mt
            break

    # --- Placement stats ---
    all_placements = _compute_placements(scores, normalize_game=normalize_game, games=games, dr=dr)
    user_ranks = all_placements.get(uid, {})
    all_ranks: List[int] = []
    for g in games:
        all_ranks.extend(user_ranks.get(g, []))

    # --- Build output ---
    lines: List[str] = []
    lines.append(f"*All-time stats* for <@{uid}>")
    lines.append(f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}")
    lines.append(f"- Days played: {days_played} | :trophy:x{total_wins} | :necktie:x{total_ties}")

    lines.append("- Per game:")
    for g in games:
        vals = game_vals.get(g)
        if not vals:
            continue
        mt = game_mt.get(g, "time")
        n = len(vals)
        mean_v = statistics.mean(vals)
        median_v = statistics.median(vals)
        stdev_v = statistics.pstdev(vals) if n > 1 else 0.0
        pb = min(vals, key=lambda value: metric_sort_value(value, mt))
        if mt == "time":
            stdev_str = f"{stdev_v:.0f}s"
        else:
            stdev_str = f"{stdev_v:.1f}"
        lines.append(
            f"  - {g}: N={n}, mean={_fmt_metric(mt, mean_v)}, "
            f"median={_fmt_metric(mt, median_v)}, stdev={stdev_str}, "
            f"PB={_fmt_metric(mt, float(pb))}"
        )

    if all_ranks:
        n = len(all_ranks)
        counts: Dict[int, int] = {}
        for r in all_ranks:
            counts[r] = counts.get(r, 0) + 1
        rank_parts = []
        for rv in sorted(counts.keys()):
            pct = counts[rv] * 100.0 / n
            rank_parts.append(f"{_ordinal(rv)}={pct:.0f}%")
        lines.append(f"- Placement: {', '.join(rank_parts)}")

        top_half_count = sum(1 for r in all_ranks if r <= 2)
        stdev = statistics.pstdev(all_ranks) if n > 1 else 0.0
        lines.append(f"- Top-half rate: {top_half_count*100.0/n:.0f}% | Placement stdev: {stdev:.2f}")

    return "\n".join(lines)


def _build_stats_facts(
    scores: List[Dict[str, str]],
    payloads: Dict[str, Dict[str, Any]],
    *,
    normalize_game: Callable[[str], str],
    games: List[str],
    dr: DateRange,
) -> StatsFacts:
    primary = _iter_primary_day_game_rows(
        scores, normalize_game=normalize_game, dr=dr, include_failures=True,
    )
    score_facts: List[ScoreFact] = []

    for (day_key, game), rows in primary.items():
        if game not in games:
            continue
        parsed: List[Tuple[int, str, Dict[str, str]]] = []
        for r in rows:
            uid = str(r.get("user_id") or "").strip()
            mv_s = str(r.get("metric_value") or "").strip()
            if not uid:
                continue
            try:
                mv = int(mv_s)
            except Exception:
                if not record_is_dnf(r):
                    continue
                # Failure submissions may have no numeric result; keep their
                # participation fact while ensuring it never enters result stats.
                mv = 0
            parsed.append((mv, uid, r))
        result_rows = [item for item in parsed if not record_is_dnf(item[2])]
        ranks: Dict[Tuple[str, int], int] = {}
        if result_rows:
            metric_type = str(result_rows[0][2].get("metric_type") or "time").strip()
            result_rows.sort(key=lambda t: (metric_sort_value(t[0], str(t[2].get("metric_type") or metric_type)), t[1]))
        rank = 1
        i = 0
        while i < len(result_rows):
            j = i
            while j < len(result_rows) and result_rows[j][0] == result_rows[i][0]:
                j += 1
            for k in range(i, j):
                mv, uid, _r = result_rows[k]
                ranks[(uid, mv)] = rank
            rank = j + 1
            i = j
        for mv, uid, r in parsed:
            score_facts.append(
                ScoreFact(
                    day=day_key,
                    user_id=uid,
                    game=game,
                    metric_type=str(r.get("metric_type") or "").strip() or "time",
                    metric_value=mv,
                    display=str(r.get("display") or "").strip(),
                    rank=ranks.get((uid, mv), 0),
                    players=len(result_rows),
                    status=str(r.get("status") or "").strip(),
                )
            )

    active_counts: Dict[Tuple[str, str], int] = {}
    for f in score_facts:
        active_counts[(f.day, f.user_id)] = active_counts.get((f.day, f.user_id), 0) + 1

    awards_by_day_user: Dict[Tuple[str, str], AwardTally] = {}
    game_awards: List[GameAwardFact] = []
    for day_key, payload in payloads.items():
        d = _parse_day_key(day_key)
        if not d or d < dr.start or d > dr.end:
            continue
        for uid, v in (payload.get("awards_by_user") or {}).items():
            uid = str(uid or "").strip()
            if not uid:
                continue
            awards_by_day_user[(day_key, uid)] = unpack_awards(v)

        wbg = payload.get("winners_by_game") or {}
        if isinstance(wbg, dict):
            for game, outcome in wbg.items():
                game_norm = normalize_game(str(game or "").strip())
                if game_norm not in games or not isinstance(outcome, dict):
                    continue

                podium = outcome.get("podium")
                if isinstance(podium, list):
                    # Medal day: every medalist earned something in this game.
                    for entry in podium:
                        if not isinstance(entry, dict):
                            continue
                        medal = medal_tally(int(entry.get("place") or 0))
                        for uid in entry.get("user_ids") or []:
                            uid = str(uid or "").strip()
                            if uid and medal.has_medals:
                                game_awards.append(GameAwardFact(day_key, game_norm, uid, medal))
                    continue

                result = str(outcome.get("result") or "").strip().lower()
                winners = outcome.get("winners") or []
                if not isinstance(winners, list):
                    continue
                if result == "tie":
                    for w in winners:
                        if isinstance(w, dict):
                            uid = str(w.get("user_id") or "").strip()
                            if uid:
                                game_awards.append(GameAwardFact(day_key, game_norm, uid, AwardTally(ties=1)))
                else:
                    if winners and isinstance(winners[0], dict):
                        uid = str(winners[0].get("user_id") or "").strip()
                        if uid:
                            game_awards.append(GameAwardFact(day_key, game_norm, uid, AwardTally(wins=1)))

    daily_users: List[DailyUserFact] = []
    finalized_days = set(payloads.keys())
    for (day_key, uid), game_days in sorted(active_counts.items()):
        daily_users.append(
            DailyUserFact(
                day=day_key,
                user_id=uid,
                game_days_played=game_days,
                tally=awards_by_day_user.get((day_key, uid), AwardTally()),
                finalized=day_key in finalized_days,
            )
        )

    return StatsFacts(
        scores=score_facts,
        daily_users=daily_users,
        game_awards=game_awards,
        payloads=payloads,
        rows_scanned=len(score_facts) + len(daily_users) + len(game_awards),
    )


def _spec_uid(spec: Dict[str, Any], asker_user_id: str) -> str:
    user = str(spec.get("user") or "").strip() or "me"
    return asker_user_id if user == "me" else user


def _facts_for_user(facts: StatsFacts, uid: str, game: str = "") -> Tuple[List[ScoreFact], List[DailyUserFact], List[GameAwardFact]]:
    score_rows = [f for f in facts.scores if f.user_id == uid and (not game or f.game == game)]
    daily_rows = [f for f in facts.daily_users if f.user_id == uid]
    game_awards = [f for f in facts.game_awards if f.user_id == uid and (not game or f.game == game)]
    return score_rows, daily_rows, game_awards


def _format_scalar_count(title: str, uid: str, dr: DateRange, label: str, value: int) -> str:
    return "\n".join([
        f"*{title}* for <@{uid}>",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}",
        f"- {label}: {value}",
    ])


def _sum_tally(tallies: Any) -> AwardTally:
    return sum(tallies, AwardTally())


def _era_for(dr: DateRange) -> str:
    """How to render an empty tally for a window: as medals once it reaches the cut-over."""
    return "medals" if uses_medal_scoring(dr.end) else "legacy"


def _award_chips(t: AwardTally, dr: DateRange, *, legacy_sep: str = "  ") -> str:
    """A tally as it reads in an NL answer.

    Legacy windows keep the ':trophy:xN  :necktie:xM' layout the answers have
    always used; medal windows read '12 pts (:first_place_medal:x3, ...)', and a
    window that crosses the cut-over shows both.
    """
    return render_tally(t, empty_era=_era_for(dr), show_zeros=True, legacy_sep=legacy_sep)


def _wins_chips(t: AwardTally, dr: DateRange) -> str:
    """First-place finishes: trophies, gold medals, or both for a window across the cut-over."""
    parts = []
    if t.wins or (not t.gold and _era_for(dr) != "medals"):
        parts.append(f"{TROPHY}x{t.wins}")
    if t.gold or (not t.wins and _era_for(dr) == "medals"):
        parts.append(f"{GOLD}x{t.gold}")
    return " | ".join(parts)


def _format_user_awards_summary(facts: StatsFacts, *, uid: str, game: str, dr: DateRange) -> str:
    if game:
        tally = _sum_tally(f.tally for f in facts.game_awards if f.user_id == uid and f.game == game)
        days_scanned = sum(1 for day_key in facts.payloads if (d := _parse_day_key(day_key)) and dr.start <= d <= dr.end)
        days_with_game = 0
        for day_key, payload in facts.payloads.items():
            d = _parse_day_key(day_key)
            if not d or d < dr.start or d > dr.end:
                continue
            if game in (payload.get("winners_by_game") or {}):
                days_with_game += 1
        return "\n".join([
            f"*{game} awards* for <@{uid}>",
            f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (finalized days scanned: {days_scanned}, days with {game}: {days_with_game})",
            f"- {_award_chips(tally, dr)}",
        ])

    score_rows, daily_rows, _game_awards = _facts_for_user(facts, uid)
    tally = _sum_tally(f.tally for f in daily_rows)
    strikeouts = sum(1 for f in daily_rows if f.strikeout)
    active_days = len({f.day for f in daily_rows})
    game_days = len(score_rows)
    lines = [
        f"*Stats summary* for <@{uid}>",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}",
        f"- Active days: {active_days} | Game-days: {game_days} | {_award_chips(tally, dr, legacy_sep=' | ')} | Strikeouts: {strikeouts}",
    ]
    by_game: Dict[str, List[ScoreFact]] = {}
    for f in score_rows:
        if record_is_dnf(f):
            continue
        by_game.setdefault(f.game, []).append(f)
    if by_game:
        lines.append("- Per game:")
        for g in sorted(by_game.keys()):
            vals = by_game[g]
            best = min(vals, key=lambda f: (metric_sort_value(f.metric_value, f.metric_type), f.day))
            med = statistics.median([f.metric_value for f in vals])
            lines.append(f"  - {g}: N={len(vals)}, median={_fmt_metric(best.metric_type, float(med))}, PB={_fmt_metric(best.metric_type, float(best.metric_value), best.display)}")
    return "\n".join(lines)


def _format_awards_leaderboard(
    facts: StatsFacts, *, game: str, dr: DateRange, limit: int, by_points: bool = False,
) -> str:
    # Two ways to rank, because the two questions differ.
    #   Wins ("who wins Zip most often?"): first-place finishes first, then points.
    #     Trophies and golds are both a win, so this is the ordering that survives a
    #     window across the cut-over; a legacy-only window ranks by wins then ties.
    #   Points ("who has the most points?"): the standings, i.e. points, then the
    #     gold/silver/bronze count-back. Legacy days earned no points.
    tallies: Dict[str, AwardTally] = {}
    if game:
        for f in facts.game_awards:
            if f.game != game:
                continue
            tallies[f.user_id] = tallies.get(f.user_id, AwardTally()) + f.tally
        title = f"*{game} wins leaderboard*"
        days_with_game = 0
        days_scanned = 0
        for day_key, payload in facts.payloads.items():
            d = _parse_day_key(day_key)
            if not d or d < dr.start or d > dr.end:
                continue
            days_scanned += 1
            if game in (payload.get("winners_by_game") or {}):
                days_with_game += 1
        range_line = f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (finalized days scanned: {days_scanned}, days with {game}: {days_with_game})"
    else:
        for f in facts.daily_users:
            tallies[f.user_id] = tallies.get(f.user_id, AwardTally()) + f.tally
        title = "*All-games awards leaderboard*"
        days_scanned = len({f.day for f in facts.daily_users if f.finalized})
        days_with_awards = len({
            f.day for f in facts.daily_users
            if f.finalized and (f.tally.has_legacy or f.tally.has_medals)
        })
        range_line = f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (finalized days scanned: {days_scanned}, days with awards: {days_with_awards})"

    users = sorted(tallies, key=lambda u: tallies[u].sort_key(u, by_wins=not by_points))
    if not users:
        return f"{title}\nNo finalized awards found in that date range."
    lines = [title, range_line]
    for i, uid in enumerate(users[:limit], 1):
        lines.append(f"{i}. <@{uid}>  {_award_chips(tallies[uid], dr)}")
    return "\n".join(lines)


def _loss_counts(facts: StatsFacts, *, game: str = "") -> Dict[str, int]:
    """Count finalized losses for submitted primary puzzles; ties are not losses."""
    losses: Dict[str, int] = {}
    seen = set()
    for score in facts.scores:
        if game and score.game != game:
            continue
        key = (score.day, score.game, score.user_id)
        if key in seen:
            continue
        seen.add(key)
        payload = facts.payloads.get(score.day) or {}
        outcome = (payload.get("winners_by_game") or {}).get(score.game) or {}
        # A finalized failed submission is a loss even when every player
        # failed and the finalizer had no winner to write for that game.
        if score.day not in facts.payloads:
            continue
        result = str(outcome.get("result") or "").lower() if isinstance(outcome, dict) else ""
        if result not in ("win", "tie") and not record_is_dnf(score):
            continue
        winners = outcome.get("winners") or [] if isinstance(outcome, dict) else []
        winner_ids = {
            str(w.get("user_id") or "").strip() if isinstance(w, dict) else str(w or "").strip()
            for w in winners
        }
        if result in ("win", "tie") and score.user_id not in winner_ids:
            losses[score.user_id] = losses.get(score.user_id, 0) + 1
        elif result not in ("win", "tie") and record_is_dnf(score):
            losses[score.user_id] = losses.get(score.user_id, 0) + 1
    return losses


def _format_losses(facts: StatsFacts, *, uid: str, game: str, dr: DateRange, limit: int, leaderboard: bool) -> str:
    losses = _loss_counts(facts, game=game)
    if leaderboard:
        title = f"*{game} losses leaderboard*" if game else "*Losses leaderboard*"
        users = sorted(losses, key=lambda user: (-losses[user], user))
        if not users:
            return f"{title}\nNo finalized losses found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
        return "\n".join([
            title,
            f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}",
            *[f"{rank}. <@{user}> — {losses[user]} losses" for rank, user in enumerate(users[:limit], 1)],
        ])
    if not uid:
        return "That question needs a user."
    label = f"{game} losses" if game else "Losses"
    return "\n".join([
        f"*{label}* for <@{uid}>",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}",
        f"- Losses: {losses.get(uid, 0)}",
    ])


def _format_game_days_leaderboard(facts: StatsFacts, *, game: str, dr: DateRange, limit: int) -> str:
    counts: Dict[str, int] = {}
    for fact in facts.scores:
        if game and fact.game != game:
            continue
        counts[fact.user_id] = counts.get(fact.user_id, 0) + 1
    title = f"*{game} games played leaderboard*" if game else "*Games played leaderboard*"
    if not counts:
        return f"{title}\nNo games played in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    lines = [title, f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}"]
    for rank, (uid, count) in enumerate(sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:limit], 1):
        lines.append(f"{rank}. <@{uid}> — {count} game-days")
    return "\n".join(lines)


def _format_weekday_awards_leaderboard(
    facts: StatsFacts, *, game: str, weekdays: List[int], dr: DateRange, limit: int,
) -> str:
    names = {value: name.title() for name, value in _WEEKDAY_NUMBERS.items()}
    lines = ["*Wins by weekday*", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}"]
    for weekday in weekdays:
        day_tallies: Dict[str, AwardTally] = {}
        if game:
            for fact in facts.game_awards:
                d = _parse_day_key(fact.day)
                if not d or d.weekday() != weekday or fact.game != game:
                    continue
                day_tallies[fact.user_id] = day_tallies.get(fact.user_id, AwardTally()) + fact.tally
        else:
            for fact in facts.daily_users:
                d = _parse_day_key(fact.day)
                if not d or d.weekday() != weekday:
                    continue
                day_tallies[fact.user_id] = day_tallies.get(fact.user_id, AwardTally()) + fact.tally
        title = names.get(weekday, str(weekday))
        lines.append(f"*{title}*")
        users = sorted(day_tallies, key=lambda uid: day_tallies[uid].sort_key(uid, by_wins=True))
        if not users:
            lines.append("No finalized wins found.")
            continue
        for rank, uid in enumerate(users[:limit], 1):
            lines.append(f"{rank}. <@{uid}>  {_award_chips(day_tallies[uid], dr)}")
    return "\n".join(lines)


def _format_daily_wins_record(facts: StatsFacts, *, dr: DateRange) -> str:
    # Legacy days: the most trophies one player took in a day. Medal days: the
    # most points. They are different currencies, so each era gets its own record
    # rather than one number compared across both.
    trophy_days = [fact for fact in facts.daily_users if fact.tally.wins > 0]
    medal_days = [fact for fact in facts.daily_users if fact.tally.points > 0]
    range_line = f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}"
    if not trophy_days and not medal_days:
        return f"*Single-day trophy record*\nNo finalized win totals found in range {dr.start.isoformat()} to {dr.end.isoformat()}."

    def trophy_block() -> List[str]:
        best = max(fact.tally.wins for fact in trophy_days)
        leaders = sorted({(fact.day, fact.user_id) for fact in trophy_days if fact.tally.wins == best})
        return [f"- Record: {TROPHY}x{best}"] + [f"- <@{uid}> on {day_key}" for day_key, uid in leaders]

    def medal_block() -> List[str]:
        best = max(fact.tally.rank_key for fact in medal_days)
        leaders = sorted({(fact.day, fact.user_id) for fact in medal_days if fact.tally.rank_key == best})
        record = next(fact.tally for fact in medal_days if fact.tally.rank_key == best)
        return [f"- Record: {render_tally(record)}"] + [f"- <@{uid}> on {day_key}" for day_key, uid in leaders]

    if trophy_days and medal_days:
        lines = ["*Single-day records*", range_line, "Trophy days:", *trophy_block(), "Medal days (points):", *medal_block()]
    elif medal_days:
        lines = ["*Single-day points record*", range_line, *medal_block()]
    else:
        lines = ["*Single-day trophy record*", range_line, *trophy_block()]
    return "\n".join(lines)


def _format_clean_sweeps(facts: StatsFacts, *, dr: DateRange) -> str:
    sweeps: List[Tuple[str, str, List[str]]] = []
    for day_key, payload in facts.payloads.items():
        outcomes = payload.get("winners_by_game") or {}
        if not isinstance(outcomes, dict) or len(outcomes) < 2:
            continue
        sweep_uid = ""
        games_won: List[str] = []
        for game, outcome in outcomes.items():
            if not isinstance(outcome, dict) or str(outcome.get("result") or "").lower() != "win":
                sweep_uid = ""
                break
            winners = outcome.get("winners") or []
            if len(winners) != 1 or not isinstance(winners[0], dict):
                sweep_uid = ""
                break
            uid = str(winners[0].get("user_id") or "").strip()
            if not uid or (sweep_uid and uid != sweep_uid):
                sweep_uid = ""
                break
            sweep_uid = uid
            games_won.append(str(game))
        if sweep_uid and len(games_won) >= 2:
            sweeps.append((day_key, sweep_uid, sorted(games_won)))
    if not sweeps:
        return f"*Clean sweeps*\nNo player won every game with a recorded result in a day from {dr.start.isoformat()} to {dr.end.isoformat()}."
    lines = ["*Clean sweeps*", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}"]
    for day_key, uid, won_games in sorted(sweeps):
        lines.append(f"- {day_key}: <@{uid}> won all {len(won_games)} recorded games ({', '.join(won_games)})")
    return "\n".join(lines)


def _format_daily_report(facts: StatsFacts, *, dr: DateRange, games: List[str]) -> str:
    relevant_days = sorted(day_key for day_key in facts.payloads if (d := _parse_day_key(day_key)) and dr.start <= d <= dr.end)
    relevant_days = sorted(set(relevant_days) | {f.day for f in facts.scores if dr.start <= (_parse_day_key(f.day) or date.min) <= dr.end})
    if not relevant_days:
        return f"*Daily game report*\nNo scores or finalized results found from {dr.start.isoformat()} to {dr.end.isoformat()}."

    lines = ["*Daily game report*"]
    for day_key in relevant_days:
        lines.extend(["", f"*{day_key}*"])
        payload = facts.payloads.get(day_key) or {}
        outcomes = payload.get("winners_by_game") or {}
        reported_outcome = False
        if isinstance(outcomes, dict) and outcomes:
            for game in games:
                outcome = outcomes.get(game)
                if not isinstance(outcome, dict):
                    continue
                winners = outcome.get("winners") or []
                uids = [str(w.get("user_id") or "").strip() for w in winners if isinstance(w, dict)]
                uids = [uid for uid in uids if uid]
                if not uids:
                    continue
                reported_outcome = True
                result = str(outcome.get("result") or "").lower()
                label = "tie between" if result == "tie" else "winner"
                best_value = outcome.get("best_value")
                lines.append(f"- {game}: {label} {', '.join(f'<@{uid}>' for uid in uids)}" + (f" (score {best_value})" if best_value is not None else ""))
            if reported_outcome:
                continue

        submitted = [fact for fact in facts.scores if fact.day == day_key]
        by_game: Dict[str, List[ScoreFact]] = {}
        for fact in facts.result_scores:
            if fact.day == day_key:
                by_game.setdefault(fact.game, []).append(fact)
        if not by_game:
            lines.append("No completed game scores recorded." if submitted else "No game scores recorded.")
            continue
        lines.append("Current leaders (provisional until the day is finalized):")
        for game in games:
            entries = by_game.get(game) or []
            if not entries:
                continue
            metric_type = entries[0].metric_type
            best_value = min(entries, key=lambda f: metric_sort_value(f.metric_value, f.metric_type)).metric_value
            leaders = sorted({f.user_id for f in entries if f.metric_value == best_value})
            names = ", ".join(f"<@{uid}>" for uid in leaders)
            lines.append(f"- {game}: {names} ({_fmt_metric(metric_type, float(best_value))})")
    return "\n".join(lines)


def _format_monthly_titles(store, *, uid: str, dr: DateRange) -> str:
    monthly_ws = getattr(store, "monthly", None)
    if monthly_ws is None:
        return f"*Monthly championships* for <@{uid}>\nMonthly results are unavailable."
    try:
        rows = monthly_ws.get_all_values()
    except Exception:
        rows = []
    if len(rows) <= 1:
        return f"*Monthly championships* for <@{uid}>\nRecorded titles: 0"
    header = rows[0]
    if "month" not in header or "summary_json" not in header:
        return f"*Monthly championships* for <@{uid}>\nMonthly results are unavailable."
    month_i, summary_i = header.index("month"), header.index("summary_json")
    titles: List[str] = []
    for row in rows[1:]:
        month_key = (row[month_i] if month_i < len(row) else "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}", month_key):
            continue
        month_start = _parse_day_key(month_key + "-01")
        if not month_start or month_start < dr.start or month_start > dr.end:
            continue
        raw = (row[summary_i] if summary_i < len(row) else "").strip()
        try:
            summary = json.loads(raw) if raw else {}
        except Exception:
            continue
        champion = summary.get("champion") if isinstance(summary, dict) else None
        winners = champion.get("user_ids") if isinstance(champion, dict) else None
        if not isinstance(winners, list) and isinstance(summary, dict):
            winners = [str(s.get("user_id") or "") for s in summary.get("standings") or [] if isinstance(s, dict) and int(s.get("place") or 0) == 1]
        if uid in (winners or []):
            titles.append(month_key)
    lines = [f"*Monthly championships* for <@{uid}>", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}", f"- Recorded titles: {len(titles)}"]
    if titles:
        lines.append(f"- Months: {', '.join(sorted(set(titles)))}")
    return "\n".join(lines)


def _format_user_score_stat(facts: StatsFacts, *, uid: str, game: str, stat: str, dr: DateRange) -> str:
    vals = [f for f in facts.result_scores if f.user_id == uid and f.game == game]
    if not vals:
        return f"*{game} {stat}* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    metric_type = vals[0].metric_type or "time"
    nums = [f.metric_value for f in vals]
    if stat == "count":
        out_val = str(len(nums))
    elif stat == "mean":
        out_val = _fmt_metric(metric_type, float(statistics.mean(nums)))
    elif stat == "median":
        out_val = _fmt_metric(metric_type, float(statistics.median(nums)))
    elif stat == "min":
        out_val = _fmt_metric(metric_type, float(min(nums)))
    elif stat == "max":
        out_val = _fmt_metric(metric_type, float(max(nums)))
    elif stat == "best":
        best_value = min(nums, key=lambda value: metric_sort_value(value, metric_type))
        out_val = _fmt_metric(metric_type, float(best_value))
    elif stat == "worst":
        worst_value = max(nums, key=lambda value: metric_sort_value(value, metric_type))
        out_val = _fmt_metric(metric_type, float(worst_value))
    else:
        out_val = _fmt_metric(metric_type, float(statistics.median(nums)))

    lines = [
        f"*{game} {stat}* for <@{uid}>",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}",
        f"- N={len(nums)}  {stat}={out_val}",
    ]
    if stat != "count":
        best = sorted(vals, key=lambda f: (metric_sort_value(f.metric_value, f.metric_type), f.day))[:3]
        worst = sorted(vals, key=lambda f: (-metric_sort_value(f.metric_value, f.metric_type), f.day))[:3]
        lines.append("- Best:")
        for f in best:
            lines.append(f"  - {f.day}: {_fmt_metric(f.metric_type, float(f.metric_value), f.display)} ({f.display or _fmt_metric(f.metric_type, float(f.metric_value))})")
        lines.append("- Worst:")
        for f in worst:
            lines.append(f"  - {f.day}: {_fmt_metric(f.metric_type, float(f.metric_value), f.display)} ({f.display or _fmt_metric(f.metric_type, float(f.metric_value))})")
    return "\n".join(lines)


def _format_score_distribution(facts: StatsFacts, *, uid: str, game: str, dr: DateRange) -> str:
    vals = [f for f in facts.result_scores if f.user_id == uid and f.game == game]
    if not vals:
        return f"*{game} distribution* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    nums = sorted(f.metric_value for f in vals)
    mt = vals[0].metric_type
    n = len(nums)

    def q(p: float) -> float:
        if n == 1:
            return float(nums[0])
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(n - 1, lo + 1)
        frac = idx - lo
        return nums[lo] * (1 - frac) + nums[hi] * frac

    lines = [
        f"*{game} distribution* for <@{uid}>",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)",
        f"- N={n}",
        f"- min={_fmt_metric(mt, float(nums[0]))}  p10={_fmt_metric(mt, q(0.10))}  p25={_fmt_metric(mt, q(0.25))}  median={_fmt_metric(mt, q(0.50))}  p75={_fmt_metric(mt, q(0.75))}  p90={_fmt_metric(mt, q(0.90))}  max={_fmt_metric(mt, float(nums[-1]))}",
    ]
    return "\n".join(lines)


def _score_query_sort_value(value: int, metric_type: str, mode: str) -> int:
    """Sort ascending to select the requested score extremum."""
    if mode == "min":
        return value
    if mode == "max":
        return -value
    metric_value = metric_sort_value(value, metric_type)
    return -metric_value if mode == "worst" else metric_value


def _score_extreme_label(mode: str) -> str:
    return {"min": "Minimum", "max": "Maximum", "worst": "Worst"}.get(mode, "Best")


def _format_game_record(facts: StatsFacts, *, game: str, dr: DateRange, limit: int, mode: str = "best") -> str:
    title = f"{game} {mode} scores" if mode in ("min", "max", "worst") else f"{game} record"
    entries = sorted(
        [f for f in facts.result_scores if f.game == game],
        key=lambda f: (_score_query_sort_value(f.metric_value, f.metric_type, mode), f.day, f.user_id),
    )
    if not entries:
        return f"*{title}*\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    top = entries[:limit]
    best = top[0]
    lines = [
        f"*{title}*",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)",
        f"- {_score_extreme_label(mode)}: {_fmt_metric(best.metric_type, float(best.metric_value), best.display)} by <@{best.user_id}> on {best.day} ({best.display or _fmt_metric(best.metric_type, float(best.metric_value))})",
        f"- {'Bottom' if mode == 'worst' else 'Top'}:",
    ]
    for i, f in enumerate(top, 1):
        lines.append(f"  {i}. {_fmt_metric(f.metric_type, float(f.metric_value), f.display)} by <@{f.user_id}> on {f.day}")
    return "\n".join(lines)


def _format_bests_by_game(facts: StatsFacts, *, uid: str, games: List[str], dr: DateRange, mode: str = "best") -> str:
    best_by_game: Dict[str, ScoreFact] = {}
    for f in facts.result_scores:
        if f.user_id != uid:
            continue
        cur = best_by_game.get(f.game)
        candidate_key = (_score_query_sort_value(f.metric_value, f.metric_type, mode), f.day)
        current_key = (_score_query_sort_value(cur.metric_value, cur.metric_type, mode), cur.day) if cur else None
        if cur is None or candidate_key < current_key:
            best_by_game[f.game] = f
    if not best_by_game:
        title = "Personal worst scores" if mode == "worst" else "Personal bests"
        if mode in ("min", "max"):
            title = f"Personal {mode} scores"
        return f"*{title} (by game)* for <@{uid}>\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    title = "Personal worst scores" if mode == "worst" else "Personal bests"
    if mode in ("min", "max"):
        title = f"Personal {mode} scores"
    lines = [f"*{title} (by game)* for <@{uid}>", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)"]
    for g in games:
        f = best_by_game.get(g)
        if f:
            lines.append(f"- {g}: {_fmt_metric(f.metric_type, float(f.metric_value), f.display)} on {f.day} (<@{uid}>)")
    return "\n".join(lines)


def _format_global_bests_by_game(facts: StatsFacts, *, games: List[str], dr: DateRange, mode: str = "best") -> str:
    best_by_game: Dict[str, ScoreFact] = {}
    for f in facts.result_scores:
        cur = best_by_game.get(f.game)
        candidate_key = (_score_query_sort_value(f.metric_value, f.metric_type, mode), f.day, f.user_id)
        current_key = (_score_query_sort_value(cur.metric_value, cur.metric_type, mode), cur.day, cur.user_id) if cur else None
        if cur is None or candidate_key < current_key:
            best_by_game[f.game] = f
    if not best_by_game:
        title = "All-time worst scores" if mode == "worst" else "All-time best scores"
        if mode in ("min", "max"):
            title = f"All-time {mode} scores"
        return f"*{title} (by game)*\nNo scores found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    title = "All-time worst scores" if mode == "worst" else "All-time best scores"
    if mode in ("min", "max"):
        title = f"All-time {mode} scores"
    lines = [f"*{title} (by game)*", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (primary puzzles only)"]
    for g in games:
        f = best_by_game.get(g)
        if f:
            lines.append(f"- {g}: {_fmt_metric(f.metric_type, float(f.metric_value), f.display)} by <@{f.user_id}> on {f.day} ({f.display or _fmt_metric(f.metric_type, float(f.metric_value))})")
    return "\n".join(lines)


def _format_best_week(facts: StatsFacts, *, uid: str, dr: DateRange) -> str:
    weekly: Dict[date, AwardTally] = {}
    for f in facts.daily_users:
        if f.user_id != uid:
            continue
        d = _parse_day_key(f.day)
        if not d:
            continue
        ws = d - timedelta(days=d.weekday())
        weekly[ws] = weekly.get(ws, AwardTally()) + f.tally
    if not weekly:
        return f"*Best week* for <@{uid}>\nNo finalized awards found in range {dr.start.isoformat()} to {dr.end.isoformat()}."
    # Best tally wins; the latest week breaks a tie.
    best_start, best = max(weekly.items(), key=lambda kv: (kv[1].rank_key, kv[0].toordinal()))
    return "\n".join([
        f"*Best week* for <@{uid}>",
        f"Range: {dr.start.isoformat()} to {dr.end.isoformat()} (finalized days)",
        f"- Week: {best_start.isoformat()} to {(best_start + timedelta(days=6)).isoformat()}",
        f"- Awards: {_award_chips(best, dr)}",
    ])


def _execute_stats_query(
    spec: Dict[str, Any],
    *,
    asker_user_id: str,
    store,
    games: List[str],
    normalize_game: Callable[[str], str],
    dr: DateRange,
) -> Tuple[str, int]:
    scores = _load_scores_records(store)
    payloads = _load_daily_payloads(store)
    facts = _build_stats_facts(scores, payloads, normalize_game=normalize_game, games=games, dr=dr)

    measure = str(spec.get("measure") or "")
    aggregation = str(spec.get("aggregation") or "")
    output = str(spec.get("output") or "")
    group_by = str(spec.get("group_by") or "")
    subject = str(spec.get("subject") or "")
    game = str(spec.get("game") or "")
    uid = _spec_uid(spec, asker_user_id) if subject == "user" else ""
    limit = int(spec.get("limit") or 5)
    filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}

    if measure == "tie_rules":
        return (_tie_rules_text(), facts.rows_scanned)

    if measure == "daily_report":
        return (_format_daily_report(facts, dr=dr, games=games), facts.rows_scanned)

    if measure == "clean_sweep":
        return (_format_clean_sweeps(facts, dr=dr), facts.rows_scanned)

    if measure == "monthly_titles":
        if not uid:
            return ("That question needs a user.", facts.rows_scanned)
        return (_format_monthly_titles(store, uid=uid, dr=dr), facts.rows_scanned)

    if measure == "wins" and group_by == "day" and aggregation == "max":
        return (_format_daily_wins_record(facts, dr=dr), facts.rows_scanned)

    weekdays = filters.get("weekdays") or []
    if measure in ("wins", "ties", "awards") and weekdays:
        return (_format_weekday_awards_leaderboard(facts, game=game, weekdays=list(weekdays), dr=dr, limit=limit), facts.rows_scanned)

    if measure == "losses":
        leaderboard = output == "leaderboard" or aggregation == "leaderboard" or subject == "all_users"
        return (_format_losses(facts, uid=uid, game=game, dr=dr, limit=limit, leaderboard=leaderboard), facts.rows_scanned)

    if measure == "awards" and aggregation == "max" and group_by == "week" and uid:
        return (_format_best_week(facts, uid=uid, dr=dr), facts.rows_scanned)

    if measure == "game_days_played":
        if output == "leaderboard" or aggregation == "leaderboard" or subject == "all_users":
            return (_format_game_days_leaderboard(facts, game=game, dr=dr, limit=limit), facts.rows_scanned)
        if not uid:
            return ("That question needs a user.", facts.rows_scanned)
        n = len([f for f in facts.scores if f.user_id == uid and (not game or f.game == game)])
        label = f"{game} games played" if game else "Game-days played"
        return (_format_scalar_count("Games played", uid, dr, label, n), facts.rows_scanned)

    if measure == "active_days":
        if not uid:
            return ("That question needs a user.", facts.rows_scanned)
        n = len({f.day for f in facts.daily_users if f.user_id == uid})
        return (_format_scalar_count("Active days", uid, dr, "Active days", n), facts.rows_scanned)

    if measure == "strikeouts":
        if not uid:
            return ("That question needs a user.", facts.rows_scanned)
        n = sum(1 for f in facts.daily_users if f.user_id == uid and f.strikeout)
        return (_format_scalar_count("Strikeouts", uid, dr, "0-win, 0-tie days", n), facts.rows_scanned)

    if measure in ("wins", "ties", "awards"):
        if output == "leaderboard" or aggregation == "leaderboard" or subject == "all_users":
            return (
                _format_awards_leaderboard(facts, game=game, dr=dr, limit=limit, by_points=(measure == "awards")),
                facts.rows_scanned,
            )
        if not uid:
            return ("That question needs a user.", facts.rows_scanned)
        if output == "summary" or aggregation == "summary" or measure == "awards":
            return (_format_user_awards_summary(facts, uid=uid, game=game, dr=dr), facts.rows_scanned)
        if game:
            tally = _sum_tally(f.tally for f in facts.game_awards if f.user_id == uid and f.game == game)
        else:
            tally = _sum_tally(f.tally for f in facts.daily_users if f.user_id == uid)
        if measure == "wins":
            return ("\n".join([f"*Wins* for <@{uid}>", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}", f"- {_wins_chips(tally, dr)}"]), facts.rows_scanned)
        if measure == "ties":
            lines = [f"*Ties* for <@{uid}>", f"Range: {dr.start.isoformat()} to {dr.end.isoformat()}", f"- {NECKTIE}x{tally.ties}"]
            if uses_medal_scoring(dr.end):
                lines.append(f"- Neckties ended on {medal_scoring_start()}: tied players now share the place and its medal.")
            return ("\n".join(lines), facts.rows_scanned)
        return (_format_user_awards_summary(facts, uid=uid, game=game, dr=dr), facts.rows_scanned)

    if measure == "score_value":
        score_mode = str(spec.get("stat") or aggregation or "best").strip().lower()
        if group_by == "game" and subject == "user":
            return (_format_bests_by_game(facts, uid=uid, games=games, dr=dr, mode=score_mode), facts.rows_scanned)
        if group_by == "game" and subject == "all_users":
            return (_format_global_bests_by_game(facts, games=games, dr=dr, mode=score_mode), facts.rows_scanned)
        if output == "leaderboard" or subject == "all_users":
            if not game:
                return ("That question needs a game name (e.g., Zip, Tango).", facts.rows_scanned)
            return (_format_game_record(facts, game=game, dr=dr, limit=limit, mode=score_mode), facts.rows_scanned)
        if not uid:
            return ("That question needs a user.", facts.rows_scanned)
        if not game:
            return ("That stat needs a game name (e.g., Tango).", facts.rows_scanned)
        if output == "distribution" or aggregation == "distribution":
            return (_format_score_distribution(facts, uid=uid, game=game, dr=dr), facts.rows_scanned)
        stat = str(spec.get("stat") or aggregation or "median")
        return (_format_user_score_stat(facts, uid=uid, game=game, stat=stat, dr=dr), facts.rows_scanned)

    if measure == "placement":
        if subject == "user":
            if not uid:
                return ("That question needs a user.", facts.rows_scanned)
            return (_placement_distribution(scores, normalize_game=normalize_game, uid=uid, game=game, games=games, dr=dr), facts.rows_scanned)
        return (_consistency_compare(scores, normalize_game=normalize_game, game=game, games=games, dr=dr), facts.rows_scanned)

    return ("I can’t answer that one yet.", facts.rows_scanned)


# ---------------------------
# Main entrypoint called by score-bot
# ---------------------------

def _matched_prefix(raw_text: str) -> str:
    m = _QUERY_PREFIX_RE.match((raw_text or "").lstrip())
    return m.group(0).strip() if m else ""


def answer_nl_query(
    raw_text: str,
    asker_user_id: str,
    store,
    *,
    games: List[str],
    normalize_game: Callable[[str], str],
    today: date,
    slack_ts: str = "",
    resolve_user_name: Optional[Callable[[str], str]] = None,
) -> str:
    answer, spec, dr, meta = _answer_nl_query_inner(
        raw_text,
        asker_user_id,
        store,
        games=games,
        normalize_game=normalize_game,
        today=today,
        resolve_user_name=resolve_user_name,
    )
    try:
        log_fn = getattr(store, "log_nl_query", None)
        if callable(log_fn):
            log_fn(
                slack_ts=slack_ts,
                asker_user_id=asker_user_id,
                question=strip_nl_query_prefix(raw_text),
                prefix=_matched_prefix(raw_text),
                intent=str((spec or {}).get("measure") or (spec or {}).get("intent") or ""),
                game=str((spec or {}).get("game") or ""),
                target_user=str((spec or {}).get("user") or ""),
                stat=str((spec or {}).get("stat") or ""),
                preset=str(((spec or {}).get("date_range") or ((spec or {}).get("filters") or {}).get("date_range") or {}).get("preset") or ""),
                resolved_start=dr.start.isoformat() if dr else "",
                resolved_end=dr.end.isoformat() if dr else "",
                response_prefix=answer or "",
                schema_version=str((spec or {}).get("schema_version") or ""),
                translator_source=str((meta or {}).get("translator_source") or ""),
                raw_spec_json=json.dumps((meta or {}).get("raw_spec") or {}, separators=(",", ":"), sort_keys=True)[:5000],
                validated_spec_json=json.dumps(spec or {}, separators=(",", ":"), sort_keys=True)[:5000],
                unsupported_reason=str((meta or {}).get("unsupported_reason") or (spec or {}).get("unsupported_reason") or ""),
                rows_scanned=str((meta or {}).get("rows_scanned") or ""),
            )
    except Exception:
        # Logging must never break the user reply.
        pass
    return answer


def _answer_nl_query_inner(
    raw_text: str,
    asker_user_id: str,
    store,
    *,
    games: List[str],
    normalize_game: Callable[[str], str],
    today: date,
    resolve_user_name: Optional[Callable[[str], str]] = None,
) -> Tuple[str, Optional[Dict[str, Any]], Optional["DateRange"], Dict[str, Any]]:
    question = strip_nl_query_prefix(raw_text)
    if not question:
        return ("Try a question like: `bot: Who wins Zip most often?` or `bot: consistency`", None, None, {})

    mentioned_ids = _extract_mentioned_user_ids(raw_text)

    # 1) Let the model interpret the language and select a validated ledger query.
    # The rule translator is only a fallback for unavailable or invalid model plans.
    plan = _openai_plan(question, games, today.isoformat(), mentioned_user_ids=mentioned_ids)
    spec: Optional[Dict[str, Any]] = None
    unsupported_plan: Optional[Dict[str, Any]] = None
    raw_spec: Dict[str, Any] = dict(plan) if isinstance(plan, dict) else {}
    translator_source = ""
    if plan:
        try:
            planned_spec = _plan_to_stats_spec(
                plan,
                games,
                question=question,
                mentioned_user_ids=mentioned_ids,
                resolve_user_name=resolve_user_name,
            )
            if str(planned_spec.get("measure") or "") != "unsupported":
                spec = planned_spec
                translator_source = "openai_function"
            else:
                unsupported_plan = planned_spec
        except Exception:
            # The constrained planner may still fail local semantic validation;
            # the existing parser gets a chance before returning an error.
            spec = None

    if spec is None and unsupported_plan is None:
        fallback_spec = _rule_based_translate(question, games, today=today, resolve_user_name=resolve_user_name)
        if fallback_spec:
            spec = fallback_spec
            raw_spec = dict(fallback_spec)
            translator_source = "rule"
    if spec is None and unsupported_plan is not None:
        spec = unsupported_plan
        translator_source = "openai_function"

    if not spec:
        err = last_openai_error()
        if err:
            return (f"Natural-language query translation failed: {err}", None, None, {"translator_source": translator_source, "unsupported_reason": err})
        return ("Natural-language queries require a working OPENAI_API_KEY.", None, None, {"translator_source": translator_source, "unsupported_reason": "missing OpenAI key"})

    # Preserve the explicit mention if any legacy fallback emitted a non-ID user.
    try:
        if isinstance(spec, dict):
            u = str(spec.get("user") or "").strip()
            if mentioned_ids and (not u or not _canonicalize_user_ref(u)):
                if len(mentioned_ids) == 1:
                    spec["user"] = mentioned_ids[0]
    except Exception:
        pass

    try:
        spec = _validate_spec(spec, games)
    except Exception as e:
        return (
            f"Couldn't run that query (invalid spec): {e}",
            spec if isinstance(spec, dict) else None,
            None,
            {"translator_source": translator_source, "raw_spec": raw_spec, "unsupported_reason": str(e)},
        )

    if str(spec.get("measure") or "").strip() == "unsupported":
        reason = str(spec.get("unsupported_reason") or "unsupported")
        if reason.startswith("unresolved_user:"):
            unresolved_name = reason.split(":", 1)[1]
            return (
                f"I couldn’t match “{unresolved_name}” to one Slack account. Try asking again with their @mention.",
                spec,
                None,
                {"translator_source": translator_source, "raw_spec": raw_spec, "unsupported_reason": reason},
            )
        clarification = reason.strip()
        if translator_source == "openai_function" and clarification.endswith("?") and len(clarification) <= 180:
            return (
                clarification,
                spec,
                None,
                {"translator_source": translator_source, "raw_spec": raw_spec, "unsupported_reason": reason},
            )
        return (
            "I can’t answer that one yet. Try: `bot: Who wins Zip most often?` or `bot: What’s my median Tango time this month?`",
            spec,
            None,
            {"translator_source": translator_source, "raw_spec": raw_spec, "unsupported_reason": reason},
        )

    # 2) Resolve date range
    dr = _resolve_date_range(spec, today=today)

    # 3) Execute deterministically
    answer, rows_scanned = _execute_stats_query(
        spec,
        asker_user_id=asker_user_id,
        store=store,
        games=games,
        normalize_game=normalize_game,
        dr=dr,
    )
    if translator_source == "openai_function":
        answer = _openai_answer(question, spec, answer) or answer
    return (
        answer,
        spec,
        dr,
        {"translator_source": translator_source, "raw_spec": raw_spec, "rows_scanned": rows_scanned},
    )
