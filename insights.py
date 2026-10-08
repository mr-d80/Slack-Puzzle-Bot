#!/usr/bin/env python3
"""justwordle insights helpers (v3.2)

Design goals:
1) Deterministic stats drive everything (no LLM authority over scoring).
2) Narrative output is optional and can be LLM-rewritten behind an env flag.
3) Keep output Slack-friendly: short lines, low noise.

This module is intentionally standalone and uses only the Python standard library.
"""

from __future__ import annotations

import json
import logging
import os
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
import re

import hashlib

from awards import (
    POINTS_BY_PLACE, AwardTally, unpack_awards, uses_medal_scoring, render_tally, format_points,
)
from score_metrics import higher_is_better, record_is_dnf, metric_unit
from parser import canonical_user_id, normalize_game
from score_identity import deduplicate_score_records
from scoring import rank_finishers
from openai_compat import (
    chat_completion_token_limit_param as _chat_completion_token_limit_param,
    default_reasoning_effort as _default_reasoning_effort,
    model_prefers_responses as _model_prefers_responses,
    supports_temperature as _supports_temperature,
)


def _key_fp(k: str) -> str:
    k = (k or "").strip().encode("utf-8")
    return hashlib.sha256(k).hexdigest()[:10] if k else "missing"


def medal_scoring_facts() -> Dict[str, Any]:
    """Fact-bundle fields that mark a medal-era recap and say how it is scored.

    The AI rewrite is told to use only the facts it is given. The note keeps it
    from describing medal-era points as trophies, and says what the numbers mean.
    Absent from legacy bundles, which is how readers tell the two apart.
    """
    first, second, third = (POINTS_BY_PLACE[p] for p in (1, 2, 3))
    return {
        "scoring": "medals",
        "scoring_note": (
            f"Each game awards {first}/{second}/{third} points for 1st/2nd/3rd place "
            "(gold, silver, bronze medals). Players with the same result share a place "
            "and the places they occupy are skipped, so two firsts are followed by a third."
        ),
    }



DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 220
# A month wrap is 8-12 lines against a much larger fact bundle; the daily cap
# truncates it mid-sentence. Override with OPENAI_MAX_TOKENS_MONTHLY.
DEFAULT_MONTHLY_MAX_TOKENS = 700
DEFAULT_TIMEOUT_S = 15.0


logger = logging.getLogger("justwordle-insights")

# Prompt profile for recap rewrites.
# - basic: small, safe edits (close to the template)
# - flair: sports-announcer colour commentary, still grounded in facts
DEFAULT_RECAP_STYLE = "basic"
RECAP_STYLE_ENV = "AI_RECAP_STYLE"  # values: basic | flair

# v3.2: exclude certain games from the tightest-race metric (default: Pinpoint)
TIGHTEST_EXCLUDE_ENV = "TIGHTEST_RACE_EXCLUDE_GAMES"  # comma-separated game names
DEFAULT_TIGHTEST_EXCLUDE = "Pinpoint"

# Exclude certain games from the skunk metric (default: Pinpoint).
# The skunk is the largest last-place margin across games, but Pinpoint's margin
# is in guesses (plus a +100 DNF penalty) while every other game's is in seconds.
# Ranking 104 "points" against 104 "seconds" is not a comparison, so Pinpoint is
# kept out of it rather than allowed to win by unit accident.
# Measured over June-July 2026 it won only 1 of 40 skunks: the issue is that the
# one it won was incommensurable, not that it dominates.
SKUNK_EXCLUDE_ENV = "SKUNK_EXCLUDE_GAMES"  # comma-separated game names
DEFAULT_SKUNK_EXCLUDE = "Pinpoint"


