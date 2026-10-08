import unittest
import json
from datetime import date
from unittest.mock import patch

import insights
import nl_query


_GAMES = ["Wordle", "Tango", "Zip", "Pinpoint", "Queens", "Mini Sudoku", "Crossclimb", "Patches"]


class _FakeWorksheet:
    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self):
        return self._rows


class _FakeStore:
    def __init__(self):
        self.scores = _FakeWorksheet([
            ["day", "user_id", "game", "puzzle_id", "metric_type", "metric_value", "display", "slack_ts", "raw_text", "updated_at", "tiebreak_value"],
            ["2026-05-01", "U1", "Patches", "1", "time", "10", "0:10", "", "", "", ""],
            ["2026-05-01", "U1", "Tango", "1", "time", "60", "1:00", "", "", "", ""],
            ["2026-05-01", "U2", "Patches", "1", "time", "20", "0:20", "", "", "", ""],
            ["2026-05-01", "U2", "Tango", "1", "time", "50", "0:50", "", "", "", ""],
            ["2026-05-02", "U1", "Patches", "2", "time", "30", "0:30", "", "", "", ""],
            ["2026-05-02", "U1", "Tango", "2", "time", "70", "1:10", "", "", "", ""],
            ["2026-05-02", "U2", "Patches", "2", "time", "20", "0:20", "", "", "", ""],
            ["2026-05-02", "U2", "Tango", "2", "time", "65", "1:05", "", "", "", ""],
            ["2026-05-03", "U1", "Patches", "3", "time", "15", "0:15", "", "", "", ""],
            ["2026-05-03", "U2", "Patches", "3", "time", "25", "0:25", "", "", "", ""],
        ])
        payloads = {
            "2026-05-01": {
                "winners_by_game": {
                    "Patches": {"result": "win", "winners": [{"user_id": "U1"}]},
                    "Tango": {"result": "win", "winners": [{"user_id": "U2"}]},
                },
                "awards_by_user": {"U1": {"wins": 1, "ties": 0}, "U2": {"wins": 1, "ties": 0}},
            },
            "2026-05-02": {
                "winners_by_game": {
                    "Patches": {"result": "win", "winners": [{"user_id": "U2"}]},
                    "Tango": {"result": "win", "winners": [{"user_id": "U2"}]},
                },
                "awards_by_user": {"U2": {"wins": 2, "ties": 0}},
            },
            "2026-05-03": {
                "winners_by_game": {
                    "Patches": {"result": "win", "winners": [{"user_id": "U1"}]},
                },
                "awards_by_user": {"U1": {"wins": 1, "ties": 0}},
            },
        }
        self.daily = _FakeWorksheet([
            ["day", "posted_at", "summary_json"],
            *[[day, "", json.dumps(payload)] for day, payload in payloads.items()],
        ])
        self.monthly = _FakeWorksheet([
            ["month", "posted_at", "summary_json"],
            ["2026-04", "", json.dumps({"champion": {"user_ids": ["U11111111"], "wins": 12}})],
        ])


class NLQueryPrefixTests(unittest.TestCase):
    def test_bot_prefix_still_triggers_nl_queries(self) -> None:
        self.assertTrue(nl_query.is_nl_query_text("bot: Who wins Zip most often?"))

    def test_question_mark_prefix_triggers_nl_queries(self) -> None:
        self.assertTrue(nl_query.is_nl_query_text("? Who wins Zip most often?"))
        self.assertTrue(nl_query.is_nl_query_text("?Patches consistency"))
        self.assertTrue(nl_query.is_nl_query_text("bot, who wins Zip most often?"))

    def test_plain_questions_do_not_trigger_nl_queries(self) -> None:
        self.assertFalse(nl_query.is_nl_query_text("Why did this happen?"))
        self.assertFalse(nl_query.is_nl_query_text("Patches consistency last 14 days"))

    def test_strip_nl_query_prefix_removes_question_mark(self) -> None:
        self.assertEqual(
            nl_query.strip_nl_query_prefix("?Patches consistency"),
            "Patches consistency",
        )
        self.assertEqual(
            nl_query.strip_nl_query_prefix("? Patches consistency last 14 days"),
            "Patches consistency last 14 days",
        )


