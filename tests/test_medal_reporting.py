"""Tests for everything that reports medal-era results: the month wrap, recaps, weekly
summaries, the recap fallback, and natural-language answers.

The ledger here is built the way finalize_day builds it (scoring.compute_daily_winners
plus insights.build_daily_facts), then round-tripped through JSON, so these tests read
the same shapes production writes. Hand-worked expectations are in the comments.
"""

import json
import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import awards
import insights
import monthly_summary as ms
import nl_query
import recap_commands
import scoring
import weekly_summaries

# tests/conftest.py disables dotenv and sets AI_REWRITE_ENABLED=0 before imports.
# Keep this explicit setting so these tests never call OpenAI.
os.environ["AI_REWRITE_ENABLED"] = "0"


def rec(game, uid, value, *, day, tb=""):
    return {
        "day": day, "user_id": uid, "game": game, "puzzle_id": "1", "metric_type": "time",
        "metric_value": str(value), "display": f"{value // 60}:{value % 60:02d}",
        "slack_ts": "", "raw_text": "", "updated_at": "", "tiebreak_value": tb,
    }


def day_records(day, tango, zip_):
    """tango / zip_ are {user: seconds}."""
    return (
        [rec("Tango", u, v, day=day) for u, v in tango.items()]
        + [rec("Zip", u, v, day=day) for u, v in zip_.items()]
    )


def ledger_payload(day, records):
    """A DailyResults summary_json, as finalize_day writes it."""
    winners_by_game, by_user, best = scoring.compute_daily_winners(records, day=day)
    facts = insights.build_daily_facts(
        day=day, records=records, awards_by_user=by_user, best_display_by_game=best,
        expected_players=3, complete_players=3,
    )
    payload = {
        "day": day, "winners_by_game": winners_by_game, "awards_by_user": by_user,
        "best_display": best, "recap_facts": facts,
    }
    return json.loads(json.dumps(payload))


# Three players, two games. Hand-worked results:
#   Sep 28 / 29 (legacy)  Tango U1, Zip U2 win outright each day -> 1 trophy each.
#   Oct 1   Tango U1 30, U2 35, U3 40      -> U1 3, U2 2, U3 1
#           Zip   U1 13, U2 13, U3 20      -> U1 3, U2 3, U3 1 (tie for first, next is third)
#           U1 6 pts (2 gold)   U2 5 (1 gold, 1 silver)   U3 2 (2 bronze)
#   Oct 2   Tango U3 20, U1 30, U2 31      -> U3 3, U1 2, U2 1
#           Zip   U3 10, U2 11, U1 12      -> U3 3, U2 2, U1 1
#           U3 6 pts (2 gold)   U1 3 (1 silver, 1 bronze)   U2 3 (1 silver, 1 bronze)
#   October: U1 9 pts (g2 s1 b1)   U3 8 (g2 s0 b2)   U2 8 (g1 s2 b1)
#            U2 and U3 level on points; U3 is ahead on the gold count-back.
SEP28, SEP29, OCT1, OCT2 = "2026-09-28", "2026-09-29", "2026-10-01", "2026-10-02"

RECORDS = {
    SEP28: day_records(SEP28, {"U1": 30, "U2": 40}, {"U2": 20, "U1": 25}),
    SEP29: day_records(SEP29, {"U1": 30, "U2": 40}, {"U2": 20, "U1": 25}),
    OCT1: day_records(OCT1, {"U1": 30, "U2": 35, "U3": 40}, {"U1": 13, "U2": 13, "U3": 20}),
    OCT2: day_records(OCT2, {"U3": 20, "U1": 30, "U2": 31}, {"U3": 10, "U2": 11, "U1": 12}),
}


# Extra days for the wins-versus-points ranking test: A wins Tango on three September
# days (three trophies); B then wins Tango on an October day, with A second (3 and 2 points).
SEP10, SEP11, SEP12, OCT10 = "2026-09-10", "2026-09-11", "2026-09-12", "2026-10-10"
for _d in (SEP10, SEP11, SEP12):
    RECORDS[_d] = day_records(_d, {"A": 30, "B": 40}, {})
RECORDS[OCT10] = day_records(OCT10, {"B": 30, "A": 40}, {})


