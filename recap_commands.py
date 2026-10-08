#!/usr/bin/env python3
"""recap_commands.py

Slack-triggered recap/finalize commands for the JustWordle / Slack Puzzle Tracker.

Adds a `/jw-recap` slash command that can:
- Finalize a day (writes DailyResults/Totals via your existing finalize_day())
  and then post a *normal looking* daily post: scores first, then recap.
- Repost an already-finalized day from the DailyResults ledger (no recalculation).

Command usage
- /jw-recap                        -> finalize *today* (SCORE_DAY_TZ) and post scores+recap
- /jw-recap yesterday              -> finalize yesterday and post scores+recap
- /jw-recap 2026-02-20             -> finalize that day and post scores+recap
- /jw-recap 2026-02-20 --repost    -> repost scores+recap for an already-finalized day (no recalculation)

Useful flags
- --recap-only / --no-scores       -> post recap only (no scores block)
- --force                          -> if finalize_day supports a force/override flag, pass it

Integration (in score-bot.py)
  from recap_commands import register_recap_commands
  register_recap_commands(app, sys.modules[__name__])

Notes
- Uses a background thread so Slack's 3s ack window isn't missed.
- Assumes the passed bot_module exposes:
    - store (with store.day_already_posted(day) and store.daily sheet preferred)
    - finalize_day(day, channel, post=...)  (signature can vary; we introspect)
    - SCORE_DAY_TZ optional (tzinfo) to interpret 'today' in bot's scoring timezone
"""

from __future__ import annotations

import inspect
import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from awards import render_podium, render_tally, unpack_awards
from command_access import command_access_error
from score_metrics import metric_sort_value, record_is_dnf


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"(?<!\d)(\d+):(\d{2})(?::(\d{2}))?(?!\d)")
_GUESSES_RE = re.compile(r"(?<!\d)(\d+)\s*guess(?:es)?(?!\d)", re.IGNORECASE)
_WORDLE_RE = re.compile(r"(?<!\d)([1-6Xx])\s*/\s*6(?!\d)")


@dataclass(frozen=True)
class RecapArgs:
    day: str
    repost: bool = False
    with_scores: bool = True
    force: bool = False