class NLQueryDateRangeTests(unittest.TestCase):
    TODAY = date(2026, 5, 7)

    def _translate(self, text: str) -> dict:
        spec = nl_query._rule_based_translate(text, _GAMES, today=self.TODAY)
        self.assertIsNotNone(spec, f"rule-based translator returned None for {text!r}")
        return spec

    def test_relative_window_extracts_explicit_dates(self) -> None:
        spec = self._translate("Patches consistency last 14 days")
        self.assertEqual(spec["schema_version"], "stats_query_v1")
        self.assertEqual(spec["measure"], "placement")
        self.assertEqual(spec["aggregation"], "leaderboard")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_relative_window_word_form(self) -> None:
        spec = self._translate("Patches consistency last two weeks")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_relative_window_past_phrasing(self) -> None:
        spec = self._translate("Patches consistency past 14 days")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_relative_window_over_the_last(self) -> None:
        spec = self._translate("Patches consistency over the last 14 days")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_relative_window_months(self) -> None:
        spec = self._translate("Patches consistency last 3 months")
        self.assertEqual(spec["date_range"]["start"], "2026-02-07")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_explicit_iso_range_extracts_dates(self) -> None:
        spec = self._translate("Patches consistency range 2026-04-18 to 2026-05-07")
        self.assertEqual(spec["date_range"]["start"], "2026-04-18")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_explicit_iso_between(self) -> None:
        spec = self._translate("Patches consistency between 2026-04-18 and 2026-05-07")
        self.assertEqual(spec["date_range"]["start"], "2026-04-18")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_known_preset_still_works(self) -> None:
        spec = self._translate("Crossclimb consistency last 30 days")
        self.assertEqual(spec["date_range"]["start"], "2026-04-08")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_no_window_means_all_time(self) -> None:
        spec = self._translate("Crossclimb consistency")
        self.assertEqual(spec["date_range"]["preset"], "all_time")
        self.assertEqual(spec["date_range"]["start"], "")
        self.assertEqual(spec["date_range"]["end"], "")

    def test_unrecognized_window_falls_through_to_all_time(self) -> None:
        spec = self._translate("Patches consistency last fortnight")
        self.assertEqual(spec["date_range"]["preset"], "all_time")
        self.assertEqual(spec["date_range"]["start"], "")
        self.assertEqual(spec["date_range"]["end"], "")

    def test_unprefixed_iso_range_extracts_dates(self) -> None:
        spec = self._translate("How many zip games did I win 2026-06-01 to 2026-06-30?")
        self.assertEqual(spec["date_range"]["start"], "2026-06-01")
        self.assertEqual(spec["date_range"]["end"], "2026-06-30")

    def test_named_month_uses_most_recent_occurrence(self) -> None:
        spec = nl_query._rule_based_translate("How many zip games did I win in June?", _GAMES, today=date(2026, 7, 1))
        self.assertEqual(spec["date_range"]["start"], "2026-06-01")
        self.assertEqual(spec["date_range"]["end"], "2026-06-30")


