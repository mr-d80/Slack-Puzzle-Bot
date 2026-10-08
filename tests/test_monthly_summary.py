"""Tests for the month-end summary: aggregation, rollover gating, idempotency."""

import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import day_utils
import insights
import monthly_summary as ms

# tests/conftest.py disables dotenv and sets AI_REWRITE_ENABLED=0 before imports.
# _openai_rewrite reads the env at call time, and this explicit setting keeps
# recap calls in this module from using OpenAI.
os.environ["AI_REWRITE_ENABLED"] = "0"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Worksheet:
    def __init__(self, rows):
        self._rows = [list(r) for r in rows]

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def append_row(self, row, value_input_option=None):
        self._rows.append(list(row))

    def update(self, values=None, range_name=None, **kwargs):
        self._rows[0] = list(values[0])


class _Store:
    """Enough of SheetStore for the monthly paths, backed by in-memory rows."""

    def __init__(self, day_payloads=None, posted_months=()):
        rows = [["day", "posted_at", "summary_json"]]
        for day, payload in sorted((day_payloads or {}).items()):
            rows.append([day, "", json.dumps(payload)])
        self.daily = _Worksheet(rows)

        mrows = [["month", "posted_at", "summary_json"]]
        for m in posted_months:
            mrows.append([m, "", json.dumps({"month": m})])
        self.monthly = _Worksheet(mrows)

        self.updates = []

    # -- reads --
    def load_daily_payloads_in_range(self, start_day, end_day):
        start = day_utils._parse_day_key_loose(start_day)
        end = day_utils._parse_day_key_loose(end_day)
        out = {}
        for r in self.daily.get_all_values()[1:]:
            d = day_utils._parse_day_key_loose(r[0])
            if not d or d < start or d > end or not r[2].strip():
                continue
            out[d.isoformat()] = json.loads(r[2])
        return out

    def day_already_posted(self, day):
        return any(
            r[0].strip() == day and r[2].strip() for r in self.daily.get_all_values()[1:]
        )

    def list_posted_months(self):
        return sorted(r[0] for r in self.monthly.get_all_values()[1:] if r[0] and r[2].strip())

    def month_already_posted(self, month):
        return month in self.list_posted_months()

    # -- writes --
    def mark_month_posted(self, month, summary):
        if self.month_already_posted(month):
            return False
        self.monthly.append_row([month, "now", json.dumps(summary)])
        return True

    def replace_month_summary(self, month, summary):
        for r in self.monthly._rows[1:]:
            if r[0] == month:
                r[2] = json.dumps(summary)
                return True
        return False

    def update_month_summary(self, month, updates):
        self.updates.append((month, updates))
        return True


class _Client:
    def __init__(self):
        self.posts = []
        self._n = 0

    def chat_postMessage(self, channel, text, thread_ts=None):
        self._n += 1
        ts = f"{self._n}.000"
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts})
        return {"ok": True, "ts": ts}


def _day_payload(day, awards, winners_by_game=None, players=None, skunk_uids=(),
                 best_display=None, skunk_game="Zip"):
    """Build a DailyResults summary_json shaped like finalize_day writes it."""
    return {
        "day": day,
        "awards_by_user": {uid: {"wins": w, "ties": t} for uid, (w, t) in awards.items()},
        "winners_by_game": winners_by_game or {},
        "best_display": best_display or {},
        "recap_facts": {
            "day": day,
            "players": list(players if players is not None else awards.keys()),
            "skunk": ({"game": skunk_game, "last_uids": list(skunk_uids), "margin": 30,
                       "metric_type": "time", "last_display": "1:30"} if skunk_uids else None),
        },
    }


def _win(uid, best_value, metric_type="time"):
    return {"result": "win", "icon": ":trophy:", "best_value": best_value,
            "winners": [{"user_id": uid, "metric_type": metric_type}]}


def _tie(uids, best_value, metric_type="time"):
    return {"result": "tie", "icon": ":necktie:", "best_value": best_value,
            "winners": [{"user_id": u, "metric_type": metric_type} for u in uids]}