class _DefaultCutover(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(awards.MEDAL_SCORING_START_ENV, None)
        os.environ.pop("AI_RECAP_STYLE", None)

    @staticmethod
    def payloads(*days):
        return {d: ledger_payload(d, RECORDS[d]) for d in days}


# ---------------------------------------------------------------------------
# Month wrap
# ---------------------------------------------------------------------------
class TestMedalMonth(_DefaultCutover):
    def facts(self):
        return ms.build_monthly_facts("2026-10", self.payloads(OCT1, OCT2))

    def test_standings_rank_by_points_with_a_gold_count_back(self):
        facts = self.facts()
        self.assertEqual(facts["scoring"], "medals")
        self.assertIn("3/2/1", facts["scoring_note"])
        self.assertEqual(
            [(s["user_id"], s["place"], s["points"], s["gold"], s["silver"], s["bronze"]) for s in facts["standings"]],
            [("U1", 1, 9, 2, 1, 1), ("U3", 2, 8, 2, 0, 2), ("U2", 3, 8, 1, 2, 1)],
        )

    def test_champion_and_runner_up_are_in_points(self):
        facts = self.facts()
        self.assertEqual(facts["champion"], {
            "user_ids": ["U1"], "points": 9, "gold": 2, "silver": 1, "bronze": 1,
            "shared": False, "margin": 1,
        })
        self.assertEqual(facts["runner_up"]["user_ids"], ["U3"])
        self.assertEqual(facts["runner_up"]["points"], 8)

    def test_a_champion_level_on_points_wins_on_the_medal_count_back(self):
        # A: three golds. B: two golds, a silver and a bronze. Both 9 points.
        payload = {
            "day": OCT1, "winners_by_game": {}, "recap_facts": {"players": ["A", "B"]},
            "awards_by_user": {
                "A": {"gold": 3, "silver": 0, "bronze": 0, "points": 9},
                "B": {"gold": 2, "silver": 1, "bronze": 1, "points": 9},
            },
        }
        facts = ms.build_monthly_facts("2026-10", {OCT1: payload})
        self.assertEqual([s["user_id"] for s in facts["standings"]], ["A", "B"])
        self.assertEqual([s["place"] for s in facts["standings"]], [1, 2])
        self.assertEqual(facts["champion"]["user_ids"], ["A"])
        self.assertFalse(facts["champion"]["shared"])
        self.assertEqual(facts["champion"]["margin"], 0)
        self.assertIn("level with <@B> on points", insights.render_monthly_recap_text(facts))

    def test_points_not_golds_decide_the_title(self):
        # A has the only gold but B has more points from consistent podium finishes.
        payload = {
            "day": OCT1, "winners_by_game": {}, "recap_facts": {"players": ["A", "B"]},
            "awards_by_user": {
                "A": {"gold": 1, "silver": 0, "bronze": 0, "points": 3},
                "B": {"gold": 0, "silver": 3, "bronze": 0, "points": 6},
            },
        }
        facts = ms.build_monthly_facts("2026-10", {OCT1: payload})
        self.assertEqual([s["user_id"] for s in facts["standings"]], ["B", "A"])
        self.assertEqual(facts["champion"]["user_ids"], ["B"])
        self.assertEqual(facts["champion"]["margin"], 3)

    def test_a_cutover_inside_the_month_is_logged(self):
        # Only reachable by moving MEDAL_SCORING_START mid-month, e.g. to preview a wrap.
        with patch.dict(os.environ, {"MEDAL_SCORING_START": "2026-10-02"}):
            with self.assertLogs("monthly_summary", level="WARNING") as logs:
                ms.build_monthly_facts("2026-10", self.payloads(OCT1, OCT2))
        self.assertIn("mixes trophy-scored and medal-scored days", logs.output[0])

    def test_a_clean_month_logs_nothing(self):
        with self.assertNoLogs("monthly_summary", level="WARNING"):
            self.facts()

    def test_the_era_is_read_off_the_posted_days_not_the_first_of_the_month(self):
        # With the cut-over moved to the 15th, a month whose posted days are all
        # medal days is still a medal month, and says nothing about a mixed one.
        with patch.dict(os.environ, {"MEDAL_SCORING_START": "2026-10-15"}):
            payload = {
                "day": "2026-10-20", "winners_by_game": {}, "recap_facts": {"players": ["A", "B"]},
                "awards_by_user": {
                    "A": {"gold": 3, "silver": 0, "bronze": 0, "points": 9},
                    "B": {"gold": 0, "silver": 3, "bronze": 0, "points": 6},
                },
            }
            with self.assertNoLogs("monthly_summary", level="WARNING"):
                facts = ms.build_monthly_facts("2026-10", {"2026-10-20": payload})
        self.assertEqual(facts["scoring"], "medals")
        self.assertEqual(facts["champion"]["points"], 9)
        self.assertIn("9 pts", ms.render_monthly_standings_text(facts))

    def test_an_empty_month_falls_back_to_its_first_day(self):
        self.assertEqual(ms.build_monthly_facts("2026-10", {}).get("scoring"), "medals")
        self.assertNotIn("scoring", ms.build_monthly_facts("2026-09", {}))

    def test_players_equal_on_every_count_share_a_place(self):
        facts = ms.build_monthly_facts("2026-10", self.payloads(OCT2))
        # Oct 2 alone: U3 6 points, then U1 and U2 on 3 (a silver and a bronze each).
        self.assertEqual([(s["user_id"], s["place"]) for s in facts["standings"]],
                         [("U3", 1), ("U1", 2), ("U2", 2)])

    def test_game_champions_count_first_places_including_shared_ones(self):
        champs = self.facts()["game_champions"]
        # Tango golds: U1 (Oct 1) and U3 (Oct 2). Zip golds: U1+U2 (Oct 1, shared) and U3 (Oct 2).
        self.assertEqual(champs["Tango"], {"game": "Tango", "user_ids": ["U1", "U3"], "gold": 1})
        self.assertEqual(champs["Zip"], {"game": "Zip", "user_ids": ["U1", "U2", "U3"], "gold": 1})

    def test_best_day_is_the_most_points_in_a_day(self):
        best = self.facts()["best_day"]
        self.assertEqual(best["points"], 6)
        self.assertEqual(best["user_ids"], ["U1", "U3"])
        self.assertEqual(best["day"], OCT1)       # U1's best day, the first of the tied leaders

    def test_per_player_gold_counts_by_game(self):
        by_user = {s["user_id"]: s for s in self.facts()["standings"]}
        self.assertEqual(by_user["U1"]["gold_by_game"], {"Tango": 1, "Zip": 1})
        self.assertEqual(by_user["U2"]["gold_by_game"], {"Zip": 1})

    def test_month_records_still_use_the_winning_result(self):
        records = self.facts()["records"]
        self.assertEqual(records["Zip"]["value"], 10)
        self.assertEqual(records["Zip"]["user_ids"], ["U3"])

    def test_the_standings_post(self):
        text = ms.render_monthly_standings_text(self.facts())
        self.assertEqual(text.splitlines()[:9], [
            "*Monthly results for October 2026*",
            "_2026-10-01 to 2026-10-31, 2 scored days_",
            "",
            "*Final standings*",
            "1. <@U1> 9 pts (:first_place_medal:x2, :second_place_medal:x1, :third_place_medal:x1)",
            "2. <@U3> 8 pts (:first_place_medal:x2, :second_place_medal:x0, :third_place_medal:x2)",
            "3. <@U2> 8 pts (:first_place_medal:x1, :second_place_medal:x2, :third_place_medal:x1)",
            "",
            "*Game champions*",
        ])
        self.assertIn("- Tango: <@U1>, <@U3> (:first_place_medal:x1)", text)
        self.assertNotIn(":trophy:", text)

    def test_the_player_breakdown(self):
        facts = self.facts()
        text = ms.render_player_month_text(facts["standings"][0], facts)
        self.assertEqual(text.splitlines(), [
            "*October 2026 for <@U1>*",
            "- Finish: 1st of 3, 9 pts (:first_place_medal:x2, :second_place_medal:x1, :third_place_medal:x1)",
            "- Participation: 2/2 days",
            "- Best game: Tango (:first_place_medal:x1)",
            "- Best day: 2026-10-01 (6 pts)",
        ])

    def test_the_template_wrap_speaks_in_points(self):
        text = insights.render_monthly_recap_text(self.facts())
        self.assertIn("- Champion: <@U1> (9 pts), 1 clear of <@U3>", text)
        self.assertIn("- Best single day: <@U1>, <@U3> on 2026-10-01 (6 pts)", text)
        self.assertNotIn("trophies", text)

    def test_the_ai_must_keep_the_champions_points(self):
        facts = self.facts()
        callouts = insights._required_monthly_callouts(facts)
        self.assertIn("Champion: explicitly name <@U1> as the month champion with 9 points.", callouts)
        self.assertEqual(
            insights._missing_required_monthly_fact_labels("<@U1> took October with 9 points.", facts), [],
        )
        self.assertEqual(
            insights._missing_required_monthly_fact_labels("<@U1> took October.", facts), ["champion"],
        )

    def test_photo_finish_and_runaway_cutoffs_scale_with_points(self):
        with patch.dict(os.environ, {"AI_RECAP_STYLE": "flair"}):
            _, medal_prompt, _ = insights.build_ai_prompts("monthly recap", self.facts(), "fallback")
            legacy_facts = ms.build_monthly_facts("2026-09", self.payloads(SEP28, SEP29))
            _, legacy_prompt, _ = insights.build_ai_prompts("monthly recap", legacy_facts, "fallback")
        self.assertIn("champion.margin <= 6", medal_prompt)
        self.assertIn("champion.margin >= 30", medal_prompt)
        self.assertIn("champion.margin <= 2", legacy_prompt)
        self.assertIn("champion.margin >= 10", legacy_prompt)

    def test_the_prompt_facts_explain_the_scoring(self):
        _, prompt, _ = insights.build_ai_prompts("monthly recap", self.facts(), "fallback")
        self.assertIn("3/2/1 points", prompt)


class TestMonthOverMonth(_DefaultCutover):
    NOV = ("2026-11-01", "2026-11-02", "2026-11-03")

    def nov_payloads(self):
        # U2 wins everything, U1 is second, U3 third: 6 / 4 / 2 points a day.
        out = {}
        for d in self.NOV:
            out[d] = ledger_payload(d, day_records(
                d, {"U2": 30, "U1": 35, "U3": 40}, {"U2": 13, "U1": 14, "U3": 15},
            ))
        return out

    def test_the_cutover_month_has_no_comparison_with_the_trophy_month(self):
        facts = ms.build_monthly_facts(
            "2026-10", self.payloads(OCT1, OCT2), self.payloads(SEP28, SEP29),
        )
        self.assertIsNone(facts["prev_month"])
        self.assertIsNone(facts["biggest_mover"])
        text = ms.render_player_month_text(facts["standings"][0], facts)
        self.assertNotIn("vs ", text)

    def test_the_month_after_compares_points_with_points(self):
        facts = ms.build_monthly_facts("2026-11", self.nov_payloads(), self.payloads(OCT1, OCT2))
        self.assertEqual(facts["prev_month"], "2026-10")
        self.assertEqual(facts["biggest_mover"], {
            "user_ids": ["U2"], "delta_points": 10, "points": 18, "prev_points": 8,
        })
        entry = next(s for s in facts["standings"] if s["user_id"] == "U2")
        self.assertEqual((entry["prev_points"], entry["delta_points"]), (8, 10))
        self.assertIn("- vs October 2026: +10 pts (was 8)", ms.render_player_month_text(entry, facts))
        self.assertIn("- Biggest mover: <@U2> +10 points vs October 2026", insights.render_monthly_recap_text(facts))

    def test_a_legacy_month_is_untouched(self):
        facts = ms.build_monthly_facts("2026-09", self.payloads(SEP28, SEP29))
        self.assertNotIn("scoring", facts)
        self.assertEqual(facts["champion"]["wins"], 2)
        self.assertNotIn("points", facts["champion"])
        text = ms.render_monthly_standings_text(facts)
        self.assertIn(":trophy:x2", text)
        self.assertNotIn("pts", text)


class _MonthStore:
    """Enough of SheetStore for finalize_month, backed by in-memory payloads."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.posted_months = {}

    def load_daily_payloads_in_range(self, start_day, end_day):
        return {d: p for d, p in self.payloads.items() if start_day <= d <= end_day}

    def day_already_posted(self, day):
        return day in self.payloads

    def month_already_posted(self, month):
        return month in self.posted_months

    def mark_month_posted(self, month, summary):
        self.posted_months[month] = summary
        return True

    def replace_month_summary(self, month, summary):
        self.posted_months[month] = summary
        return True

    def update_month_summary(self, month, updates):
        return True


class _Client:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, channel, text, thread_ts=None):
        self.posts.append({"text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": f"{len(self.posts)}.000"}


class TestMonthWrapEndToEnd(_DefaultCutover):
    def test_a_medal_month_posts_standings_commentary_and_breakdowns(self):
        store = _MonthStore(self.payloads(OCT1, OCT2))
        client = _Client()
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        status = ms.finalize_month("2026-10", "C1", store, client, now_utc=now)
        self.assertEqual(status, "posted")
        texts = [p["text"] for p in client.posts]
        self.assertIn("9 pts", texts[0])
        self.assertIn("Champion: <@U1> (9 pts)", texts[1])
        self.assertEqual(len(texts), 2 + 3)                       # standings, commentary, one per player
        self.assertTrue(all("trophies" not in t and ":trophy:" not in t for t in texts))
        self.assertEqual(store.posted_months["2026-10"]["facts"]["scoring"], "medals")


# ---------------------------------------------------------------------------
# Daily recap facts
# ---------------------------------------------------------------------------
class TestDailyRecap(_DefaultCutover):
    def facts(self, day, by_user):
        return insights.build_daily_facts(
            day=day, records=[], awards_by_user=by_user, best_display_by_game={},
            expected_players=3, complete_players=3,
        )

    def test_the_mvp_is_the_most_points_not_the_most_golds(self):
        facts = self.facts(OCT1, {
            "A": {"gold": 1, "silver": 0, "bronze": 0, "points": 3},
            "B": {"gold": 0, "silver": 2, "bronze": 0, "points": 4},
        })
        self.assertEqual(facts["mvp"], {
            "user_id": "B", "user_ids": ["B"], "gold": 0, "silver": 2, "bronze": 0, "points": 4,
        })
        self.assertEqual(facts["scoring"], "medals")
        self.assertIn("3/2/1", facts["scoring_note"])
        self.assertIn("- MVP: <@B>, 4 pts (:second_place_medal:x2)", insights.render_daily_recap_text(facts))

    def test_players_level_on_points_share_the_mvp(self):
        facts = self.facts(OCT1, {
            "A": {"gold": 1, "silver": 0, "bronze": 0, "points": 3},
            "B": {"gold": 1, "silver": 0, "bronze": 0, "points": 3},
            "C": {"gold": 0, "silver": 1, "bronze": 0, "points": 2},
        })
        self.assertEqual(facts["mvp"]["user_ids"], ["A", "B"])

    def test_a_legacy_day_is_unchanged(self):
        facts = self.facts(SEP29, {"U1": {"wins": 2, "ties": 1}, "U2": {"wins": 1, "ties": 3}})
        self.assertEqual(facts["mvp"], {"user_id": "U1", "user_ids": ["U1"], "wins": 2, "ties": 1})
        self.assertNotIn("scoring", facts)
        self.assertIn("- MVP: <@U1> (:trophy:x2, :necktie:x1)", insights.render_daily_recap_text(facts))

    def test_a_day_with_no_awards_has_no_mvp(self):
        self.assertIsNone(self.facts(OCT1, {})["mvp"])

    def test_the_recap_fallback_lists_each_podium(self):
        payload = ledger_payload(OCT1, RECORDS[OCT1])
        text = recap_commands._render_recap_from_payload(OCT1, payload)
        self.assertEqual(text.splitlines(), [
            "*Recap for 2026-10-01*",
            "- Participation: 3 players",
            "- MVP: <@U1>, 6 pts (:first_place_medal:x2)",
            "- Tango: :first_place_medal: <@U1> (0:30) | :second_place_medal: <@U2> (0:35) | :third_place_medal: <@U3> (0:40)",
            "- Zip: :first_place_medal: <@U1>, <@U2> (0:13) | :third_place_medal: <@U3> (0:20)",
        ])

    def test_the_recap_fallback_for_a_legacy_day_is_unchanged(self):
        payload = ledger_payload(SEP29, RECORDS[SEP29])
        text = recap_commands._render_recap_from_payload(SEP29, payload)
        self.assertEqual(text.splitlines(), [
            "*Recap for 2026-09-29*",
            "- Participation: 2 players",
            "- MVP: <@U1>, <@U2> (:trophy:x1)",
            "- Tango: <@U1>",
            "- Zip: <@U2>",
        ])


# ---------------------------------------------------------------------------
# Weekly summaries
# ---------------------------------------------------------------------------
class TestWeeklySummaries(_DefaultCutover):
    def message(self, cur_days, prev_days, uid="U1"):
        payloads = self.payloads(*(cur_days + prev_days))
        cur = weekly_summaries._compute_period_stats(None, cur_days, payloads)
        prev = weekly_summaries._compute_period_stats(None, prev_days, payloads) if prev_days else {}
        return weekly_summaries._render_user_message(
            uid, cur_days, cur.get(uid) or weekly_summaries.UserWeek(), prev.get(uid), prev_days,
        )

    def test_a_legacy_week_keeps_its_trophy_delta(self):
        text = self.message([SEP29], [SEP28])
        self.assertIn("- Awards: :trophy:x1 (Δ +0 trophies, +0 ties)", text)

    def test_a_medal_week_reports_points_and_a_points_delta(self):
        # U1: 3 points on Oct 2 against 6 on Oct 1.
        text = self.message([OCT2], [OCT1])
        self.assertIn("- Awards: 3 pts (:second_place_medal:x1, :third_place_medal:x1) (Δ -3 pts)", text)
        self.assertIn("- Best day: 2026-10-02 (3 pts)", text)

    def test_best_game_counts_every_medal_on_a_medal_day(self):
        text = self.message([OCT1, OCT2], [])
        # U1 medalled in both games on both days.
        self.assertIn("- Best game: Zip (2 podiums)", text)

    def test_no_delta_across_the_cutover(self):
        text = self.message([OCT1], [SEP29])
        self.assertIn("- Awards: 6 pts (:first_place_medal:x2)", text)
        self.assertNotIn("Δ", text)

    def test_a_week_that_straddles_the_cutover_shows_both_and_no_delta(self):
        text = self.message([SEP29, OCT1], [SEP28])
        self.assertIn("- Awards: :trophy:x1 | 6 pts (:first_place_medal:x2)", text)
        self.assertNotIn("Δ", text)

    def test_the_ai_facts_for_a_medal_week_carry_points(self):
        seen = {}

        def fake_rewrite(kind, facts, fallback):
            seen.update(facts)
            return fallback

        with patch.object(weekly_summaries, "rewrite_text", side_effect=fake_rewrite):
            self.message([OCT2], [OCT1])
        self.assertEqual((seen["points"], seen["delta_points"], seen["gold"]), (3, -3, 0))
        self.assertEqual(seen["scoring"], "medals")
        self.assertNotIn("delta_wins", seen)


# ---------------------------------------------------------------------------
# Natural-language answers
# ---------------------------------------------------------------------------
_SCORE_HEADER = ["day", "user_id", "game", "puzzle_id", "metric_type", "metric_value", "display",
                 "slack_ts", "raw_text", "updated_at", "tiebreak_value"]


class _Sheet:
    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self):
        return self._rows


class _NLStore:
    def __init__(self, days):
        scores = [list(_SCORE_HEADER)]
        for d in days:
            for r in RECORDS[d]:
                scores.append([r[c] for c in _SCORE_HEADER])
        self.scores = _Sheet(scores)
        self.daily = _Sheet(
            [["day", "posted_at", "summary_json"]]
            + [[d, "", json.dumps(ledger_payload(d, RECORDS[d]))] for d in days]
        )
        self.monthly = _Sheet([["month", "posted_at", "summary_json"]])


class TestNLAnswers(_DefaultCutover):
    TODAY = date(2026, 10, 3)

    def ask(self, text, user="U1", days=(SEP28, SEP29, OCT1, OCT2), today=None):
        with patch.object(nl_query, "_openai_plan", return_value=None):
            return nl_query.answer_nl_query(
                text, user, _NLStore(days), games=["Tango", "Zip"],
                normalize_game=lambda g: (g or "").strip(), today=today or self.TODAY,
            )

    def test_the_points_leaderboard_ranks_by_points_then_count_back(self):
        ans = self.ask("bot: who has the most points this month?")
        lines = ans.splitlines()
        self.assertEqual(lines[0], "*All-games awards leaderboard*")
        self.assertEqual(lines[2:], [
            "1. <@U1>  9 pts (:first_place_medal:x2, :second_place_medal:x1, :third_place_medal:x1)",
            "2. <@U3>  8 pts (:first_place_medal:x2, :second_place_medal:x0, :third_place_medal:x2)",
            "3. <@U2>  8 pts (:first_place_medal:x1, :second_place_medal:x2, :third_place_medal:x1)",
        ])

    def test_points_default_to_this_month_not_last(self):
        # "Most trophies" keeps its last-month default (September); "most points" means now.
        trophies = self.ask("bot: who won the most trophies?")
        points = self.ask("bot: who has the most points?")
        self.assertIn("1. <@U1>  :trophy:x2  :necktie:x0", trophies)      # September: U1 and U2 won 2 each
        self.assertIn("pts", points)
        self.assertNotIn(":trophy:", points)

    def test_a_legacy_window_still_answers_with_trophies(self):
        ans = self.ask("bot: who won the most trophies last month?")
        self.assertIn("1. <@U1>  :trophy:x2  :necktie:x0", ans)
        self.assertIn("2. <@U2>  :trophy:x2  :necktie:x0", ans)
        self.assertNotIn("pts", ans)

    def test_how_many_points_do_i_have_covers_both_eras(self):
        ans = self.ask("bot: how many points do I have?")
        self.assertIn("*Stats summary* for <@U1>", ans)
        # Two trophies from September, then nine points of medals.
        self.assertIn(":trophy:x2 | :necktie:x0 | 9 pts (:first_place_medal:x2, :second_place_medal:x1, :third_place_medal:x1)", ans)

    def test_legacy_answers_keep_their_exact_layout(self):
        # September only (U1 won Tango and U2 won Zip on each of two days), asked about last month.
        game = self.ask("bot: how did I do in Tango last month?")
        self.assertEqual(game.splitlines(), [
            "*Tango awards* for <@U1>",
            "Range: 2026-09-01 to 2026-09-30 (finalized days scanned: 2, days with Tango: 2)",
            "- :trophy:x2  :necktie:x0",
        ])
        summary = self.ask("bot: what's my win record last month?")
        self.assertEqual(summary.splitlines()[:3], [     # a per-game block follows, which is not about awards
            "*Stats summary* for <@U1>",
            "Range: 2026-09-01 to 2026-09-30",
            "- Active days: 2 | Game-days: 4 | :trophy:x2 | :necktie:x0 | Strikeouts: 0",
        ])
        week = self.ask("bot: show my best week last month")
        self.assertEqual(week.splitlines(), [
            "*Best week* for <@U1>",
            "Range: 2026-09-01 to 2026-09-30 (finalized days)",
            "- Week: 2026-09-28 to 2026-10-04",
            "- Awards: :trophy:x2  :necktie:x0",
        ])

    def test_a_medal_best_week_is_ranked_by_points(self):
        # Oct 1-2 fall in one week. U1: 9 points across it.
        week = self.ask("bot: show my best week this month")
        self.assertEqual(week.splitlines()[2:], [
            "- Week: 2026-09-28 to 2026-10-04",
            "- Awards: 9 pts (:first_place_medal:x2, :second_place_medal:x1, :third_place_medal:x1)",
        ])

    def test_a_points_leaderboard_ranks_points_above_golds_and_a_wins_one_does_not(self):
        today = lambda uid, tally: nl_query.DailyUserFact("2026-10-01", uid, 3, tally, True)
        facts = nl_query.StatsFacts(
            scores=[], game_awards=[], payloads={}, rows_scanned=0,
            daily_users=[
                today("A", awards.AwardTally(gold=1, points=3)),
                today("B", awards.AwardTally(silver=3, points=6)),
            ],
        )
        dr = nl_query.DateRange(start=date(2026, 10, 1), end=date(2026, 10, 31))
        by_points = nl_query._format_awards_leaderboard(facts, game="", dr=dr, limit=5, by_points=True).splitlines()
        self.assertTrue(by_points[2].startswith("1. <@B>  6 pts"), by_points)
        self.assertTrue(by_points[3].startswith("2. <@A>  3 pts"), by_points)
        # "Wins" ask who won most: A has the only first place, whatever B's points.
        by_wins = nl_query._format_awards_leaderboard(facts, game="", dr=dr, limit=5).splitlines()
        self.assertTrue(by_wins[2].startswith("1. <@A>"), by_wins)
        self.assertTrue(by_wins[3].startswith("2. <@B>"), by_wins)

    def test_a_wins_leaderboard_across_the_cutover_ranks_by_wins_not_points(self):
        # A won Tango three times in September and took silver on Oct 10 (2 points);
        # B won it once, as gold (3 points). By points B would lead a "who wins Tango
        # most often?" answer, burying A's three wins.
        ans = self.ask(
            "bot: who wins Tango most often?",
            days=(SEP10, SEP11, SEP12, OCT10), today=date(2026, 10, 12),
        )
        lines = ans.splitlines()
        self.assertTrue(lines[2].startswith("1. <@A>"), lines)
        self.assertTrue(lines[3].startswith("2. <@B>"), lines)
        self.assertIn(":trophy:x3", lines[2])

    def test_the_points_question_ranks_by_points_and_honours_a_game(self):
        ans = self.ask("bot: who has the most points in Zip this month?")
        lines = ans.splitlines()
        self.assertEqual(lines[0], "*Zip wins leaderboard*")
        self.assertTrue(lines[2].startswith("1. <@U2>  5 pts"), lines)

    def test_the_wins_question_for_a_medal_month_ranks_by_first_places(self):
        # Oct 1-2 golds in Zip: U1, U2 and U3 one each, so points (U2 5, U1 4, U3 4) break it.
        lines = self.ask("bot: who wins Zip most often this month?").splitlines()
        self.assertTrue(lines[2].startswith("1. <@U2>  5 pts"), lines)

    def test_no_awards_reads_in_the_currency_of_the_window(self):
        # U9 has never played; the zero still has to speak medals in October and trophies in September.
        self.assertIn("- :first_place_medal:x0", self.ask("bot: how many wins do I have this month?", user="U9"))
        self.assertIn("- :trophy:x0", self.ask("bot: how many wins do I have last month?", user="U9"))
        self.assertIn("0 pts", self.ask("bot: how many points do I have this month?", user="U9"))
        self.assertIn(":trophy:x0 | :necktie:x0", self.ask("bot: how many points do I have last month?", user="U9"))

    def test_ties_are_retired_in_the_medal_era(self):
        ans = self.ask("bot: how many ties do I have this month?")
        self.assertIn("- :necktie:x0", ans)
        self.assertIn("Neckties ended on 2026-10-01", ans)
        self.assertNotIn("Neckties ended", self.ask("bot: how many ties do I have last month?"))

    def test_a_medal_month_wins_are_gold_medals(self):
        ans = self.ask("bot: how many wins do I have this month?")
        self.assertIn("- :first_place_medal:x2", ans)
        self.assertNotIn(":trophy:", ans)

    def test_wins_across_the_cutover_show_trophies_and_golds(self):
        ans = self.ask("bot: how many wins do I have?")
        self.assertIn("- :trophy:x2 | :first_place_medal:x2", ans)

    def test_a_strikeout_is_a_day_with_no_first_place(self):
        # U2 won Zip (shared) on Oct 1 and took no gold on Oct 2.
        ans = self.ask("bot: how many times have I struck out with 0 wins and 0 ties this month?", user="U2")
        self.assertIn("0-win, 0-tie days: 1", ans)

    def test_the_single_day_record_for_a_medal_month_is_in_points(self):
        ans = self.ask("bot: what is the highest number of games won by a single player in one day this month?")
        self.assertEqual(ans.splitlines(), [
            "*Single-day points record*",
            "Range: 2026-10-01 to 2026-10-31",
            "- Record: 6 pts (:first_place_medal:x2)",
            "- <@U1> on 2026-10-01",
            "- <@U3> on 2026-10-02",
        ])

    def test_the_single_day_record_across_the_cutover_keeps_the_eras_apart(self):
        ans = self.ask("bot: what is the highest number of games won by a single player in one day?")
        self.assertIn("*Single-day records*", ans)
        self.assertIn("Trophy days:", ans)
        self.assertIn("- Record: :trophy:x1", ans)     # one outright win a day is the most anyone had
        self.assertIn("Medal days (points):", ans)
        self.assertIn("- Record: 6 pts (:first_place_medal:x2)", ans)

    def test_a_game_leaderboard_counts_every_medal(self):
        ans = self.ask("bot: who wins Zip most often this month?")
        # Zip in October: U1 3+1, U2 3+2, U3 1+3.
        self.assertIn("1. <@U2>  5 pts", ans)
        self.assertIn("2. <@U1>  4 pts", ans)
        self.assertIn("3. <@U3>  4 pts", ans)

    def test_the_tie_rules_describe_both_eras(self):
        ans = self.ask("bot: how are ties decided?")
        self.assertIn("From 2026-10-01", ans)
        self.assertIn("3/2/1 points", ans)
        self.assertIn("If any tied player is missing tiebreak data", ans)
        self.assertIn("Before 2026-10-01", ans)
        self.assertIn("necktie", ans)

    def test_the_translator_prompt_defines_both_eras(self):
        text = nl_query._award_definitions()
        self.assertIn("Before 2026-10-01", text)
        self.assertIn("trophies", text)
        self.assertIn("3/2/1 points", text)


class TestNLRouting(_DefaultCutover):
    TODAY = date(2026, 10, 3)

    def translate(self, text):
        return nl_query._rule_based_translate(text, ["Tango", "Zip"], today=self.TODAY)

    def test_points_questions_route_to_the_awards_views(self):
        spec = self.translate("who has the most points?")
        self.assertEqual((spec["measure"], spec["aggregation"]), ("awards", "leaderboard"))
        self.assertEqual(spec["date_range"]["preset"], "this_month")
        self.assertEqual(self.translate("who has the most points in Zip?")["game"], "Zip")
        spec = self.translate("how many points do I have?")
        self.assertEqual((spec["measure"], spec["aggregation"], spec["user"]), ("awards", "summary", "me"))
        spec = self.translate("who has the most medals last month?")
        self.assertEqual(spec["date_range"]["preset"], "last_month")

    def test_trophy_questions_keep_their_last_month_default_and_rank_by_wins(self):
        spec = self.translate("who won the most trophies?")
        self.assertEqual(spec["date_range"]["preset"], "last_month")
        self.assertEqual(spec["measure"], "wins")


if __name__ == "__main__":
    unittest.main()