class NLQuerySynonymTests(unittest.TestCase):
    """
    Covers the natural phrasings users actually tried in production that the
    LLM was returning intent=unsupported for. Each test is a phrasing that must
    route to user_game_awards via the rule-based path (no LLM).
    """
    TODAY = date(2026, 5, 7)

    def _translate(self, text: str) -> dict:
        spec = nl_query._rule_based_translate(text, _GAMES, today=self.TODAY)
        self.assertIsNotNone(spec, f"rule-based translator returned None for {text!r}")
        return spec

    def test_how_did_i_do(self) -> None:
        spec = self._translate("how'd I do in patches the last 14 days?")
        self.assertEqual(spec["schema_version"], "stats_query_v1")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["aggregation"], "summary")
        self.assertEqual(spec["game"], "Patches")
        self.assertEqual(spec["user"], "me")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_how_did_i_do_word_form(self) -> None:
        spec = self._translate("how did I do in patches the last 2 weeks?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["user"], "me")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")

    def test_how_am_i_doing(self) -> None:
        spec = self._translate("how am I doing in patches?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["user"], "me")

    def test_what_are_my_results(self) -> None:
        spec = self._translate("what are my results in Patches for the last 14 days?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["game"], "Patches")
        self.assertEqual(spec["user"], "me")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")

    def test_my_performance(self) -> None:
        spec = self._translate("my performance in Tango")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["game"], "Tango")
        self.assertEqual(spec["user"], "me")

    def test_win_frequency(self) -> None:
        spec = self._translate("what is the win frequency on patches over the last two weeks?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["game"], "Patches")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")

    # --- Mention-aware variants: <@user>'s ... ---

    def test_mention_results(self) -> None:
        spec = self._translate("what are <@UTEST0001>'s results in Patches for the last 14 days?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["game"], "Patches")
        self.assertEqual(spec["user"], "UTEST0001")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")

    def test_mention_win_frequency(self) -> None:
        spec = self._translate("what is <@UTEST0001>'s win frequency on patches over the last two weeks?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["user"], "UTEST0001")
        self.assertEqual(spec["date_range"]["start"], "2026-04-24")
        self.assertEqual(spec["date_range"]["end"], "2026-05-07")

    def test_mention_how_did(self) -> None:
        spec = self._translate("how did <@UTEST0001> do in patches the last 14 days?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["user"], "UTEST0001")

    def test_mention_performance(self) -> None:
        spec = self._translate("<@UTEST0001>'s performance in Tango")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["game"], "Tango")
        self.assertEqual(spec["user"], "UTEST0001")

    # --- Mention-aware variants on existing intents ---

    def test_mention_consistency_routes_to_placement(self) -> None:
        spec = self._translate("<@UTEST0001>'s consistency on Patches")
        self.assertEqual(spec["measure"], "placement")
        self.assertEqual(spec["aggregation"], "distribution")
        self.assertEqual(spec["user"], "UTEST0001")
        self.assertEqual(spec["game"], "Patches")

    def test_mention_user_stat(self) -> None:
        spec = self._translate("<@UTEST0001>'s median Tango time")
        self.assertEqual(spec["measure"], "score_value")
        self.assertEqual(spec["user"], "UTEST0001")
        self.assertEqual(spec["stat"], "median")

    def test_mention_placement_distribution(self) -> None:
        spec = self._translate("<@UTEST0001>'s placement distribution")
        self.assertEqual(spec["measure"], "placement")
        self.assertEqual(spec["aggregation"], "distribution")
        self.assertEqual(spec["user"], "UTEST0001")

    def test_target_user_resolver_prefers_mention_over_first_person(self) -> None:
        # If both 'I' and a mention appear, the explicit mention wins.
        self.assertEqual(
            nl_query._resolve_target_user("how did I and <@UTEST0001> do in Patches?"),
            "UTEST0001",
        )

    def test_target_user_resolver_two_mentions_is_ambiguous(self) -> None:
        self.assertEqual(
            nl_query._resolve_target_user("how did <@U1> and <@U2> do in Patches?"),
            "",
        )


class NLQueryGeneralStatsTests(unittest.TestCase):
    TODAY = date(2026, 5, 26)

    def _translate(self, text: str) -> dict:
        spec = nl_query._rule_based_translate(text, _GAMES, today=self.TODAY)
        self.assertIsNotNone(spec, f"rule-based translator returned None for {text!r}")
        return nl_query._validate_spec(spec, _GAMES)

    def test_games_played_translates_to_game_days_count(self) -> None:
        spec = self._translate("how many games have I played?")
        self.assertEqual(spec["schema_version"], "stats_query_v1")
        self.assertEqual(spec["measure"], "game_days_played")
        self.assertEqual(spec["aggregation"], "count")
        self.assertEqual(spec["user"], "me")

    def test_win_record_translates_to_awards_summary(self) -> None:
        spec = self._translate("what's my win record?")
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["aggregation"], "summary")
        self.assertEqual(spec["output"], "summary")

    def test_wins_translates_to_wins_sum(self) -> None:
        spec = self._translate("how many wins do I have?")
        self.assertEqual(spec["measure"], "wins")
        self.assertEqual(spec["aggregation"], "sum")

    def test_strikeout_query_translates_with_zero_award_filters(self) -> None:
        spec = self._translate("How many times has <@U11111111> struck out with 0 wins and 0 ties?")
        self.assertEqual(spec["measure"], "strikeouts")
        self.assertEqual(spec["aggregation"], "count")
        self.assertEqual(spec["user"], "U11111111")
        self.assertEqual(spec["filters"]["wins_eq"], 0)
        self.assertEqual(spec["filters"]["ties_eq"], 0)

    def test_rewire_query_is_not_rule_supported(self) -> None:
        self.assertIsNone(nl_query._rule_based_translate("I'm going to rewire your brain now.", _GAMES, today=self.TODAY))


class NLQueryWorkbookExampleTests(unittest.TestCase):
    TODAY = date(2026, 5, 3)

    def _translate(self, text, *, resolver=None):
        return nl_query._validate_spec(
            nl_query._rule_based_translate(text, _GAMES, today=self.TODAY, resolve_user_name=resolver),
            _GAMES,
        )

    def _answer(self, text, *, resolver=None):
        store = _FakeStore()
        if "clean sweep" in text.lower():
            store.daily._rows.append(["2026-05-04", "", json.dumps({
                "winners_by_game": {
                    "Patches": {"result": "win", "winners": [{"user_id": "U1"}]},
                    "Tango": {"result": "win", "winners": [{"user_id": "U1"}]},
                },
                "awards_by_user": {"U1": {"wins": 2, "ties": 0}},
            })])
        with patch.object(nl_query, "_openai_plan", return_value=None):
            return nl_query.answer_nl_query(
                text,
                "U1",
                store,
                games=_GAMES,
                normalize_game=lambda g: (g or "").strip(),
                today=self.TODAY,
                resolve_user_name=resolver,
            )

    def test_today_participation_is_a_leaderboard(self):
        spec = self._translate("Who has played games today?")
        self.assertEqual(spec["measure"], "game_days_played")
        self.assertEqual(spec["subject"], "all_users")
        self.assertEqual(spec["date_range"]["start"], "2026-05-03")

    def test_named_player_is_resolved_for_game_count(self):
        spec = self._translate("How many games did Alice play today?", resolver=lambda name: "U11111111" if name.lower() == "alice" else "")
        self.assertEqual(spec["measure"], "game_days_played")
        self.assertEqual(spec["user"], "U11111111")

    def test_named_player_wins_query_does_not_fall_back_to_asker(self):
        spec = self._translate(
            "How many Zip games did bob win the last 31 days?",
            resolver=lambda name: "U22222222" if name.lower() == "bob" else "",
        )
        self.assertEqual(spec["measure"], "wins")
        self.assertEqual(spec["user"], "U22222222")

    def test_unresolved_named_player_asks_for_a_slack_mention(self):
        answer = self._answer("bot: How many games did Alice play today?")
        self.assertIn("couldn’t match", answer)
        self.assertIn("@mention", answer)

    def test_daily_trophy_record_and_mvp_phrasing_route_to_single_day_max(self):
        for question in (
            "what is the highest number of games won by a single player in one day?",
            "what's the daily record for mvp trophies?",
        ):
            spec = self._translate(question)
            self.assertEqual(spec["measure"], "wins")
            self.assertEqual(spec["group_by"], "day")
            self.assertEqual(spec["aggregation"], "max")

    def test_weekday_queries_keep_each_requested_weekday(self):
        spec = self._translate("Break down wins by day of the week. Who wins most on Mondays and Saturdays?")
        self.assertEqual(spec["filters"]["weekdays"], [0, 5])
        self.assertEqual(spec["group_by"], "weekday")

    def test_tie_rules_and_clean_sweep_are_supported(self):
        self.assertEqual(self._translate("how are ties decided?")["measure"], "tie_rules")
        self.assertEqual(self._translate("has anyone done a clean sweep?")["measure"], "clean_sweep")

    def test_monthly_championship_count_is_supported(self):
        spec = self._translate("How many months did <@U11111111> win the monthly totals?")
        self.assertEqual(spec["measure"], "monthly_titles")
        self.assertEqual(spec["user"], "U11111111")

    def test_today_report_routes_to_daily_report(self):
        spec = self._translate("run report for today's games")
        self.assertEqual(spec["measure"], "daily_report")
        self.assertEqual(spec["date_range"]["start"], "2026-05-03")

    def test_executor_answers_workbook_query_shapes(self):
        participation = self._answer("bot: Who has played games today?")
        self.assertIn("Games played leaderboard", participation)
        self.assertIn("<@U1>", participation)

        daily_record = self._answer("bot: what is the highest number of games won by a single player in one day?")
        self.assertIn(":trophy:x2", daily_record)
        self.assertIn("<@U2> on 2026-05-02", daily_record)

        weekday = self._answer("bot: Who wins most on Saturdays?")
        self.assertIn("*Saturday*", weekday)
        self.assertIn("<@U2>  :trophy:x2", weekday)

        report = self._answer("bot: run report for today's games")
        self.assertIn("Daily game report", report)
        self.assertIn("Patches: winner <@U1>", report)

        sweep = self._answer("bot: has anyone done a clean sweep?")
        self.assertIn("2026-05-04: <@U1> won all 2 recorded games", sweep)

        monthly = self._answer("bot: How many months did <@U11111111> win the monthly totals?")
        self.assertIn("Recorded titles: 1", monthly)

        rules = self._answer("bot: how are ties decided?")
        self.assertIn("If any tied player is missing tiebreak data", rules)


class NLQueryExecutorTests(unittest.TestCase):
    TODAY = date(2026, 5, 26)

    def _answer(self, question: str, user: str = "U1") -> str:
        with patch.object(nl_query, "_openai_plan", return_value=None):
            return nl_query.answer_nl_query(
                question,
                user,
                _FakeStore(),
                games=_GAMES,
                normalize_game=lambda g: (g or "").strip(),
                today=self.TODAY,
            )

    def test_game_days_count_uses_primary_score_rows(self) -> None:
        ans = self._answer("bot: how many games have I played?")
        self.assertIn("Game-days played: 5", ans)

    def test_wins_sum_uses_daily_results_awards(self) -> None:
        ans = self._answer("bot: how many wins do I have?")
        self.assertIn(":trophy:x2", ans)

    def test_strikeouts_count_any_active_zero_award_day(self) -> None:
        ans = self._answer("bot: how many times have I struck out with 0 wins and 0 ties?")
        self.assertIn("0-win, 0-tie days: 1", ans)

    def test_game_awards_summary_preserves_patches_results_shape(self) -> None:
        ans = self._answer("? how'd I do in patches the last 30 days?")
        self.assertIn("*Patches awards* for <@U1>", ans)
        self.assertIn(":trophy:x2", ans)

    def test_wins_leaderboard_sorts_by_wins_then_ties_then_user(self) -> None:
        ans = self._answer("bot: who wins Patches most often?")
        self.assertIn("1. <@U1>  :trophy:x2", ans)
        self.assertIn("2. <@U2>  :trophy:x1", ans)

    def test_score_stat_formats_time_metrics(self) -> None:
        ans = self._answer("bot: what's my median Tango time?")
        self.assertIn("*Tango median* for <@U1>", ans)
        self.assertIn("median=1:05", ans)


class DailyRecapTests(unittest.TestCase):
    def test_missing_skunk_fact_triggers_ai_revision_pass(self) -> None:
        facts = {
            "day": "2026-04-07",
            "players": ["U1", "U2", "U3"],
            "expected_players": 3,
            "complete_players": 3,
            "mvp": None,
            "tightest_race": None,
            "blowout": None,
            "skunk": {
                "game": "Tango",
                "last_uids": ["U3"],
                "last_value": 72,
                "margin": 32,
                "metric_type": "time",
                "last_display": "1:12",
            },
        }

        with patch.object(
            insights,
            "_openai_rewrite",
            side_effect=[
                "*Recap for 2026-04-07*\n_No notes. The scoreboard has spoken._",
                "*Recap for 2026-04-07*\nTango left <@U3> skunked by 32 seconds after a 1:12 finish.\n_No notes. The scoreboard has spoken._",
            ],
        ) as mocked:
            recap = insights.build_daily_recap_text(facts)

        self.assertEqual(mocked.call_count, 2)
        self.assertIn("Tango left <@U3> skunked by 32 seconds", recap)

    def test_skunk_complete_first_pass_skips_revision(self) -> None:
        facts = {
            "day": "2026-04-07",
            "players": ["U1", "U2", "U3"],
            "expected_players": 3,
            "complete_players": 3,
            "mvp": None,
            "tightest_race": None,
            "blowout": None,
            "skunk": {
                "game": "Tango",
                "last_uids": ["U3"],
                "last_value": 72,
                "margin": 32,
                "metric_type": "time",
                "last_display": "1:12",
            },
        }

        with patch.object(
            insights,
            "_openai_rewrite",
            return_value="*Recap for 2026-04-07*\nTango left <@U3> skunked by 32 seconds after a 1:12 finish.\n_No notes. The scoreboard has spoken._",
        ) as mocked:
            recap = insights.build_daily_recap_text(facts)

        self.assertEqual(mocked.call_count, 1)
        self.assertIn("Tango left <@U3> skunked by 32 seconds", recap)


if __name__ == "__main__":
    unittest.main()
