#!/usr/bin/env python3
"""
nl_query_smoketest.py

Fast local harness for nl_query.py without Slack.
- Can run translate-only (to debug OpenAI API quickly)
- Or full pipeline (requires Google Sheets creds)

Usage:
  python nl_query_smoketest.py --translate-only "What’s my median Tango time this month?"
  python nl_query_smoketest.py "bot: Who wins Zip most often?" --user U111AAA
  python nl_query_smoketest.py --repl --user U111AAA
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load this checkout's .env for local testing unless the caller explicitly
# disabled dotenv loading. Existing deployment environment values take priority.
if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

import nl_query

def _print_import_info() -> None:
    print(f"nl_query_file={getattr(nl_query, '__file__', '')}")
    print(f"nl_query_version={getattr(nl_query, '__version__', '')}")


def _build_store():
    """
    Builds a minimal store object with .scores and .daily worksheets.
    Requires:
      SPREADSHEET_ID
      GOOGLE_SERVICE_ACCOUNT_FILE
    """
    sid = (os.environ.get("SPREADSHEET_ID") or "").strip()
    sa = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if not sid or not sa:
        raise RuntimeError("Missing SPREADSHEET_ID or GOOGLE_SERVICE_ACCOUNT_FILE (needed for full pipeline).")

    # Lazy imports so translate-only works in minimal env
    import gspread  # type: ignore
    from google.oauth2.service_account import Credentials  # type: ignore

    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(sa, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sid)

    class Store:
        pass

    store = Store()
    store.scores = sh.worksheet("Scores")
    store.daily = sh.worksheet("DailyResults")
    return store


def _default_games():
    # Keep consistent with your bot's game set; adjust if your tracker differs.
    return ["Wordle", "Tango", "Zip", "Pinpoint", "Queens", "Mini Sudoku", "Crossclimb", "Patches"]


def _normalize_game(name: str) -> str:
    # Mirror whatever normalization you use in the bot (case/spacing).
    return (name or "").strip()


def run_one(question: str, asker_uid: str, store, today: date) -> int:
    games = _default_games()
    ans = nl_query.answer_nl_query(
        question,
        asker_uid,
        store,
        games=games,
        normalize_game=_normalize_game,
        today=today,
    )
    print(ans)
    return 0


def translate_only(question: str, today: date) -> int:
    games = _default_games()
    q = nl_query.strip_nl_query_prefix(question)
    spec = nl_query._rule_based_translate(q, games, today=today)  # intentional: internal for debugging
    if not spec:
        spec = nl_query._openai_translate(q, games, today.isoformat())
    if not spec:
        print("translate failed")
        print(nl_query.last_openai_error())
        return 2
    print(spec)
    return 0


# Frozen-clock cases. today=2026-05-07 throughout.
# Date-range parsing cases:
_CASES = [
    ("bot: Patches consistency last 14 days",                    "placement",   "2026-04-24", "2026-05-07"),
    ("bot: Patches consistency last two weeks",                  "placement",   "2026-04-24", "2026-05-07"),
    ("bot: Patches consistency past 14 days",                    "placement",   "2026-04-24", "2026-05-07"),
    ("bot: Patches consistency over the last 14 days",           "placement",   "2026-04-24", "2026-05-07"),
    ("bot: Patches consistency last 2 weeks",                    "placement",   "2026-04-24", "2026-05-07"),
    ("bot: Patches consistency last 3 months",                   "placement",   "2026-02-07", "2026-05-07"),
    ("bot: Patches consistency range 2026-04-18 to 2026-05-07",  "placement",   "2026-04-18", "2026-05-07"),
    ("bot: Patches consistency between 2026-04-18 and 2026-05-07","placement",  "2026-04-18", "2026-05-07"),
    ("bot: Crossclimb consistency last 30 days",                 "placement",   "2026-04-08", "2026-05-07"),
    ("bot: Crossclimb consistency",                              "placement",   "1970-01-01", "2026-05-07"),
    # Synonym-layer cases (regression rows from NLQueries production data):
    ("? what are my results in Patches for the last 14 days?",   "awards", "2026-04-24", "2026-05-07"),
    ("? how'd I do in patches the last 14 days?",                "awards", "2026-04-24", "2026-05-07"),
    ("? how did I do in patches the last 2 weeks?",              "awards", "2026-04-24", "2026-05-07"),
    ("? how'd I do in patches the last 7 days?",                 "awards", "2026-05-01", "2026-05-07"),
    ("? what is <@UTEST0001>'s win frequency on patches over the last two weeks?",
                                                                 "awards", "2026-04-24", "2026-05-07"),
    ("? how did <@UTEST0001> do in patches the last 14 days?",   "awards", "2026-04-24", "2026-05-07"),
    ("? <@UTEST0001>'s performance in Tango",                    "awards", "1970-01-01", "2026-05-07"),
    ("bot: how many games have I played?",                       "game_days_played", "1970-01-01", "2026-05-07"),
    ("bot: what is my win record?",                              "awards", "1970-01-01", "2026-05-07"),
    ("bot: how many wins do I have?",                            "wins", "1970-01-01", "2026-05-07"),
    ("? How many times has <@UTEST0002> struck out with 0 wins and 0 ties?",
                                                                 "strikeouts", "1970-01-01", "2026-05-07"),
]

_CASES_GAMES = ["Wordle", "Tango", "Zip", "Pinpoint", "Queens", "Mini Sudoku", "Crossclimb", "Patches"]


def run_cases(today: date) -> int:
    failures = 0
    for question, want_measure, want_start, want_end in _CASES:
        q = nl_query.strip_nl_query_prefix(question)
        spec = nl_query._rule_based_translate(q, _CASES_GAMES, today=today)
        if not spec:
            print(f"FAIL  {question!r}: translate returned None")
            failures += 1
            continue
        try:
            spec = nl_query._validate_spec(spec, _CASES_GAMES)
        except Exception as e:
            print(f"FAIL  {question!r}: validate failed: {e}")
            failures += 1
            continue
        dr = nl_query._resolve_date_range(spec, today=today)
        got_measure = str(spec.get("measure") or "")
        got_start = dr.start.isoformat()
        got_end = dr.end.isoformat()
        ok = got_measure == want_measure and got_start == want_start and got_end == want_end
        marker = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"{marker}  {question!r}: measure={got_measure} window={got_start}..{got_end}")

    # Prefix sanity
    prefix_cases = [
        ("bot: Patches consistency",  True),
        ("?Patches consistency",       True),
        ("? Patches consistency",      True),
        ("bot, who wins Zip?",         True),
        ("Patches consistency",        False),
        ("Why did this happen?",       False),
    ]
    for text, want in prefix_cases:
        got = nl_query.is_nl_query_text(text)
        marker = "PASS" if got == want else "FAIL"
        if got != want:
            failures += 1
        print(f"{marker}  prefix {text!r}: is_nl_query={got} (want {want})")

    print(f"\n{len(_CASES) + len(prefix_cases)} cases, {failures} failure(s)")
    return 0 if failures == 0 else 1


def repl(asker_uid: str, store, today: date) -> int:
    games = _default_games()
    print("REPL: type a question, Ctrl+C to exit.")
    while True:
        try:
            q = input("> ").strip()
        except KeyboardInterrupt:
            print()
            return 0
        if not q:
            continue
        ans = nl_query.answer_nl_query(
            q,
            asker_uid,
            store,
            games=games,
            normalize_game=_normalize_game,
            today=today,
        )
        print(ans)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", default="")
    parser.add_argument("--user", default="U_TEST")
    parser.add_argument("--translate-only", action="store_true")
    parser.add_argument("--repl", action="store_true")
    parser.add_argument("--cases", action="store_true",
                        help="Run frozen-clock test cases (today=2026-05-07).")
    args = parser.parse_args()

    _print_import_info()
    today = date.today()

    if args.cases:
        return run_cases(date(2026, 5, 7))

    if args.translate_only:
        if not args.question:
            print("Provide a question string.")
            return 2
        return translate_only(args.question, today)

    if args.repl:
        store = _build_store()
        return repl(args.user, store, today)

    if not args.question:
        print("Provide a question or use --repl.")
        return 2

    store = _build_store()
    return run_one(args.question, args.user, store, today)


if __name__ == "__main__":
    raise SystemExit(main())