# ---------------------------------------------------------------------------
# Month key helpers
# ---------------------------------------------------------------------------
class TestMonthKeys(unittest.TestCase):
    def test_month_key_and_bounds(self):
        self.assertEqual(day_utils.month_key_for_day("2026-06-14"), "2026-06")
        self.assertEqual(day_utils.month_bounds("2026-06"), ("2026-06-01", "2026-06-30"))
        self.assertEqual(day_utils.month_bounds("2026-02"), ("2026-02-01", "2026-02-28"))
        self.assertEqual(day_utils.month_bounds("2024-02"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(day_utils.month_bounds("2026-12"), ("2026-12-01", "2026-12-31"))

    def test_prev_month_key_crosses_year(self):
        self.assertEqual(day_utils.prev_month_key("2026-01"), "2025-12")
        self.assertEqual(day_utils.prev_month_key("2026-07"), "2026-06")

    def test_is_month_closed_tracks_last_day(self):
        # June 2026 closes at midnight America/Vancouver on July 1.
        just_before = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)  # 2026-06-30 23:00 PDT
        just_after = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)   # 2026-07-01 01:00 PDT
        self.assertFalse(day_utils.is_month_closed("2026-06", just_before))
        self.assertTrue(day_utils.is_month_closed("2026-06", just_after))

    def test_bad_keys_raise(self):
        with self.assertRaises(ValueError):
            day_utils.month_bounds("not-a-month")
        with self.assertRaises(ValueError):
            day_utils.month_key_for_day("")


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
class TestBuildMonthlyFacts(unittest.TestCase):
    def _payloads(self):
        return {
            "2026-06-01": _day_payload(
                "2026-06-01",
                {"U1": (2, 0), "U2": (0, 1), "U3": (0, 1)},
                winners_by_game={
                    "Wordle": _win("U1", 30),
                    "Zip": _win("U1", 45),
                    "Pinpoint": _tie(["U2", "U3"], 2, "guesses"),
                },
                best_display={"Wordle": "0:30", "Zip": "0:45", "Pinpoint": "2"},
                skunk_uids=["U3"],
            ),
            "2026-06-02": _day_payload(
                "2026-06-02",
                {"U1": (1, 0), "U2": (1, 0)},
                winners_by_game={
                    "Wordle": _win("U2", 25),
                    "Zip": _win("U1", 50),
                },
                best_display={"Wordle": "0:25", "Zip": "0:50"},
                players=["U1", "U2", "U3"],
                skunk_uids=["U3"],
            ),
        }

    def test_standings_ranking_and_totals(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        standings = {s["user_id"]: s for s in facts["standings"]}

        self.assertEqual([s["user_id"] for s in facts["standings"]], ["U1", "U2", "U3"])
        self.assertEqual((standings["U1"]["wins"], standings["U1"]["ties"]), (3, 0))
        self.assertEqual((standings["U2"]["wins"], standings["U2"]["ties"]), (1, 1))
        self.assertEqual((standings["U3"]["wins"], standings["U3"]["ties"]), (0, 1))
        self.assertEqual([s["place"] for s in facts["standings"]], [1, 2, 3])

    def test_participation_counts_days_present(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        standings = {s["user_id"]: s for s in facts["standings"]}
        self.assertEqual(standings["U1"]["days_played"], 2)
        # U3 played both days: awarded on day 1, listed in players on day 2.
        self.assertEqual(standings["U3"]["days_played"], 2)
        self.assertEqual(facts["days_counted"], 2)
        self.assertEqual(facts["perfect_attendance"], ["U1", "U2", "U3"])

    def test_champion_and_margin(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        self.assertEqual(facts["champion"]["user_ids"], ["U1"])
        self.assertEqual(facts["champion"]["wins"], 3)
        self.assertEqual(facts["champion"]["margin"], 2)
        self.assertFalse(facts["champion"]["shared"])
        self.assertEqual(facts["runner_up"]["user_ids"], ["U2"])

    def test_shared_title_shares_first_place(self):
        payloads = {
            "2026-06-01": _day_payload("2026-06-01", {"U1": (1, 0), "U2": (1, 0)}),
        }
        facts = ms.build_monthly_facts("2026-06", payloads)
        self.assertTrue(facts["champion"]["shared"])
        self.assertEqual(facts["champion"]["user_ids"], ["U1", "U2"])
        self.assertEqual([s["place"] for s in facts["standings"]], [1, 1])
        self.assertIsNone(facts["runner_up"])

    def test_game_champions_count_outright_wins_only(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        self.assertEqual(facts["game_champions"]["Zip"], {"game": "Zip", "user_ids": ["U1"], "wins": 2})
        # Wordle is 1-1, so both share it.
        self.assertEqual(sorted(facts["game_champions"]["Wordle"]["user_ids"]), ["U1", "U2"])
        # Pinpoint was only ever tied, so nobody claims it.
        self.assertNotIn("Pinpoint", facts["game_champions"])

    def test_records_keep_the_best_value_and_its_day(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        self.assertEqual(facts["records"]["Wordle"]["value"], 25)
        self.assertEqual(facts["records"]["Wordle"]["day"], "2026-06-02")
        self.assertEqual(facts["records"]["Wordle"]["display"], "0:25")
        self.assertEqual(facts["records"]["Wordle"]["user_ids"], ["U2"])
        # Zip's best is day 1's 45, not day 2's 50.
        self.assertEqual(facts["records"]["Zip"]["value"], 45)
        self.assertEqual(facts["records"]["Zip"]["day"], "2026-06-01")

    def test_skunk_king_tallies_across_the_month(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        self.assertEqual(facts["skunk_king"], {"user_ids": ["U3"], "count": 2})

    def test_deltas_against_previous_month(self):
        prev = {"2026-05-01": _day_payload("2026-05-01", {"U1": (5, 0), "U2": (0, 0)})}
        facts = ms.build_monthly_facts("2026-06", self._payloads(), prev)
        standings = {s["user_id"]: s for s in facts["standings"]}
        self.assertEqual(standings["U1"]["prev_wins"], 5)
        self.assertEqual(standings["U1"]["delta_wins"], -2)
        self.assertEqual(standings["U2"]["delta_wins"], 1)
        self.assertEqual(facts["prev_month"], "2026-05")
        self.assertEqual(facts["biggest_mover"]["user_ids"], ["U2"])
        self.assertEqual(facts["biggest_mover"]["delta_wins"], 1)

    def test_no_previous_month_means_no_mover(self):
        facts = ms.build_monthly_facts("2026-06", self._payloads())
        self.assertIsNone(facts["biggest_mover"])
        self.assertIsNone(facts["prev_month"])

    def test_empty_month_is_safe(self):
        facts = ms.build_monthly_facts("2026-06", {})
        self.assertEqual(facts["days_counted"], 0)
        self.assertEqual(facts["standings"], [])
        self.assertIsNone(facts["champion"])
        self.assertEqual(facts["perfect_attendance"], [])

    def test_legacy_int_awards_are_accepted(self):
        """Older ledger rows stored awards as a bare trophy count."""
        payloads = {"2026-06-01": {
            "day": "2026-06-01",
            "awards_by_user": {"U1": 3, "U2": 1},
            "winners_by_game": {},
            "recap_facts": {"players": ["U1", "U2"]},
        }}
        facts = ms.build_monthly_facts("2026-06", payloads)
        standings = {s["user_id"]: s for s in facts["standings"]}
        self.assertEqual(standings["U1"]["wins"], 3)
        self.assertEqual(standings["U2"]["wins"], 1)


class TestSkunkExclusions(unittest.TestCase):
    """Pinpoint's +100 DNF penalty dwarfs real last-place gaps, so it can't skunk."""

    def test_pinpoint_is_excluded_by_default(self):
        self.assertIn("Pinpoint", insights.skunk_exclude_games())

    def test_detection_skips_excluded_games(self):
        """A Pinpoint blowout loses to a smaller but legitimate Zip gap."""
        records = [
            # Pinpoint: U3 concedes (DNF = 5 guesses + 100 penalty).
            {"user_id": "U1", "game": "Pinpoint", "metric_type": "guesses",
             "metric_value": "1", "display": "1"},
            {"user_id": "U3", "game": "Pinpoint", "metric_type": "guesses",
             "metric_value": "105", "display": "DNF(5)"},
            # Zip: a real skunk, but a far smaller gap than the Pinpoint DNF.
            {"user_id": "U1", "game": "Zip", "metric_type": "time",
             "metric_value": "30", "display": "0:30"},
            {"user_id": "U2", "game": "Zip", "metric_type": "time",
             "metric_value": "75", "display": "1:15"},
        ]
        facts = insights.build_daily_facts(
            day="2026-06-01", records=records, awards_by_user={},
            best_display_by_game={}, expected_players=3, complete_players=3,
        )
        self.assertIsNotNone(facts["skunk"])
        self.assertEqual(facts["skunk"]["game"], "Zip")
        self.assertEqual(facts["skunk"]["last_uids"], ["U2"])

    def test_a_pinpoint_only_day_has_no_skunk(self):
        records = [
            {"user_id": "U1", "game": "Pinpoint", "metric_type": "guesses",
             "metric_value": "1", "display": "1"},
            {"user_id": "U3", "game": "Pinpoint", "metric_type": "guesses",
             "metric_value": "105", "display": "DNF(5)"},
        ]
        facts = insights.build_daily_facts(
            day="2026-06-01", records=records, awards_by_user={},
            best_display_by_game={}, expected_players=2, complete_players=2,
        )
        self.assertIsNone(facts["skunk"])

    def test_monthly_ignores_excluded_skunks_already_in_the_ledger(self):
        """Days finalized before the exclusion keep their Pinpoint skunk on the sheet."""
        payloads = {
            "2026-06-01": _day_payload("2026-06-01", {"U1": (1, 0)},
                                       skunk_uids=["U1"], skunk_game="Pinpoint"),
            "2026-06-02": _day_payload("2026-06-02", {"U1": (1, 0)},
                                       skunk_uids=["U1"], skunk_game="Pinpoint"),
            "2026-06-03": _day_payload("2026-06-03", {"U2": (1, 0)},
                                       skunk_uids=["U2"], skunk_game="Zip"),
        }
        facts = ms.build_monthly_facts("2026-06", payloads)
        standings = {s["user_id"]: s for s in facts["standings"]}

        self.assertEqual(standings["U1"]["skunks"], 0)  # both were Pinpoint
        self.assertEqual(standings["U2"]["skunks"], 1)
        self.assertEqual(facts["skunk_king"], {"user_ids": ["U2"], "count": 1})

    def test_clearing_the_exclusion_counts_everything_again(self):
        payloads = {
            "2026-06-01": _day_payload("2026-06-01", {"U1": (1, 0)},
                                       skunk_uids=["U1"], skunk_game="Pinpoint"),
        }
        with patch.dict(os.environ, {"SKUNK_EXCLUDE_GAMES": ""}):
            facts = ms.build_monthly_facts("2026-06", payloads)
        standings = {s["user_id"]: s for s in facts["standings"]}
        self.assertEqual(standings["U1"]["skunks"], 1)


class TestExtremes(unittest.TestCase):
    def test_picks_min_tightest_and_max_blowout_across_days(self):
        def with_races(day, tight_margin, blow_margin):
            p = _day_payload(day, {"U1": (1, 0)})
            p["recap_facts"]["tightest_race"] = {
                "game": "Wordle", "margin": tight_margin, "metric_type": "time",
                "winner_uids": ["U1"], "runner_up_uid": "U2",
            }
            p["recap_facts"]["blowout"] = {
                "game": "Zip", "margin": blow_margin, "metric_type": "time",
                "winner_uids": ["U1"], "runner_up_uid": "U2",
            }
            return p

        payloads = {
            "2026-06-01": with_races("2026-06-01", 5, 20),
            "2026-06-02": with_races("2026-06-02", 1, 60),
            "2026-06-03": with_races("2026-06-03", 9, 15),
        }
        facts = ms.build_monthly_facts("2026-06", payloads)
        self.assertEqual(facts["tightest_race"]["margin"], 1)
        self.assertEqual(facts["tightest_race"]["day"], "2026-06-02")
        self.assertEqual(facts["blowout"]["margin"], 60)
        self.assertEqual(facts["blowout"]["day"], "2026-06-02")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class TestRendering(unittest.TestCase):
    def setUp(self):
        self.facts = ms.build_monthly_facts(
            "2026-06",
            {"2026-06-01": _day_payload(
                "2026-06-01",
                {"U1": (2, 0), "U2": (0, 1), "U3": (0, 1)},
                winners_by_game={"Wordle": _win("U1", 30)},
                best_display={"Wordle": "0:30"},
                skunk_uids=["U3"],
            )},
        )

    def test_standings_text_has_every_section(self):
        text = ms.render_monthly_standings_text(self.facts)
        self.assertIn("*Monthly results for June 2026*", text)
        self.assertIn("*Final standings*", text)
        self.assertIn("1. <@U1>", text)
        self.assertIn("*Game champions*", text)
        self.assertIn("*Month records*", text)
        self.assertIn("0:30", text)
        self.assertIn("*Participation*", text)

    def test_player_text_reports_place_and_skunks(self):
        entry = {s["user_id"]: s for s in self.facts["standings"]}["U3"]
        text = ms.render_player_month_text(entry, self.facts)
        self.assertIn("<@U3>", text)
        self.assertIn("of 3", text)
        self.assertIn("Skunked: 1x", text)

    def test_ordinals(self):
        self.assertEqual(
            [ms._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)],
            ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"],
        )

    def test_recap_fallback_names_champion_and_skunk(self):
        text = insights.render_monthly_recap_text(self.facts)
        self.assertIn("Month in review: June 2026", text)
        self.assertIn("Champion: <@U1>", text)
        self.assertIn("Skunk of the month: <@U3>", text)

    def test_recap_fallback_is_stable_across_runs(self):
        self.assertEqual(
            insights.render_monthly_recap_text(self.facts),
            insights.render_monthly_recap_text(self.facts),
        )


class TestMonthlyRequiredCallouts(unittest.TestCase):
    def setUp(self):
        self.facts = {
            "champion": {"user_ids": ["U1"], "wins": 12, "ties": 0, "shared": False},
            "skunk_king": {"user_ids": ["U3"], "count": 4},
        }

    def test_complete_text_has_nothing_missing(self):
        text = "<@U1> took the crown with 12 trophies. <@U3> got skunked all month."
        self.assertEqual(insights._missing_required_monthly_fact_labels(text, self.facts), [])

    def test_missing_champion_and_skunk_are_flagged(self):
        self.assertEqual(
            insights._missing_required_monthly_fact_labels("A quiet month.", self.facts),
            ["champion", "skunk_king"],
        )

    def test_champion_needs_the_win_count_too(self):
        text = "<@U1> took the crown. <@U3> got skunked all month."
        self.assertEqual(
            insights._missing_required_monthly_fact_labels(text, self.facts), ["champion"]
        )

    def test_absent_facts_are_not_required(self):
        self.assertEqual(insights._missing_required_monthly_fact_labels("", {}), [])
        self.assertEqual(
            insights._missing_required_monthly_fact_labels(
                "", {"skunk_king": {"user_ids": ["U3"], "count": 0}}
            ),
            [],
        )

    def test_trailing_markdown_line_breaks_are_stripped(self):
        raw = "Champion line.  \n\n  \nSecond line.\t\n"
        self.assertEqual(insights._collapse_blank_lines(raw), "Champion line.\nSecond line.")

    def test_prompts_route_to_the_monthly_profile(self):
        facts = dict(self.facts, month_label="June 2026")
        sys_prompt, user_prompt, _extra = insights.build_ai_prompts("monthly recap", facts, "fallback")
        self.assertIn("month-end", sys_prompt)
        self.assertIn("June 2026", user_prompt)
        self.assertIn("<@U1>", user_prompt)  # required callout made it into the prompt


# ---------------------------------------------------------------------------
# Rollover, gating, idempotency
# ---------------------------------------------------------------------------
_AFTER_JUNE = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
_DURING_JUNE = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
# 2026-06-30 20:00 America/Vancouver: the last day of June is done, but June
# itself has not closed on the clock (it closes at 2026-07-01 07:00 UTC).
_EVENING_OF_JUNE_30 = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)

_JUNE_DAYS = {
    "2026-06-01": _day_payload("2026-06-01", {"U1": (2, 0), "U2": (0, 1)},
                               winners_by_game={"Wordle": _win("U1", 30)},
                               best_display={"Wordle": "0:30"}),
    "2026-06-02": _day_payload("2026-06-02", {"U2": (1, 0), "U1": (0, 0)}),
}


def _june_store(**kwargs):
    """June with its final day NOT yet posted -> only the clock can finish it."""
    return _Store(dict(_JUNE_DAYS), **kwargs)


def _june_through_the_30th_store(**kwargs):
    """June with its final day posted -> final regardless of the clock."""
    days = dict(_JUNE_DAYS)
    days["2026-06-30"] = _day_payload("2026-06-30", {"U1": (1, 0), "U2": (1, 0)})
    return _Store(days, **kwargs)


class TestFinalizeMonth(unittest.TestCase):
    def test_posts_standings_recap_and_breakdowns(self):
        store, client = _june_store(), _Client()
        status = ms.finalize_month("2026-06", "C1", store, client, now_utc=_AFTER_JUNE)

        self.assertEqual(status, "posted")
        self.assertTrue(store.month_already_posted("2026-06"))

        # parent + recap + one breakdown per player
        self.assertEqual(len(client.posts), 4)
        self.assertIsNone(client.posts[0]["thread_ts"])
        self.assertIn("*Monthly results for June 2026*", client.posts[0]["text"])
        for p in client.posts[1:]:
            self.assertEqual(p["thread_ts"], client.posts[0]["ts"])
        self.assertIn("Month in review", client.posts[1]["text"])
        self.assertEqual(store.updates[-1][1]["slack_month_results_ts"], client.posts[0]["ts"])

    def test_unfinished_month_is_not_finalized(self):
        store, client = _june_store(), _Client()
        self.assertEqual(
            ms.finalize_month("2026-06", "C1", store, client, now_utc=_DURING_JUNE), "not_final"
        )
        self.assertEqual(client.posts, [])
        self.assertFalse(store.month_already_posted("2026-06"))

    def test_already_posted_month_is_skipped(self):
        store, client = _june_store(posted_months=["2026-06"]), _Client()
        self.assertEqual(
            ms.finalize_month("2026-06", "C1", store, client, now_utc=_AFTER_JUNE), "already_posted"
        )
        self.assertEqual(client.posts, [])

    def test_force_replaces_instead_of_appending(self):
        store, client = _june_store(posted_months=["2026-06"]), _Client()
        before = len(store.monthly.get_all_values())
        status = ms.finalize_month(
            "2026-06", "C1", store, client, force=True, now_utc=_AFTER_JUNE
        )
        self.assertEqual(status, "posted")
        self.assertEqual(len(store.monthly.get_all_values()), before)
        self.assertTrue(client.posts)

    def test_month_without_scored_days_is_not_posted(self):
        store, client = _Store({}), _Client()
        self.assertEqual(
            ms.finalize_month("2026-06", "C1", store, client, now_utc=_AFTER_JUNE), "no_days"
        )
        self.assertEqual(client.posts, [])
        self.assertFalse(store.month_already_posted("2026-06"))

    def test_ledger_is_claimed_before_posting(self):
        """A Slack failure must not leave the month unclaimed and repostable."""
        store = _june_store()

        class _Boom:
            def chat_postMessage(self, **kwargs):
                raise RuntimeError("slack down")

        with self.assertRaises(RuntimeError):
            ms.finalize_month("2026-06", "C1", store, _Boom(), now_utc=_AFTER_JUNE)
        self.assertTrue(store.month_already_posted("2026-06"))

    def test_breakdowns_can_be_disabled(self):
        store, client = _june_store(), _Client()
        with patch.object(ms, "POST_MONTHLY_PLAYER_BREAKDOWNS", False):
            ms.finalize_month("2026-06", "C1", store, client, now_utc=_AFTER_JUNE)
        self.assertEqual(len(client.posts), 2)  # standings + recap only


class TestMonthIsFinal(unittest.TestCase):
    """A month finishes when its last day is in the books, or when the clock says so."""

    def test_final_day_posted_beats_the_clock(self):
        store = _june_through_the_30th_store()
        # Still June on the wall clock, but June 30 is already finalized.
        self.assertFalse(day_utils.is_month_closed("2026-06", _EVENING_OF_JUNE_30))
        self.assertTrue(ms.month_is_final(store, "2026-06", _EVENING_OF_JUNE_30))

    def test_clock_is_the_backstop_when_the_final_day_never_posted(self):
        store = _june_store()
        self.assertFalse(ms.month_is_final(store, "2026-06", _EVENING_OF_JUNE_30))
        self.assertTrue(ms.month_is_final(store, "2026-06", _AFTER_JUNE))

    def test_mid_month_is_not_final(self):
        self.assertFalse(ms.month_is_final(_june_store(), "2026-06", _DURING_JUNE))
        self.assertFalse(ms.month_is_final(_june_through_the_30th_store(), "2026-07", _DURING_JUNE))


class TestDueMonths(unittest.TestCase):
    def test_closed_unposted_month_is_due(self):
        self.assertEqual(ms.due_months(_june_store(), _AFTER_JUNE), ["2026-06"])

    def test_open_month_is_not_due(self):
        self.assertEqual(ms.due_months(_june_store(), _DURING_JUNE), [])

    def test_month_whose_final_day_posted_is_due_before_it_closes(self):
        self.assertEqual(
            ms.due_months(_june_through_the_30th_store(), _EVENING_OF_JUNE_30), ["2026-06"]
        )

    def test_due_months_agrees_with_month_is_final_for_recent_months(self):
        for store in (_june_store(), _june_through_the_30th_store()):
            for now in (_DURING_JUNE, _EVENING_OF_JUNE_30, _AFTER_JUNE):
                expected = ["2026-06"] if ms.month_is_final(store, "2026-06", now) else []
                self.assertEqual(ms.due_months(store, now), expected)

    def test_posted_month_is_not_due(self):
        self.assertEqual(ms.due_months(_june_store(posted_months=["2026-06"]), _AFTER_JUNE), [])


class TestBackCatalogueGuard(unittest.TestCase):
    """MonthlyResults starts empty, so every old month looks unposted on day one."""

    def _history(self):
        return _Store({
            "2026-03-01": _day_payload("2026-03-01", {"U1": (1, 0)}),
            "2026-04-01": _day_payload("2026-04-01", {"U1": (1, 0)}),
            "2026-05-01": _day_payload("2026-05-01", {"U1": (1, 0)}),
            "2026-06-01": _day_payload("2026-06-01", {"U1": (1, 0)}),
        })

    def test_only_the_just_finished_month_is_due(self):
        # Every one of these months is final and unposted, but only June is news.
        store = self._history()
        for m in ("2026-03", "2026-04", "2026-05", "2026-06"):
            self.assertTrue(ms.month_is_final(store, m, _AFTER_JUNE), m)
        self.assertEqual(ms.due_months(store, _AFTER_JUNE), ["2026-06"])

    def test_first_rollover_does_not_flood_the_channel(self):
        store, client = self._history(), _Client()
        self.assertEqual(
            ms.finalize_due_months("C1", store, client, now_utc=_AFTER_JUNE), ["2026-06"]
        )
        self.assertTrue(client.posts)
        self.assertTrue(all("June 2026" in p["text"] for p in client.posts))
        for stale in ("2026-03", "2026-04", "2026-05"):
            self.assertFalse(store.month_already_posted(stale), stale)

    def test_skipped_months_are_left_unclaimed_for_manual_posting(self):
        store, client = self._history(), _Client()
        ms.finalize_due_months("C1", store, client, now_utc=_AFTER_JUNE)

        # Not silently claimed, so --month can still post it deliberately.
        self.assertEqual(
            ms.finalize_month("2026-04", "C1", store, client, now_utc=_AFTER_JUNE), "posted"
        )

    def test_age_is_measured_from_the_month_end(self):
        self.assertEqual(ms.month_age_days("2026-06", _AFTER_JUNE), 2)
        self.assertEqual(ms.month_age_days("2026-06", _EVENING_OF_JUNE_30), 0)
        self.assertEqual(ms.month_age_days("2026-05", _AFTER_JUNE), 32)
        self.assertLess(ms.month_age_days("2026-06", _DURING_JUNE), 0)  # still running

    def test_zero_disables_the_limit(self):
        with patch.object(ms, "MONTHLY_SUMMARY_MAX_AGE_DAYS", 0):
            self.assertEqual(
                ms.due_months(self._history(), _AFTER_JUNE),
                ["2026-03", "2026-04", "2026-05", "2026-06"],
            )

    def test_a_wider_window_lets_more_through(self):
        with patch.object(ms, "MONTHLY_SUMMARY_MAX_AGE_DAYS", 40):
            self.assertEqual(ms.due_months(self._history(), _AFTER_JUNE), ["2026-05", "2026-06"])

    def test_deploying_mid_month_stays_quiet_then_wraps_that_month(self):
        """Deploy on 2026-07-29 against an existing ledger; July ends on the 31st."""
        days = {
            "2026-05-01": _day_payload("2026-05-01", {"U1": (1, 0)}),
            "2026-06-01": _day_payload("2026-06-01", {"U1": (1, 0)}),
            "2026-06-30": _day_payload("2026-06-30", {"U1": (1, 0)}),
            "2026-07-29": _day_payload("2026-07-29", {"U1": (1, 0), "U2": (1, 0)}),
        }
        store, client = _Store(dict(days)), _Client()

        # Deploy day: June is final but 29 days stale, July is still running.
        deploy_day = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)  # 2026-07-29 20:00 PDT
        self.assertEqual(ms.finalize_due_months("C1", store, client, now_utc=deploy_day), [])
        self.assertEqual(client.posts, [])

        # The 31st finalizes -> July is final at age 0 and wraps that evening,
        # without waiting for August and without dragging June along.
        days["2026-07-31"] = _day_payload("2026-07-31", {"U1": (2, 0), "U2": (0, 1)})
        store, client = _Store(dict(days)), _Client()
        month_end = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)  # 2026-07-31 20:00 PDT

        self.assertEqual(
            ms.finalize_due_months("C1", store, client, now_utc=month_end), ["2026-07"]
        )
        self.assertIn("*Monthly results for July 2026*", client.posts[0]["text"])
        self.assertFalse(store.month_already_posted("2026-06"))


class TestFinalizeDueMonths(unittest.TestCase):
    def test_wraps_on_the_final_day_without_waiting_for_the_new_month(self):
        """The gap-closing path: June 30 finalizes, June wraps the same evening."""
        store, client = _june_through_the_30th_store(), _Client()
        self.assertEqual(
            ms.finalize_due_months("C1", store, client, now_utc=_EVENING_OF_JUNE_30), ["2026-06"]
        )
        self.assertIn("*Monthly results for June 2026*", client.posts[0]["text"])

        # And July, still in progress, is left alone.
        self.assertFalse(store.month_already_posted("2026-07"))

    def test_posts_once_and_not_again(self):
        store, client = _june_store(), _Client()

        self.assertEqual(
            ms.finalize_due_months("C1", store, client, now_utc=_AFTER_JUNE), ["2026-06"]
        )
        posts_after_first = len(client.posts)
        self.assertTrue(posts_after_first)

        # Re-running (as it does on every finalized day) must be a no-op.
        self.assertEqual(ms.finalize_due_months("C1", store, client, now_utc=_AFTER_JUNE), [])
        self.assertEqual(len(client.posts), posts_after_first)

    def test_feature_flag_disables_the_rollover(self):
        store, client = _june_store(), _Client()
        with patch.object(ms, "POST_MONTHLY_SUMMARY", False):
            self.assertEqual(ms.finalize_due_months("C1", store, client, now_utc=_AFTER_JUNE), [])
        self.assertEqual(client.posts, [])
        self.assertFalse(store.month_already_posted("2026-06"))

    def test_one_bad_month_does_not_block_the_others(self):
        store, client = _Store({
            "2026-05-01": _day_payload("2026-05-01", {"U1": (1, 0)}),
            "2026-06-01": _day_payload("2026-06-01", {"U1": (1, 0)}),
        }), _Client()

        real = ms.finalize_month

        def flaky(month, *args, **kwargs):
            if month == "2026-05":
                raise RuntimeError("boom")
            return real(month, *args, **kwargs)

        with patch.object(ms, "finalize_month", side_effect=flaky):
            self.assertEqual(
                ms.finalize_due_months("C1", store, client, now_utc=_AFTER_JUNE), ["2026-06"]
            )
        self.assertTrue(store.month_already_posted("2026-06"))
        self.assertFalse(store.month_already_posted("2026-05"))


# ---------------------------------------------------------------------------
# The daily -> monthly hand-off in finalization.py
# ---------------------------------------------------------------------------
class TestRolloverHook(unittest.TestCase):
    class _DayStore:
        def __init__(self, days, posted=()):
            self._days = list(days)
            self._posted = set(posted)

        def list_days_with_scores(self):
            return list(self._days)

        def day_already_posted(self, day):
            return day in self._posted

        def mark(self, day):
            self._posted.add(day)

    def _run(self, store, day_status):
        import finalization

        def fake_finalize_day(day, channel, s, c, post=True, now_utc=None, **kwargs):
            store.mark(day)
            return day_status.get(day, "not_ready")

        with patch.object(finalization, "finalize_day", side_effect=fake_finalize_day), \
             patch.object(finalization, "finalize_due_months") as months:
            finalization._finalize_due_days_inner("C1", store, _Client(), post=True)
        return months

    def test_month_check_runs_after_a_day_posts(self):
        store = self._DayStore(["2026-07-01"])
        months = self._run(store, {"2026-07-01": "posted"})
        months.assert_called_once()
        self.assertEqual(months.call_args.args[0], "C1")

    def test_month_check_is_skipped_when_nothing_was_finalized(self):
        store = self._DayStore(["2026-07-01"])
        months = self._run(store, {"2026-07-01": "not_ready"})
        months.assert_not_called()

    def test_month_check_is_skipped_when_all_days_already_posted(self):
        store = self._DayStore(["2026-07-01"], posted=["2026-07-01"])
        months = self._run(store, {})
        months.assert_not_called()


# ---------------------------------------------------------------------------
# The real SheetStore ledger methods, against fake worksheets
# ---------------------------------------------------------------------------
class _LedgerWorksheet(_Worksheet):
    """_Worksheet plus the single-cell update SheetStore uses for patches."""

    def __init__(self, rows):
        super().__init__(rows)
        self.title = "MonthlyResults"

    def row_values(self, n):
        return list(self._rows[n - 1]) if len(self._rows) >= n else []

    def update(self, values=None, range_name=None, **kwargs):
        # "A1" -> header row; "C3" -> single cell.
        col = ord(range_name[0]) - ord("A")
        row = int(range_name[1:]) - 1
        if row == 0 and len(values[0]) > 1:
            self._rows[0] = list(values[0])
            return
        while len(self._rows[row]) <= col:
            self._rows[row].append("")
        self._rows[row][col] = values[0][0]


def _bare_store(rows):
    """A SheetStore with only what the monthly ledger methods touch."""
    from sheet_store import SheetStore
    import threading as _threading

    store = SheetStore.__new__(SheetStore)
    store.monthly = _LedgerWorksheet(rows)
    store._write_lock = _threading.RLock()
    store._sheets_write_max_retries = 1
    store._sheets_write_base_delay_s = 0.0
    store._sheets_write_max_delay_s = 0.0
    store._sheets_429_max_retries = 1
    store._sheets_429_base_delay_s = 0.0
    store._sheets_429_max_delay_s = 0.0
    return store


class TestSheetStoreMonthlyLedger(unittest.TestCase):
    HEADER = ["month", "posted_at", "summary_json"]

    def test_mark_month_posted_appends_once(self):
        store = _bare_store([self.HEADER])

        self.assertTrue(store.mark_month_posted("2026-06", {"month": "2026-06"}))
        self.assertTrue(store.month_already_posted("2026-06"))

        # Second call is the duplicate-claim guard.
        self.assertFalse(store.mark_month_posted("2026-06", {"month": "2026-06"}))
        self.assertEqual(len(store.monthly.get_all_values()), 2)

    def test_month_with_blank_summary_is_not_posted(self):
        store = _bare_store([self.HEADER, ["2026-06", "now", ""]])
        self.assertFalse(store.month_already_posted("2026-06"))
        self.assertEqual(store.list_posted_months(), [])
        # ...and can still be claimed.
        self.assertTrue(store.mark_month_posted("2026-06", {"month": "2026-06"}))

    def test_list_posted_months_is_sorted_and_deduped(self):
        store = _bare_store([
            self.HEADER,
            ["2026-07", "now", "{}"],
            ["2026-05", "now", "{}"],
            ["2026-07", "now", "{}"],
        ])
        self.assertEqual(store.list_posted_months(), ["2026-05", "2026-07"])

    def test_update_month_summary_patches_without_dropping_keys(self):
        store = _bare_store([self.HEADER])
        store.mark_month_posted("2026-06", {"month": "2026-06", "standings_text": "keep me"})

        self.assertTrue(store.update_month_summary("2026-06", {"slack_month_results_ts": "1.5"}))

        payload = json.loads(store.monthly.get_all_values()[1][2])
        self.assertEqual(payload["slack_month_results_ts"], "1.5")
        self.assertEqual(payload["standings_text"], "keep me")

    def test_update_and_replace_on_a_missing_month_report_failure(self):
        store = _bare_store([self.HEADER])
        self.assertFalse(store.update_month_summary("2026-06", {"x": 1}))
        self.assertFalse(store.replace_month_summary("2026-06", {"x": 1}))

    def test_replace_month_summary_overwrites_in_place(self):
        store = _bare_store([self.HEADER])
        store.mark_month_posted("2026-06", {"month": "2026-06", "standings_text": "old"})

        self.assertTrue(store.replace_month_summary("2026-06", {"month": "2026-06", "standings_text": "new"}))

        rows = store.monthly.get_all_values()
        self.assertEqual(len(rows), 2)
        self.assertEqual(json.loads(rows[1][2])["standings_text"], "new")

    def test_header_is_repaired_when_missing(self):
        store = _bare_store([[]])
        self.assertTrue(store.mark_month_posted("2026-06", {"month": "2026-06"}))
        self.assertEqual(store.monthly.get_all_values()[0], self.HEADER)
        self.assertTrue(store.month_already_posted("2026-06"))


if __name__ == "__main__":
    unittest.main()