def _parse_exclude_games(env_name: str, default: str) -> set:
    raw = (os.environ.get(env_name, default) or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


def _tightest_exclude_games() -> set:
    return _parse_exclude_games(TIGHTEST_EXCLUDE_ENV, DEFAULT_TIGHTEST_EXCLUDE)


def skunk_exclude_games() -> set:
    """Games that cannot be called a skunk. Public: the monthly rollup reuses it."""
    return _parse_exclude_games(SKUNK_EXCLUDE_ENV, DEFAULT_SKUNK_EXCLUDE)


def _collapse_blank_lines(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Models like to end lines with the two-space markdown line break, which
    # Slack does not use and which shows up as ragged trailing whitespace.
    t = re.sub(r"[ \t]+\n", "\n", t)
    # collapse any run of blank lines (or whitespace-only lines) to a single newline
    t = re.sub(r"\n[ \t]*\n+", "\n", t)
    return t.strip()

def _normalize_recap_style(v: str) -> str:
    v = str(v or "").strip().lower()
    if v in ("flair", "sports", "announcer", "color", "colour"):
        return "flair"
    if v in ("basic", "rewrite", "plain"):
        return "basic"
    return DEFAULT_RECAP_STYLE


def _recap_style_for_kind(kind: str) -> str:
    """Return the prompt profile for a given rewrite kind."""
    k = str(kind or "").strip().lower()
    if "recap" in k:
        return _normalize_recap_style(os.environ.get(RECAP_STYLE_ENV, DEFAULT_RECAP_STYLE))
    return "basic"


def _required_daily_callouts(facts: Dict[str, Any], labels: Optional[List[str]] = None) -> List[str]:
    """Return concrete fact callouts the AI recap must keep."""
    requested = set(labels or [])
    callouts: List[str] = []

    skunk = facts.get("skunk")
    if (not requested or "skunk" in requested) and isinstance(skunk, dict):
        game = str(skunk.get("game") or "").strip()
        last_uids = [str(uid).strip() for uid in (skunk.get("last_uids") or []) if str(uid).strip()]
        margin = int(skunk.get("margin") or 0)
        last_display = str(skunk.get("last_display") or skunk.get("last_value") or "").strip()
        if game and last_uids and margin > 0:
            tags = ", ".join(f"<@{uid}>" for uid in last_uids)
            mt = str(skunk.get("metric_type") or "time")
            unit = f" {metric_unit(mt) or mt}"
            display_clause = f" with a {last_display} finish" if last_display else ""
            callouts.append(
                f"Skunk: explicitly mention {tags} in {game} finished {margin}{unit} behind second-to-last{display_clause}."
            )

    return callouts


def _rewrite_includes_skunk_fact(text: str, skunk: Any) -> bool:
    """Detect whether an AI recap still contains the key skunk fact."""
    if not isinstance(skunk, dict) or not skunk.get("game") or not skunk.get("last_uids"):
        return True

    normalized = str(text or "").strip().lower()
    if not normalized:
        return False

    if str(skunk.get("game") or "").strip().lower() not in normalized:
        return False

    if str(skunk.get("margin") or "").strip() not in normalized:
        return False

    for uid in skunk.get("last_uids") or []:
        if f"<@{uid}>".lower() not in normalized:
            return False

    return True


def _missing_required_daily_fact_labels(text: str, facts: Dict[str, Any]) -> List[str]:
    """Return labels for required recap facts missing from the AI rewrite."""
    missing: List[str] = []
    if not _rewrite_includes_skunk_fact(text, facts.get("skunk")):
        missing.append("skunk")
    return missing


# ---------------------------------------------------------------------------
# Monthly recap required facts
#
# A month wrap has more facts competing for space than a daily recap, so the
# guardrail is narrower: the champion must survive, and the skunk of the month
# must survive if there was one. Everything else is the model's to prioritise.
# ---------------------------------------------------------------------------
def _champion_score(champion: Dict[str, Any]) -> Tuple[int, str]:
    """The champion's headline number and its unit: points for a medal month,
    trophies for a legacy one. Medal champions carry ``points``; legacy ones don't."""
    points = int(champion.get("points") or 0)
    if points > 0:
        return points, "points"
    return int(champion.get("wins") or 0), "trophies"


def _required_monthly_callouts(facts: Dict[str, Any], labels: Optional[List[str]] = None) -> List[str]:
    """Return concrete fact callouts the AI month wrap must keep."""
    requested = set(labels or [])
    callouts: List[str] = []

    champion = facts.get("champion")
    if (not requested or "champion" in requested) and isinstance(champion, dict):
        uids = [str(u).strip() for u in (champion.get("user_ids") or []) if str(u).strip()]
        if uids:
            tags = ", ".join(f"<@{uid}>" for uid in uids)
            score, unit = _champion_score(champion)
            shared = " (a shared title)" if champion.get("shared") else ""
            callouts.append(
                f"Champion: explicitly name {tags} as the month champion with {score} {unit}{shared}."
            )

    skunk_king = facts.get("skunk_king")
    if (not requested or "skunk_king" in requested) and isinstance(skunk_king, dict):
        uids = [str(u).strip() for u in (skunk_king.get("user_ids") or []) if str(u).strip()]
        count = int(skunk_king.get("count") or 0)
        if uids and count > 0:
            tags = ", ".join(f"<@{uid}>" for uid in uids)
            callouts.append(
                f"Skunk of the month: explicitly mention {tags} was skunked {count} times."
            )

    return callouts


def _rewrite_includes_champion_fact(text: str, champion: Any) -> bool:
    if not isinstance(champion, dict):
        return True
    uids = [str(u).strip() for u in (champion.get("user_ids") or []) if str(u).strip()]
    if not uids:
        return True

    normalized = str(text or "").strip().lower()
    if not normalized:
        return False

    if str(_champion_score(champion)[0]) not in normalized:
        return False

    return all(f"<@{uid}>".lower() in normalized for uid in uids)


def _rewrite_includes_skunk_king_fact(text: str, skunk_king: Any) -> bool:
    if not isinstance(skunk_king, dict) or int(skunk_king.get("count") or 0) <= 0:
        return True
    uids = [str(u).strip() for u in (skunk_king.get("user_ids") or []) if str(u).strip()]
    if not uids:
        return True

    normalized = str(text or "").strip().lower()
    if not normalized:
        return False

    return all(f"<@{uid}>".lower() in normalized for uid in uids)


def _missing_required_monthly_fact_labels(text: str, facts: Dict[str, Any]) -> List[str]:
    """Return labels for required month-wrap facts missing from the AI rewrite."""
    missing: List[str] = []
    if not _rewrite_includes_champion_fact(text, facts.get("champion")):
        missing.append("champion")
    if not _rewrite_includes_skunk_king_fact(text, facts.get("skunk_king")):
        missing.append("skunk_king")
    return missing


def _build_monthly_prompts(
    kind_l: str,
    facts: Dict[str, Any],
    fallback_text: str,
    style: str,
    required_block: str,
) -> Tuple[str, str, Dict[str, Any]]:
    """Prompts for the month wrap. Same voice as the daily recap, longer format."""
    label = str(facts.get("month_label") or facts.get("month") or "the month")

    # The photo-finish and runaway cut-offs were tuned in trophies. A medal month
    # counts in points, where one first place is worth POINTS_BY_PLACE[1], so
    # scale the same cut-offs rather than call every month a runaway.
    margin_unit = POINTS_BY_PLACE[1] if facts.get("scoring") == "medals" else 1
    photo_finish, runaway = 2 * margin_unit, 10 * margin_unit

    if kind_l == "monthly recap revision":
        sys_prompt = (
            "You revise month-end Slack wrap-ups for a friendly puzzle league. "
            "Preserve the voice, entertainment value, and core wording of the draft where possible. "
            "Use only the provided facts. Never invent winners, scores, margins, or players. "
            "When a game result is measured in points, higher points are better; never describe it as faster or as a time. "
            "Every required callout must appear explicitly in the revision. "
            "Keep the output Slack-friendly and under 12 lines."
        )
        user_prompt = (
            f"Revise this month wrap-up for {label} so it keeps the original voice "
            f"but includes every missing required callout.\n\n"
            f"{required_block}"
            f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"Draft wrap-up to revise:\n{fallback_text}"
        )
        return sys_prompt, user_prompt, {}

    if style == "flair":
        sys_prompt = (
            "You write month-end Slack wrap-ups for a friendly puzzle league. "
            "Use only the provided facts. Never invent winners, scores, margins, or players. "
            "When a game result is measured in points, higher points are better; never describe it as faster or as a time. "
            "Write in a lively sports-announcer voice with heavy snark, pitched as a season wrap. "
            "No profanity. "
            "Output must be 8-12 lines total, Slack-friendly formatting. "
            "Every required callout must appear explicitly in the wrap-up exactly once. "
            "End with a single snarky tagline (do not label it). "
            "Avoid hashtags. "
            "Do not use em-dash, en-dash, or similar punctuation. "
            "Do not repeat yourself within the wrap-up. "
            "When referencing a player, use Slack mention syntax `<@USERID>` exactly (not `@USERID`). "
            "If you bold a player mention, wrap the whole mention like `*<@USERID>*`. "
            "Bold game names similarly, like `*Zip*`."
        )
        user_prompt = (
            f"Write a fresh month-end wrap-up for {label} from the Facts JSON. Do NOT do a light rewrite. "
            f"Treat the Fallback as a factual checksum only, not as wording or structure.\n\n"
            f"Hard requirements:\n"
            f"- Use at least 60% new phrasing versus the Fallback.\n"
            f"- Do not copy any full line from the Fallback.\n"
            f"- Crown the champion in the opening line.\n"
            f"- If champion.margin <= {photo_finish}, sell it as a photo finish; if champion.margin >= {runaway}, call it a runaway.\n"
            f"- If champion.shared is true, treat it as a split title, never a solo win.\n"
            f"- Call out biggest_mover if present, framed as the month's momentum story.\n"
            f"- Name at least two game_champions and at least one entry from records.\n"
            f"- If skunk_king is present, roast them for the season-long futility.\n"
            f"- Mention perfect_attendance if anyone earned it.\n"
            f"- Do not list every player's line by line stats; this is colour, not a table.\n"
            f"{required_block}"
            f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"Fallback (do not copy wording):\n{fallback_text}"
        )
        extra: Dict[str, Any] = {"text": {"verbosity": os.environ.get("OPENAI_TEXT_VERBOSITY", "high")}}
        return sys_prompt, user_prompt, extra

    # basic
    sys_prompt = (
        "You write month-end Slack wrap-ups for a friendly puzzle league. "
        "Use only the provided facts. Do not invent winners, scores, or players. "
        "When a game result is measured in points, higher points are better; never describe it as faster or as a time. "
        "Keep it under 12 lines. Avoid hashtags. "
        "Every required callout must appear explicitly in the wrap-up exactly once. "
        "Use your best sports announcer voice to add colour commentary. "
        "The tagline (but never label it as 'Tagline') should scale its snark to how "
        "dramatic the title race was."
    )
    user_prompt = (
        f"Rewrite this month-end wrap-up for {label} to be punchy and readable. "
        f"Lead with the champion. Return plain text only.\n\n"
        f"{required_block}"
        f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"Fallback:\n{fallback_text}"
    )
    return sys_prompt, user_prompt, {}


def build_ai_prompts(kind: str, facts: Dict[str, Any], fallback_text: str) -> Tuple[str, str, Dict[str, Any]]:
    """Return (system_prompt, user_prompt, extra_fields).

    extra_fields are intended for the Responses API body (e.g., text verbosity).
    Callers using Chat Completions can ignore extra_fields.
    """
    kind_l = str(kind or "").strip().lower()
    monthly = kind_l.startswith("monthly recap")

    style = _recap_style_for_kind(kind)
    required_callouts = (
        _required_monthly_callouts(facts) if monthly else _required_daily_callouts(facts)
    )
    required_block = ""
    if required_callouts:
        required_block = "Required callouts that must appear explicitly in the recap:\n"
        required_block += "\n".join(f"- {line}" for line in required_callouts)
        required_block += "\n\n"

    if monthly:
        return _build_monthly_prompts(kind_l, facts, fallback_text, style, required_block)

    if kind_l == "daily recap revision":
        sys_prompt = (
            "You revise short Slack recaps for a friendly puzzle league. "
            "Preserve the voice, entertainment value, and core wording of the draft where possible. "
            "Use only the provided facts. Never invent winners, scores, margins, or players. "
            "When a game result is measured in points, higher points are better; never describe it as faster or as a time. "
            "Every required callout must appear explicitly in the revision. "
            "Keep the output Slack-friendly and under 8 lines."
        )
        user_prompt = (
            f"Revise this {kind} so it keeps the original voice but includes every missing required callout.\n\n"
            f"{required_block}"
            f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"Draft recap to revise:\n{fallback_text}"
        )
        return sys_prompt, user_prompt, {}

    if style == "flair":
        sys_prompt = (
            "You write short Slack recaps for a friendly puzzle league. "
            "Use only the provided facts. Never invent winners, scores, margins, or players. "
            "When a game result is measured in points, higher points are better; never describe it as faster or as a time. "
            "Write in a lively sports-announcer voice with heavy snark. "
            "No profanity. "
            "Output must be 6-8 lines total, Slack-friendly formatting. "
            "Every required callout must appear explicitly in the recap exactly once. "
            "End with a single snarky tagline (do not label it). "
            "Avoid hashtags. "
            "Do not use em-dash, en-dash, or similar punctuation. "
            "Do not repeat yourself within the recap. "
            "When referencing a player, use Slack mention syntax `<@USERID>` exactly (not `@USERID`). If you bold a player mention, wrap the whole mention like `*<@USERID>*`. Bold game names similarly, like `*Zip*`."
        )

        user_prompt = (
            f"Write a fresh {kind} from the Facts JSON. Do NOT do a light rewrite. "
            f"Treat the Fallback as a factual checksum only, not as wording or structure.\n\n"
            f"Hard requirements:\n"
            f"- Use at least 60% new phrasing versus the Fallback.\n"
            f"- Do not copy any full line from the Fallback.\n"
            f"- Add a short colour call (metaphors, hype, playful snark) tied to real facts (do not label it).\n"
            f"- Mention MVP if present.\n"
            f"- If tightest_race.margin <= 3, crank snark up 100 percent.\n"
            f"- If blowout.margin >= 20, add a 'rout' style call.\n"
            f"- If skunk is present, roast the skunked player(s) for their terrible finish.\n"
            f"{required_block}"
            f"- Do not repeat yourself within the recap.\n"
            f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"Fallback (do not copy wording):\n{fallback_text}"
        )

        # Responses verbosity is used for richer prose without sampling parameters.
        extra: Dict[str, Any] = {"text": {"verbosity": os.environ.get("OPENAI_TEXT_VERBOSITY", "high")}}
        return sys_prompt, user_prompt, extra

    # basic
    sys_prompt = (
        "You write short Slack recaps for a friendly puzzle league. "
        "Use only the provided facts. Do not invent winners, scores, or players. "
        "When a game result is measured in points, higher points are better; never describe it as faster or as a time. "
        "Keep it under 8 lines. Avoid hashtags. "
        "Every required callout must appear explicitly in the recap exactly once. "
        "Use your best sports announcer voice to add colour commentary to the recap. "
        "The tagline (but never label it as 'Tagline') should be adjusted based on the recap to increase the snark level for any dramatic wins, losses, or close games."
    )
    user_prompt = (
        f"Rewrite this {kind} message to be punchy and readable. "
        f"Return plain text only.\n\n"
        f"{required_block}"
        f"Facts JSON:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"Fallback:\n{fallback_text}"
    )
    return sys_prompt, user_prompt, {}


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _rewrite_debug_enabled() -> bool:
    # Set to 1/true/yes/on to log why AI rewrites were skipped or failed.
    return _truthy(os.environ.get("OPENAI_REWRITE_DEBUG", os.environ.get("AI_REWRITE_DEBUG", "0")))


def _normalize_api_mode(v: str) -> str:
    v = str(v or "").strip().lower()
    if v in ("responses", "response"):
        return "responses"
    if v in ("chat", "chatcompletions", "chat_completions"):
        return "chat"
    return "auto"


def _resolve_api_mode(model: str, requested: str) -> Tuple[str, str]:
    """Return (resolved_mode, endpoint_path)."""
    mode = _normalize_api_mode(requested)
    if mode == "responses":
        return "responses", "/v1/responses"
    if mode == "chat":
        return "chat", "/v1/chat/completions"

    if _model_prefers_responses(model):
        return "responses", "/v1/responses"
    return "chat", "/v1/chat/completions"


def _safe_int(x: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(x).strip())
    except Exception:
        return default


def _seconds_to_mmss(seconds: int) -> str:
    seconds = max(0, int(seconds))
    mm = seconds // 60
    ss = seconds % 60
    return f"{mm}:{ss:02d}"


SKUNK_THRESHOLD = 30  # last-place gap to trigger a "skunked" call


@dataclass(frozen=True)
class DailyRace:
    game: str
    winner_uid: str
    runner_up_uid: Optional[str]
    best_value: int
    runner_up_value: Optional[int]
    margin: Optional[int]
    metric_type: str  # e.g. "time", "guesses", or "points"
    winner_uids: Tuple[str, ...] = ()  # all tied winners (empty = legacy)
    runner_up_uids: Tuple[str, ...] = ()  # all players in the next rank group
    last_uids: Tuple[str, ...] = ()    # all tied last-place players
    last_value: Optional[int] = None   # worst metric_value in this game
    last_margin: Optional[int] = None  # gap between last and second-to-last


def compute_daily_races(records: Iterable[Dict[str, Any]], day: Optional[str] = None) -> List[DailyRace]:
    """Compute per-game rank groups before deriving race and last-place facts."""
    records = deduplicate_score_records(records, day=day)
    by_game: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        g = normalize_game(str(r.get("game") or "").strip())
        if not g:
            continue
        by_game.setdefault(g, []).append(r)

    races: List[DailyRace] = []
    for game, rows in by_game.items():
        parsed: List[Tuple[int, Dict[str, Any]]] = []
        for r in rows:
            uid = canonical_user_id(str(r.get("user_id") or "").strip())
            if not uid:
                continue
            mv = _safe_int(r.get("metric_value"))
            if mv is None or record_is_dnf(r):
                continue
            parsed.append((mv, r))

        if not parsed:
            continue

        mt = str(parsed[0][1].get("metric_type") or "time").strip() or "time"
        groups = rank_finishers(parsed, metric_type=mt)
        first = groups[0]
        first_value = first["value"]
        winner_uids = tuple(sorted({
            canonical_user_id(str(r.get("user_id") or "").strip())
            for r in first["records"]
        } - {""}))
        second = groups[1] if len(groups) > 1 else None
        runner_uids = tuple(sorted({
            canonical_user_id(str(r.get("user_id") or "").strip())
            for r in second["records"]
        } - {""})) if second else ()
        last = groups[-1]
        last_uids = tuple(sorted({
            canonical_user_id(str(r.get("user_id") or "").strip())
            for r in last["records"]
        } - {""}))
        previous = groups[-2] if len(groups) > 1 else None

        margin = None
        if second and first_value != second["value"]:
            margin = first_value - second["value"] if higher_is_better(mt) else second["value"] - first_value

        last_margin = None
        if previous and previous["value"] != last["value"]:
            last_margin = previous["value"] - last["value"] if higher_is_better(mt) else last["value"] - previous["value"]

        races.append(DailyRace(
            game=game,
            winner_uid=winner_uids[0] if winner_uids else "",
            runner_up_uid=runner_uids[0] if runner_uids else None,
            best_value=first_value,
            runner_up_value=second["value"] if second else None,
            margin=margin,
            metric_type=mt,
            winner_uids=winner_uids,
            runner_up_uids=runner_uids,
            last_uids=last_uids,
            last_value=last["value"],
            last_margin=last_margin,
        ))

    return races


def _format_value(metric_type: str, value: int, display_hint: Optional[str] = None) -> str:
    if metric_type == "time":
        if display_hint:
            return str(display_hint)
        return _seconds_to_mmss(value)
    shown = str(display_hint).strip() if display_hint else str(value)
    unit = metric_unit(metric_type) or str(metric_type or "units")
    # Preserve an already-labelled parser display; add units to bare values.
    if unit.lower() in shown.lower() or (metric_type == "points" and "pt" in shown.lower()):
        return shown
    return f"{shown} {unit}"


def build_daily_facts(
    day: str,
    records: List[Dict[str, Any]],
    awards_by_user: Dict[str, Any],
    best_display_by_game: Dict[str, Any],
    expected_players: int,
    complete_players: int,
) -> Dict[str, Any]:
    """Extract a compact, deterministic fact bundle for recap generation."""
    players = sorted({
        uid for r in records
        if (uid := canonical_user_id(str(r.get("user_id") or "").strip()))
    })
    races = compute_daily_races(records, day=day)

    tightest: Optional[DailyRace] = None
    blowout: Optional[DailyRace] = None
    tightest_exclude = _tightest_exclude_games()

    for r in races:
        if r.margin is None:
            continue
        # These month/day cross-game callouts compare seconds, so only time
        # results belong in the shared comparison.
        if r.metric_type != "time":
            continue

        # v3.2: Pinpoint is guesses-based and can skew "tightest race"; exclude by default.
        if r.game in tightest_exclude:
            # Still eligible for blowout.
            if blowout is None or r.margin > blowout.margin:  # type: ignore[operator]
                blowout = r
            continue

        if tightest is None or r.margin < tightest.margin:  # type: ignore[operator]
            tightest = r
        if blowout is None or r.margin > blowout.margin:  # type: ignore[operator]
            blowout = r

    # MVP: the best tally of the day (points, then gold/silver/bronze, for medal
    # days; wins then ties for legacy days) — collect ALL players at that level
    tallies = {uid: unpack_awards(v) for uid, v in (awards_by_user or {}).items()}
    mvp_key = max((t.rank_key for t in tallies.values()), default=None)

    mvp_uids: List[str] = []
    if mvp_key is not None:
        mvp_uids = sorted(uid for uid, t in tallies.items() if t.rank_key == mvp_key)

    mvp_obj: Optional[Dict[str, Any]] = None
    if mvp_uids:
        mvp_tally = tallies[mvp_uids[0]]
        mvp_obj = {
            "user_id": mvp_uids[0],
            "user_ids": mvp_uids,
        }
        if mvp_tally.has_medals:
            mvp_obj.update(
                gold=mvp_tally.gold, silver=mvp_tally.silver, bronze=mvp_tally.bronze,
                points=mvp_tally.points,
            )
        else:
            mvp_obj.update(wins=mvp_tally.wins, ties=mvp_tally.ties)

    facts: Dict[str, Any] = {
        "day": day,
        "players": players,
        "expected_players": int(expected_players or 0),
        "complete_players": int(complete_players or 0),
        "mvp": mvp_obj,
    }
    if uses_medal_scoring(day):
        facts.update(medal_scoring_facts())

    def race_to_obj(r: DailyRace) -> Dict[str, Any]:
        w_uids = list(r.winner_uids) if r.winner_uids else [r.winner_uid]
        return {
            "game": r.game,
            "winner_uid": r.winner_uid,
            "winner_uids": w_uids,
            "runner_up_uid": r.runner_up_uid,
            "runner_up_uids": list(r.runner_up_uids),
            "best_value": r.best_value,
            "runner_up_value": r.runner_up_value,
            "margin": r.margin,
            "metric_type": r.metric_type,
            "best_display": best_display_by_game.get(r.game),
        }

    facts["tightest_race"] = race_to_obj(tightest) if tightest else None
    facts["blowout"] = race_to_obj(blowout) if blowout else None

    # Skunk: find the game where last place lags second-to-last the most (≥ threshold).
    # Excluded games can't win it — see skunk_exclude_games for why Pinpoint is out.
    skunk_race: Optional[DailyRace] = None
    skunk_exclude = skunk_exclude_games()
    for r in races:
        if r.metric_type != "time":
            continue
        if r.game in skunk_exclude:
            continue
        if r.last_margin is not None and r.last_margin >= SKUNK_THRESHOLD:
            if skunk_race is None or r.last_margin > skunk_race.last_margin:  # type: ignore[operator]
                skunk_race = r
    if skunk_race is not None:
        facts["skunk"] = {
            "game": skunk_race.game,
            "last_uids": list(skunk_race.last_uids),
            "last_value": skunk_race.last_value,
            "margin": skunk_race.last_margin,
            "metric_type": skunk_race.metric_type,
            "last_display": _format_value(
                skunk_race.metric_type,
                skunk_race.last_value or 0,
            ),
        }
    else:
        facts["skunk"] = None

    return facts


def render_daily_recap_text(facts: Dict[str, Any]) -> str:
    """Pure template recap (no network calls)."""
    day = str(facts.get("day") or "").strip()
    players = facts.get("players") or []
    expected = int(facts.get("expected_players") or 0)
    complete = int(facts.get("complete_players") or 0)

    lines: List[str] = []
    lines.append(f"*Recap for {day}*")
    lines.append(f"- Participation: {len(players)} players, {complete}/{expected} fully completed")

    mvp = facts.get("mvp")
    if isinstance(mvp, dict) and mvp.get("user_id"):
        mvp_uids = mvp.get("user_ids") or [mvp["user_id"]]
        mvp_tags = ", ".join(f"<@{u}>" for u in mvp_uids)
        mvp_tally = unpack_awards(mvp)
        if mvp_tally.has_medals:
            # The points already carry their own parentheses.
            lines.append(f"- MVP: {mvp_tags}, {render_tally(mvp_tally)}")
        else:
            lines.append(f"- MVP: {mvp_tags} ({render_tally(mvp_tally)})")

    tight = facts.get("tightest_race")
    if isinstance(tight, dict) and tight.get("game") and tight.get("margin") is not None:
        g = tight["game"]
        w_uids = tight.get("winner_uids") or [tight.get("winner_uid")]
        runner_uids = tight.get("runner_up_uids") or ([tight.get("runner_up_uid")] if tight.get("runner_up_uid") else [])
        margin = int(tight.get("margin") or 0)
        mt = str(tight.get("metric_type") or "time")
        best_display = tight.get("best_display")
        best_val = _format_value(mt, int(tight.get("best_value") or 0), best_display)
        unit = "s" if mt == "time" else f" {metric_unit(mt) or mt}"
        if runner_uids:
            winner_tags = ", ".join(f"<@{u}>" for u in w_uids)
            runner_tags = ", ".join(f"<@{u}>" for u in runner_uids)
            lines.append(f"- Tightest: {g} won by {winner_tags} over {runner_tags} by {margin}{unit} (best {best_val})")

    blow = facts.get("blowout")
    if isinstance(blow, dict) and blow.get("game") and blow.get("margin") is not None:
        g = blow["game"]
        w_uids = blow.get("winner_uids") or [blow.get("winner_uid")]
        runner = blow.get("runner_up_uid")
        margin = int(blow.get("margin") or 0)
        mt = str(blow.get("metric_type") or "time")
        best_display = blow.get("best_display")
        best_val = _format_value(mt, int(blow.get("best_value") or 0), best_display)
        unit = "s" if mt == "time" else f" {metric_unit(mt) or mt}"
        if runner:
            winner_tags = ", ".join(f"<@{u}>" for u in w_uids)
            lines.append(f"- Biggest gap: {g} {winner_tags} by {margin}{unit} (best {best_val})")

    skunk = facts.get("skunk")
    if isinstance(skunk, dict) and skunk.get("game") and skunk.get("last_uids"):
        g = skunk["game"]
        s_uids = skunk["last_uids"]
        s_margin = int(skunk.get("margin") or 0)
        s_display = skunk.get("last_display") or str(skunk.get("last_value") or "")
        mt = str(skunk.get("metric_type") or "time")
        unit = "s" if mt == "time" else f" {metric_unit(mt) or mt}"
        skunk_tags = ", ".join(f"<@{u}>" for u in s_uids)
        lines.append(f"- Skunked: {skunk_tags} in {g} ({s_display}, +{s_margin}{unit} behind)")

    # Tiny spice, but deterministic enough (seeded by day) so reruns are stable.
    try:
        random.seed(day)
        taglines = [
            "No notes. The scoreboard has spoken.",
            "Stats only. Ego optional.",
            "Remember: it's just numbers until it's your numbers.",
        ]
        lines.append(f"_{random.choice(taglines)}_")
    except Exception:
        pass

    return "\n".join(lines)


def render_monthly_recap_text(facts: Dict[str, Any]) -> str:
    """Pure template month wrap (no network calls).

    This is commentary, not data: the standings table is a separate message
    built by ``monthly_summary.render_monthly_standings_text``.
    """
    label = str(facts.get("month_label") or facts.get("month") or "").strip()

    def tags(uids: Any) -> str:
        normalized: List[str] = []
        seen: set[str] = set()
        for value in uids if isinstance(uids, (list, tuple)) else []:
            uid = canonical_user_id(str(value or "").strip())
            if uid and uid not in seen:
                seen.add(uid)
                normalized.append(uid)
        return ", ".join(f"<@{uid}>" for uid in normalized)

    lines: List[str] = []
    lines.append(f"*Month in review: {label}*")

    medals = facts.get("scoring") == "medals"

    champion = facts.get("champion")
    if isinstance(champion, dict) and champion.get("user_ids"):
        margin = int(champion.get("margin") or 0)
        title = "Co-champions" if champion.get("shared") else "Champion"
        if medals:
            earned = format_points(int(champion.get("points") or 0))
            level_on = "points"
        else:
            earned = render_tally(unpack_awards(champion), show_zeros=False)
            level_on = "trophies"
        line = f"- {title}: {tags(champion['user_ids'])} ({earned})"
        runner_up = facts.get("runner_up")
        if isinstance(runner_up, dict) and runner_up.get("user_ids"):
            if margin > 0:
                line += f", {margin} clear of {tags(runner_up['user_ids'])}"
            else:
                line += f", level with {tags(runner_up['user_ids'])} on {level_on}"
        lines.append(line)

    mover = facts.get("biggest_mover")
    if isinstance(mover, dict) and mover.get("user_ids"):
        prev_label = str(facts.get("prev_month_label") or facts.get("prev_month") or "last month")
        if medals:
            moved = f"{int(mover.get('delta_points') or 0):+d} points"
        else:
            moved = f"{int(mover.get('delta_wins') or 0):+d} trophies"
        lines.append(f"- Biggest mover: {tags(mover['user_ids'])} {moved} vs {prev_label}")

    best_day = facts.get("best_day")
    if isinstance(best_day, dict) and best_day.get("user_ids") and best_day.get("day"):
        if medals:
            earned = format_points(int(best_day.get("points") or 0))
        else:
            earned = render_tally(AwardTally(wins=int(best_day.get("wins") or 0)), show_zeros=False)
        lines.append(
            f"- Best single day: {tags(best_day['user_ids'])} on {best_day['day']} ({earned})"
        )

    records = facts.get("records") or {}
    if isinstance(records, dict) and records:
        bits = []
        for game in sorted(records):
            rec = records[game]
            if not isinstance(rec, dict) or not rec.get("user_ids"):
                continue
            bits.append(f"{game} {rec.get('display')} ({tags(rec['user_ids'])})")
        if bits:
            lines.append(f"- Month records: {'; '.join(bits)}")

    tight = facts.get("tightest_race")
    if isinstance(tight, dict) and tight.get("game") and tight.get("margin") is not None:
        mt = str(tight.get("metric_type") or "time")
        unit = "s" if mt == "time" else f" {metric_unit(mt) or mt}"
        winners = tight.get("winner_uids") or ([tight.get("winner_uid")] if tight.get("winner_uid") else [])
        runner_uids = tight.get("runner_up_uids") or ([tight.get("runner_up_uid")] if tight.get("runner_up_uid") else [])
        if winners and runner_uids:
            runner_tags = tags(runner_uids)
            lines.append(
                f"- Tightest race: {tight['game']} on {tight.get('day')}, "
                f"{tags(winners)} over {runner_tags} by {int(tight['margin'])}{unit}"
            )

    blow = facts.get("blowout")
    if isinstance(blow, dict) and blow.get("game") and blow.get("margin") is not None:
        mt = str(blow.get("metric_type") or "time")
        unit = "s" if mt == "time" else f" {metric_unit(mt) or mt}"
        winners = blow.get("winner_uids") or ([blow.get("winner_uid")] if blow.get("winner_uid") else [])
        if winners:
            lines.append(
                f"- Biggest gap: {blow['game']} on {blow.get('day')}, "
                f"{tags(winners)} by {int(blow['margin'])}{unit}"
            )

    skunk_king = facts.get("skunk_king")
    if isinstance(skunk_king, dict) and skunk_king.get("user_ids") and int(skunk_king.get("count") or 0) > 0:
        lines.append(
            f"- Skunk of the month: {tags(skunk_king['user_ids'])} ({int(skunk_king['count'])}x)"
        )

    perfect = facts.get("perfect_attendance") or []
    if perfect:
        lines.append(f"- Perfect attendance: {tags(perfect)}")

    # Seeded by month so reruns of the same month are stable.
    try:
        random.seed(str(facts.get("month") or label))
        taglines = [
            "Another month in the books. The spreadsheet remembers everything.",
            "Standings reset, grudges do not.",
            "New month, same games, fresh excuses.",
        ]
        lines.append(f"_{random.choice(taglines)}_")
    except Exception:
        pass

    return "\n".join(lines)


def _openai_rewrite(kind: str, facts: Dict[str, Any], fallback_text: str) -> Optional[str]:
    """Optional rewrite via OpenAI. Returns None on any failure."""
    debug = _rewrite_debug_enabled()

    if not _truthy(os.environ.get("AI_REWRITE_ENABLED", "0")):
        if debug:
            logger.info("AI rewrite skipped (AI_REWRITE_ENABLED is false) kind=%s", kind)
        return None

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        # This is a configuration error. Log at WARNING even if debug is off.
        logger.warning("AI rewrite disabled (OPENAI_API_KEY missing) kind=%s", kind)
        return None

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com").strip().rstrip("/")
    model = (os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL)

    requested_mode = os.environ.get("OPENAI_API_MODE", "auto")
    resolved_mode, endpoint_path = _resolve_api_mode(model, requested_mode)

    sys_prompt, user_prompt, extra_fields = build_ai_prompts(kind, facts, fallback_text)

    # Config
    try:
        temperature = float(os.environ.get("OPENAI_TEMPERATURE", str(DEFAULT_TEMPERATURE)))
    except Exception:
        temperature = DEFAULT_TEMPERATURE

    try:
        max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
        if max_tokens <= 0:
            max_tokens = DEFAULT_MAX_TOKENS
    except Exception:
        max_tokens = DEFAULT_MAX_TOKENS

    # Month wraps need more room than the shared daily cap. Take the larger of
    # the two so an explicitly raised global still wins.
    if str(kind or "").strip().lower().startswith("monthly recap"):
        try:
            monthly_cap = int(
                os.environ.get("OPENAI_MAX_TOKENS_MONTHLY", str(DEFAULT_MONTHLY_MAX_TOKENS))
            )
        except Exception:
            monthly_cap = DEFAULT_MONTHLY_MAX_TOKENS
        if monthly_cap > 0:
            max_tokens = max(max_tokens, monthly_cap)

    reasoning_effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip()
    if not reasoning_effort:
        reasoning_effort = _default_reasoning_effort(model) or ""

    supports_temp = _supports_temperature(model, reasoning_effort)

    # Build request body for chosen endpoint.
    if resolved_mode == "responses":
        body: Dict[str, Any] = {
            "model": model,
            "instructions": sys_prompt,
            "input": user_prompt,
            "max_output_tokens": max_tokens,
        }
        # Optional per-profile knobs (e.g., text verbosity).
        if isinstance(extra_fields, dict):
            for k, v in extra_fields.items():
                # Avoid clobbering core fields.
                if k not in body:
                    body[k] = v
        if supports_temp:
            body["temperature"] = temperature
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        url = f"{base_url}{endpoint_path}"
    else:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            _chat_completion_token_limit_param(model): max_tokens,
        }
        if supports_temp:
            body["temperature"] = temperature
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        url = f"{base_url}{endpoint_path}"

    if debug:
        logger.info(
            "AI rewrite request kind=%s mode=%s model=%s temp=%s output_token_cap=%s reasoning_effort=%s url=%s",
            kind,
            resolved_mode,
            model,
            ("yes" if supports_temp else "no"),
            max_tokens,
            reasoning_effort or "",
            url,
        )

    api_key = os.environ.get("OPENAI_API_KEY", "")
    proj = (os.environ.get("OPENAI_PROJECT_ID") or "").strip()
    org = (os.environ.get("OPENAI_ORG_ID") or "").strip()

    logger.info(
        "OpenAI auth: key_fp=%s org=%s project=%s model=%s base_url=%s",
        _key_fp(api_key),
        org or "<none>",
        proj or "<none>",
        model,
        base_url,
    )


    req = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    def _extract_text_from_parts(parts: Any) -> str:
        if isinstance(parts, str):
            return parts
        if not isinstance(parts, list):
            return ""
        out: List[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t in ("text", "output_text", "input_text"):
                txt = p.get("text")
                if isinstance(txt, str) and txt:
                    out.append(txt)
        return "".join(out)

    def _extract_text_from_payload(payload: Any) -> Tuple[str, str]:
        """Return (text, diagnostic)."""
        if not isinstance(payload, dict):
            return "", "response was not a JSON object"

        if isinstance(payload.get("error"), dict):
            return "", f"API error: {payload.get('error')}"

        # Responses API shape
        ot = payload.get("output_text")
        if isinstance(ot, str) and ot.strip():
            return ot.strip(), ""

        if isinstance(payload.get("output"), list):
            texts: List[str] = []
            for item in payload.get("output", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "message":
                    continue
                texts.append(_extract_text_from_parts(item.get("content")))
            joined = "".join(texts).strip()
            if joined:
                return joined, ""

        # Chat Completions shape
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            ch0 = choices[0] if isinstance(choices[0], dict) else {}
            finish = ch0.get("finish_reason")
            msg = ch0.get("message") if isinstance(ch0.get("message"), dict) else {}

            refusal = msg.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                return "", f"Model refusal: {refusal.strip()}"

            text = _extract_text_from_parts(msg.get("content")).strip()
            if text:
                return text, ""

            alt = msg.get("text")
            if isinstance(alt, str) and alt.strip():
                return alt.strip(), ""

            tool_calls = msg.get("tool_calls")
            if finish in ("tool_calls", "function_call"):
                return "", f"finish_reason={finish} (assistant attempted tool calls), tool_calls={bool(tool_calls)}"
            if finish == "content_filter":
                return "", "finish_reason=content_filter (content omitted)"

            return "", f"empty assistant message content (finish_reason={finish})"

        return "", "unrecognized response shape"

    timeout_s = float(os.environ.get("OPENAI_TIMEOUT", str(DEFAULT_TIMEOUT_S)))

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            status = getattr(resp, "status", None)
        payload = json.loads(raw)
        text, diag = _extract_text_from_payload(payload)

        if not (text or "").strip():
            # This is a runtime failure. Log at WARNING even if debug is off.
            logger.warning("AI rewrite returned empty text kind=%s status=%s diag=%s", kind, status, diag)
            if debug:
                logger.info("AI rewrite raw payload (truncated) kind=%s: %s", kind, raw[:800])
            return None

        if debug:
            logger.info("AI rewrite success kind=%s chars=%s", kind, len(text.strip()))
        return text.strip()

    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        snippet = (err_body or "").strip().replace("\n", " ")
        if len(snippet) > 800:
            snippet = snippet[:800] + "…"
        logger.warning(
            "AI rewrite HTTPError kind=%s code=%s reason=%s body=%s",
            kind,
            getattr(e, "code", None),
            getattr(e, "reason", None),
            snippet or "<no body>",
        )
        return None

    except urllib.error.URLError as e:
        logger.warning("AI rewrite URLError kind=%s reason=%s", kind, getattr(e, "reason", e))
        return None

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("AI rewrite JSON decode error kind=%s err=%s", kind, e)
        return None

    except Exception as e:
        logger.warning("AI rewrite unexpected error kind=%s err=%s", kind, e)
        return None



def build_daily_recap_text(facts: Dict[str, Any]) -> str:
    """Deterministic recap with optional LLM rewrite."""
    fallback = render_daily_recap_text(facts)
    rewritten = _openai_rewrite("daily recap", facts, fallback)
    if not rewritten:
        return fallback

    rewritten = _collapse_blank_lines(rewritten)
    missing = _missing_required_daily_fact_labels(rewritten, facts)
    if not missing:
        return rewritten

    revision_seed = (
        f"{rewritten}\n\n"
        f"Missing required callouts:\n"
        + "\n".join(f"- {line}" for line in _required_daily_callouts(facts, labels=missing))
    )
    revised = _openai_rewrite("daily recap revision", facts, revision_seed)
    if not revised:
        return rewritten

    revised = _collapse_blank_lines(revised)
    return revised if not _missing_required_daily_fact_labels(revised, facts) else rewritten


def build_monthly_recap_text(facts: Dict[str, Any]) -> str:
    """Deterministic month wrap with optional LLM rewrite.

    Mirrors build_daily_recap_text: one rewrite, then one revision pass if the
    rewrite dropped a required callout, then fall back to whichever draft is
    still factually complete.
    """
    fallback = render_monthly_recap_text(facts)
    rewritten = _openai_rewrite("monthly recap", facts, fallback)
    if not rewritten:
        return fallback

    rewritten = _collapse_blank_lines(rewritten)
    missing = _missing_required_monthly_fact_labels(rewritten, facts)
    if not missing:
        return rewritten

    revision_seed = (
        f"{rewritten}\n\n"
        f"Missing required callouts:\n"
        + "\n".join(f"- {line}" for line in _required_monthly_callouts(facts, labels=missing))
    )
    revised = _openai_rewrite("monthly recap revision", facts, revision_seed)
    if not revised:
        return fallback

    revised = _collapse_blank_lines(revised)
    return revised if not _missing_required_monthly_fact_labels(revised, facts) else fallback


def rewrite_text(kind: str, facts: Dict[str, Any], fallback_text: str) -> str:
    """Generic helper for other scripts (weekly summaries, etc.)."""
    rewritten = _openai_rewrite(kind, facts, fallback_text)
    return _collapse_blank_lines(rewritten) if rewritten else fallback_text