def _parse_day(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _today_in_tz(tz) -> date:
    if tz is None:
        return datetime.now().date()
    try:
        return datetime.now(tz=tz).date()
    except Exception:
        return datetime.now().date()


def _resolve_day_arg(text: str, bot_module: Any) -> RecapArgs:
    t = (text or "").strip()
    parts = [p for p in re.split(r"\s+", t) if p]

    repost = any(p.lower() in ("--repost", "--replay") for p in parts)
    force = any(p.lower() in ("--force", "--override") for p in parts)
    no_scores = any(p.lower() in ("--recap-only", "--no-scores", "--no-score") for p in parts)

    day = ""
    if parts:
        for p in parts:
            if p.startswith("--"):
                continue
            day = p
            break

    tz = getattr(bot_module, "SCORE_DAY_TZ", None)
    today = _today_in_tz(tz)

    if not day or day.lower() == "today":
        return RecapArgs(day=today.isoformat(), repost=repost, with_scores=not no_scores, force=force)

    if day.lower() == "yesterday":
        return RecapArgs(day=(today - timedelta(days=1)).isoformat(), repost=repost, with_scores=not no_scores, force=force)

    if _DAY_RE.match(day) and _parse_day(day):
        return RecapArgs(day=day, repost=repost, with_scores=not no_scores, force=force)

    return RecapArgs(day=today.isoformat(), repost=repost, with_scores=not no_scores, force=force)


def _day_already_posted(bot_module: Any, day: str) -> bool:
    store = getattr(bot_module, "store", None)
    if store is None:
        return False

    fn = getattr(store, "day_already_posted", None)
    if callable(fn):
        try:
            return bool(fn(day))
        except Exception:
            return False

    daily = getattr(store, "daily", None)
    if daily is None:
        return False

    try:
        rows = daily.get_all_values()
    except Exception:
        return False

    if len(rows) <= 1:
        return False

    header = rows[0]
    if "day" not in header or "summary_json" not in header:
        return False

    di = header.index("day")
    si = header.index("summary_json")

    for r in rows[1:]:
        if len(r) <= max(di, si):
            continue
        if (r[di] or "").strip() == day and (r[si] or "").strip():
            return True

    return False


def _load_daily_summary_payload(bot_module: Any, day: str) -> Optional[Dict[str, Any]]:
    store = getattr(bot_module, "store", None)
    if store is None:
        return None

    daily = getattr(store, "daily", None)
    if daily is None:
        return None

    try:
        rows = daily.get_all_values()
    except Exception:
        return None

    if len(rows) <= 1:
        return None

    header = rows[0]
    if "day" not in header or "summary_json" not in header:
        return None

    di = header.index("day")
    si = header.index("summary_json")

    for r in rows[1:]:
        if len(r) <= max(di, si):
            continue
        if (r[di] or "").strip() != day:
            continue
        raw = (r[si] or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    return None


def _render_recap_from_payload(day: str, payload: Dict[str, Any]) -> str:
    recap_text = str(payload.get("recap_text") or "").strip()
    if recap_text:
        return recap_text

    recap_facts = payload.get("recap_facts") or {}
    winners_by_game = payload.get("winners_by_game") or {}
    awards_by_user = payload.get("awards_by_user") or {}

    players = recap_facts.get("players")
    if not isinstance(players, list):
        players = []

    # MVP: the best tally of the day (points for a medal day, wins then ties for a
    # legacy day). Nobody is MVP of a day on which nothing was awarded.
    tallies = (
        {str(uid): unpack_awards(v) for uid, v in awards_by_user.items()}
        if isinstance(awards_by_user, dict) else {}
    )
    mvp_key = max((t.rank_key for t in tallies.values()), default=None)

    mvp_uids: List[str] = []
    if mvp_key is not None and any(mvp_key):
        mvp_uids = sorted(uid for uid, t in tallies.items() if t.rank_key == mvp_key)

    lines = [f"*Recap for {day}*"]
    if players:
        lines.append(f"- Participation: {len(players)} players")
    if mvp_uids:
        mvp_tags = ", ".join(f"<@{u}>" for u in mvp_uids)
        mvp_tally = tallies[mvp_uids[0]]
        if mvp_tally.has_medals:
            # The points already carry their own parentheses.
            lines.append(f"- MVP: {mvp_tags}, {render_tally(mvp_tally)}")
        else:
            lines.append(f"- MVP: {mvp_tags} ({render_tally(mvp_tally)})")

    if isinstance(winners_by_game, dict) and winners_by_game:
        for game in sorted(winners_by_game.keys(), key=str):
            out = winners_by_game.get(game)
            if not isinstance(out, dict):
                continue
            winners = out.get("winners") or []
            if not isinstance(winners, list) or not winners:
                continue
            podium_line = render_podium(out.get("podium"))
            if podium_line:
                lines.append(f"- {game}: {podium_line}")
                continue
            res = str(out.get("result") or "").strip().lower()
            if res == "tie":
                wtags = ", ".join(
                    f"<@{str(w.get('user_id') or '').strip()}>"
                    for w in winners
                    if isinstance(w, dict) and str(w.get("user_id") or "").strip()
                )
                if wtags:
                    lines.append(f"- {game}: tie ({wtags})")
            else:
                uid = ""
                if isinstance(winners[0], dict):
                    uid = str(winners[0].get("user_id") or "").strip()
                if uid:
                    lines.append(f"- {game}: <@{uid}>")

    return "\n".join(lines)


def _load_scores_for_day(bot_module: Any, day: str) -> List[Dict[str, Any]]:
    store = getattr(bot_module, "store", None)
    if store is None:
        return []

    fn = getattr(store, "load_scores_for_day", None)
    if callable(fn):
        try:
            recs = fn(day)
            if isinstance(recs, list):
                return [r for r in recs if isinstance(r, dict)]
        except Exception:
            pass

    scores = getattr(store, "scores", None)
    if scores is None:
        return []

    try:
        rows = scores.get_all_values()
    except Exception:
        return []

    if len(rows) <= 1:
        return []

    header = rows[0]
    if "day" not in header:
        return []
    di = header.index("day")

    out: List[Dict[str, Any]] = []
    for r in rows[1:]:
        if len(r) <= di:
            continue
        if (r[di] or "").strip() != day:
            continue
        rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
        out.append(rec)
    return out


def _normalize_game(bot_module: Any, g: str) -> str:
    s = str(g or "").strip()
    if not s:
        return ""
    fn = getattr(bot_module, "normalize_game", None)
    if callable(fn):
        try:
            out = str(fn(s) or "").strip()
            return out or s
        except Exception:
            return s
    return s[:1].upper() + s[1:]


def _score_sort_key(rec: Dict[str, Any]) -> Tuple[int, float, str]:
    try:
        value = int(str(rec.get("metric_value", "")).strip())
        return (1 if record_is_dnf(rec) else 0, float(metric_sort_value(value, rec.get("metric_type", ""))), str(rec.get("user_id", "")))
    except (TypeError, ValueError):
        pass
    for k in ("rank", "place", "pos", "score_rank"):
        v = rec.get(k)
        try:
            return (0, float(int(str(v).strip())), "")
        except Exception:
            pass

    raw = " ".join(str(rec.get(k) or "") for k in ("score_text", "score", "time", "result", "raw"))
    m = _TIME_RE.search(raw)
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        c = m.group(3)
        secs = a * 60 + b if c is None else a * 3600 + b * 60 + int(c)
        return (1, float(secs), "")

    mg = _GUESSES_RE.search(raw)
    if mg:
        return (2, float(int(mg.group(1))), "")

    mw = _WORDLE_RE.search(raw)
    if mw:
        ch = mw.group(1).upper()
        v = 7 if ch == "X" else int(ch)
        return (3, float(v), "")

    uid = str(rec.get("user_id") or rec.get("user") or "").strip()
    return (9, 0.0, uid)


def _render_scores_from_rows(bot_module: Any, day: str, rows: List[Dict[str, Any]]) -> str:
    by_game: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        g = _normalize_game(bot_module, str(r.get("game") or ""))
        if not g:
            continue
        by_game.setdefault(g, []).append(r)

    if not by_game:
        return f"*Scores for {day}*\n(No scores found.)"

    lines: List[str] = [f"*Scores for {day}*"]
    for game in sorted(by_game.keys(), key=str):
        lines.append(f"*{game}*")
        entries = sorted(by_game[game], key=_score_sort_key)
        for r in entries:
            uid = str(r.get("user_id") or r.get("user") or "").strip()
            if not uid:
                continue
            score = ""
            for k in ("display", "score_text", "score", "time", "result", "raw"):
                v = str(r.get(k) or "").strip()
                if v:
                    score = v
                    break
            if not score:
                score = "(score)"
            lines.append(f"- <@{uid}>: {score}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _render_scores_from_payload(bot_module: Any, day: str, payload: Dict[str, Any]) -> Optional[str]:
    for k in ("scores_text", "scores_block", "day_scores_text", "scores", "scoreboard_text"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for k in ("scores_lines", "day_scores_lines"):
        v = payload.get(k)
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return "\n".join([x.rstrip() for x in v]).strip()
    return None


def _build_daily_post(bot_module: Any, day: str, payload: Dict[str, Any], with_scores: bool) -> str:
    for k in ("full_post_text", "post_text", "daily_post_text"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    parts: List[str] = []
    if with_scores:
        scores_text = _render_scores_from_payload(bot_module, day, payload)
        if not scores_text:
            scores_text = _render_scores_from_rows(bot_module, day, _load_scores_for_day(bot_module, day))
        parts.append(scores_text)
    parts.append(_render_recap_from_payload(day, payload))
    return "\n\n".join([p for p in parts if p.strip()]).strip()


def _call_finalize_day(bot_module: Any, day: str, channel: str, *, post: Optional[bool], force: bool) -> str:
    fn = getattr(bot_module, "finalize_day", None)
    if not callable(fn):
        raise RuntimeError("bot_module.finalize_day() not found")

    sig = None
    try:
        sig = inspect.signature(fn)
    except Exception:
        sig = None

    kwargs: Dict[str, Any] = {}
    if sig is not None:
        params = set(sig.parameters.keys())

        if post is not None and "post" in params:
            kwargs["post"] = bool(post)

        if force:
            for k in ("force", "override", "allow_partial", "ignore_readiness", "skip_readiness"):
                if k in params:
                    kwargs[k] = True
                    break

        try:
            return str(fn(day, channel, **kwargs))
        except TypeError:
            pass

        try:
            return str(fn(day, **kwargs))
        except TypeError:
            pass

    return str(fn(day, channel))


def _looks_like_not_ready(status: str) -> bool:
    s = (status or "").strip().lower()
    return s in ("not_ready", "not ready") or "not_ready" in s or "not ready" in s


def register_recap_commands(app: Any, bot_module: Any) -> None:
    @app.command("/jw-recap")
    def _jw_recap(ack, respond, body, client, logger):  # type: ignore[no-redef]
        ack()

        denial = command_access_error(body, bot_module)
        if denial:
            respond(denial)
            return

        channel = (body.get("channel_id") or "").strip()
        user_id = (body.get("user_id") or "").strip()
        text = (body.get("text") or "").strip()

        args = _resolve_day_arg(text, bot_module)
        day = args.day

        if args.repost:
            respond(f"Okay <@{user_id}>. Reposting scores+recap for {day} (no recalculation).")
        else:
            respond(f"Okay <@{user_id}>. Finalizing {day} and posting scores+recap to <#{channel}>.")

        def worker() -> None:
            try:
                already = _day_already_posted(bot_module, day)

                if already and not args.repost and not args.force:
                    respond(
                        f"{day} is already finalized. Use `/jw-recap {day} --repost` to repost the stored recap, "
                        f"or `/jw-recap {day} --force` to re-finalize with fresh data."
                    )
                    return

                if args.repost:
                    payload = _load_daily_summary_payload(bot_module, day)
                    if not payload:
                        respond(
                            f"No stored recap found for {day}. That usually means the day was never finalized (or finalize returned not_ready).\n"
                            f"Run `/jw-recap {day}` once the day is complete."
                        )
                        return
                    post_text = _build_daily_post(bot_module, day, payload, with_scores=args.with_scores)
                    client.chat_postMessage(channel=channel, text=post_text)
                    respond(f"Reposted scores+recap for {day}.")
                    return

                status = _call_finalize_day(bot_module, day, channel, post=False, force=args.force)

                if _looks_like_not_ready(status):
                    respond(
                        f"Finalize refused for {day}: **not_ready**.\n"
                        f"Meaning: the bot decided the day isn't complete yet (missing scores / still in progress), so nothing was posted."
                    )
                    return

                payload = _load_daily_summary_payload(bot_module, day)
                if not payload:
                    respond(
                        f"Finalize ran for {day} (status={status}), but no stored summary_json was found in DailyResults.\n"
                        f"That means I have nothing to post. Check that finalize_day writes summary_json even when post=False."
                    )
                    return

                post_text = _build_daily_post(bot_module, day, payload, with_scores=args.with_scores)
                client.chat_postMessage(channel=channel, text=post_text)
                respond(f"Posted scores+recap for {day}. (status={status})")

            except Exception:
                logger.exception("/jw-recap failed")
                respond("The recap could not be completed. Contact an administrator if it continues.")

        threading.Thread(target=worker, daemon=True).start()
