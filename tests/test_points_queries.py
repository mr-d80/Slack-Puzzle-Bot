"""Points-aware natural-language analytics over fake score and daily sheets."""

from __future__ import annotations

import json
import unittest
from datetime import date

import nl_query


class FakeWorksheet:
    def __init__(self, headers, rows):
        self._values = [headers, *rows]

    def get_all_values(self):
        return self._values


class FakeStore:
    def __init__(self, score_rows, daily_rows=()):
        self.scores = FakeWorksheet(
            ["day", "user_id", "game", "puzzle_id", "metric_type", "metric_value", "display", "status"],
            score_rows,
        )
        self.daily = FakeWorksheet(["day", "summary_json"], daily_rows)


class PointsQueryTests(unittest.TestCase):
    TODAY = date(2026, 10, 8)
    GAMES = ["Wordle", "Tango", "Zip", "4x6", "4x3", "MapTap"]

    @staticmethod
    def normalize_game(name):
        return (name or "").strip().replace("×", "x")

    def setUp(self):
        self.store = FakeStore([
            ["2026-10-05", "U_A", "4x6", "1", "points", "4", "4 points", "completed"],
            ["2026-10-05", "U_B", "4x6", "1", "points", "2", "2 points", "completed"],
            ["2026-10-05", "U_C", "4x6", "1", "points", "9", "9 points", "completed"],
            ["2026-10-05", "U_D", "4x6", "1", "points", "999", "999 points", "failed"],
            ["2026-10-06", "U_A", "4x6", "2", "points", "0", "0 points", "completed"],
            ["2026-10-06", "U_B", "4x6", "2", "points", "1", "1 point", "completed"],
            ["2026-10-06", "U_A", "Wordle", "10", "guesses", "4", "4 guesses", "completed"],
            ["2026-10-06", "U_B", "Wordle", "10", "guesses", "5", "5 guesses", "completed"],
            # Old rows have no status column value; legacy 100-guess entries are DNF.
            ["2026-10-06", "U_C", "Wordle", "10", "guesses", "100", "100 guesses", ""],
            # Two failures outvote a later solved puzzle id when selecting the primary.
            ["2026-10-07", "U_FAIL", "4x6", "20", "points", "0", "", "failed"],
            ["2026-10-07", "U_FAIL2", "4x6", "20", "points", "0", "", "failed"],
            ["2026-10-07", "U_OTHER", "4x6", "21", "points", "20", "20 points", "completed"],
        ], [
            ["2026-10-05", json.dumps({
                "winners_by_game": {
                    "4x6": {
                        "result": "win",
                        "winners": [{"user_id": "U_A"}],
                        "podium": [
                            {"place": 1, "user_ids": ["U_A"]},
                            {"place": 2, "user_ids": ["U_B"]},
                            {"place": 3, "user_ids": ["U_C"]},
                        ],
                    },
                },
                "awards_by_user": {
                    "U_A": {"gold": 1, "silver": 0, "bronze": 0, "points": 3},
                    "U_B": {"gold": 0, "silver": 1, "bronze": 0, "points": 2},
                    "U_C": {"gold": 0, "silver": 0, "bronze": 1, "points": 1},
                },
            })],
            ["2026-10-07", json.dumps({"winners_by_game": {"4x6": {}}})],
            ["2026-10-08", json.dumps({"winners_by_game": {}})],
        ])
        self.dr = nl_query.DateRange(date(2026, 10, 1), date(2026, 10, 8))

    def run_query(self, spec):
        answer, _rows = nl_query._execute_stats_query(
            spec,
            asker_user_id="U_A",
            store=self.store,
            games=self.GAMES,
            normalize_game=self.normalize_game,
            dr=self.dr,
        )
        return answer

    @staticmethod
    def score_spec(*, user="U_A", game="4x6", stat="best", aggregation=None, subject="user", group_by=""):
        aggregation = aggregation or stat
        return {
            "measure": "score_value",
            "subject": subject,
            "user": user if subject == "user" else "",
            "game": game,
            "stat": stat,
            "aggregation": aggregation,
            "group_by": group_by,
            "output": "scalar",
            "limit": 10,
        }

    def test_native_points_best_worst_and_numeric_extrema(self):
        best = self.run_query(self.score_spec(stat="best"))
        worst = self.run_query(self.score_spec(stat="worst"))
        numeric_min = self.run_query(self.score_spec(stat="min"))
        numeric_max = self.run_query(self.score_spec(stat="max"))

        self.assertIn("best=4 points", best)
        self.assertIn("worst=0 points", worst)
        self.assertIn("min=0 points", numeric_min)
        self.assertIn("max=4 points", numeric_max)
        self.assertIn("4 points", best)
        self.assertNotIn("guesses", best)
        self.assertNotIn("0:04", best)

    def test_records_personal_and_global_bests_rank_points_high(self):
        record = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "4x6",
            "aggregation": "leaderboard", "stat": "best", "group_by": "user",
            "output": "leaderboard", "limit": 10,
        })
        personal = self.run_query(self.score_spec(stat="best", aggregation="best", group_by="game"))
        global_best = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "",
            "aggregation": "best", "stat": "best", "group_by": "game",
            "output": "breakdown", "limit": 10,
        })
        global_worst = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "",
            "aggregation": "worst", "stat": "worst", "group_by": "game",
            "output": "breakdown", "limit": 10,
        })
        global_min = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "",
            "aggregation": "min", "stat": "min", "group_by": "game",
            "output": "breakdown", "limit": 10,
        })
        global_max = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "",
            "aggregation": "max", "stat": "max", "group_by": "game",
            "output": "breakdown", "limit": 10,
        })
        record_min = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "4x6",
            "aggregation": "min", "stat": "min", "group_by": "user",
            "output": "leaderboard", "limit": 10,
        })
        record_max = self.run_query({
            "measure": "score_value", "subject": "all_users", "user": "", "game": "4x6",
            "aggregation": "max", "stat": "max", "group_by": "user",
            "output": "leaderboard", "limit": 10,
        })

        self.assertLess(record.index("9 points by <@U_C>"), record.index("4 points by <@U_A>"))
        self.assertIn("Best: 9 points by <@U_C>", record)
        self.assertIn("- 4x6: 4 points on 2026-10-05", personal)
        self.assertIn("- 4x6: 9 points by <@U_C>", global_best)
        self.assertIn("- 4x6: 0 points by <@U_A>", global_worst)
        self.assertIn("- 4x6: 0 points by <@U_A>", global_min)
        self.assertIn("- 4x6: 9 points by <@U_C>", global_max)
        self.assertIn("Minimum: 0 points by <@U_A>", record_min)
        self.assertIn("Maximum: 9 points by <@U_C>", record_max)
        self.assertNotIn("999 points", record + personal + global_best + global_worst + global_min + global_max + record_min + record_max)

    def test_placements_rank_points_high_and_exclude_failed_rows(self):
        placements = nl_query._compute_placements(
            nl_query._load_scores_records(self.store),
            normalize_game=self.normalize_game,
            games=self.GAMES,
            dr=self.dr,
        )
        self.assertEqual(placements["U_C"]["4x6"], [1])
        self.assertEqual(placements["U_A"]["4x6"], [2, 2])
        self.assertEqual(placements["U_B"]["4x6"], [3, 1])
        self.assertNotIn("U_D", placements)
        self.assertNotIn("U_C", placements.get("Wordle", {}))

    def test_points_word_in_league_query_keeps_medal_points_contract(self):
        spec = nl_query._rule_based_translate(
            "Who has the most points in 4×6 this month?",
            self.GAMES,
            today=self.TODAY,
        )
        self.assertEqual(spec["measure"], "awards")
        self.assertEqual(spec["game"], "4x6")
        answer = self.run_query(spec)
        self.assertIn("*4x6 wins leaderboard*", answer)
        self.assertIn("3 pts", answer)
        self.assertNotIn("9 points", answer)

    def test_unicode_registered_game_alias_is_canonicalized(self):
        self.assertEqual(nl_query._find_game_in_text("my best score for 4×6", self.GAMES), "4x6")
        rule_spec = nl_query._rule_based_translate(
            "my best scores for 4×6", self.GAMES, today=self.TODAY,
        )
        self.assertEqual(rule_spec["stat"], "best")
        self.assertEqual(rule_spec["game"], "4x6")
        self.assertIn("best=4 points", self.run_query(rule_spec))
        spec = nl_query._validate_spec(
            {"schema_version": nl_query.STATS_QUERY_VERSION, "subject": "user", "user": "me",
             "game": "4×6", "measure": "score_value", "aggregation": "best", "stat": "best",
             "group_by": "", "output": "scalar"},
            self.GAMES,
        )
        self.assertEqual(spec["game"], "4x6")

    def test_failure_only_finalized_primary_counts_for_activity_loss_and_strikeout(self):
        facts = nl_query._build_stats_facts(
            nl_query._load_scores_records(self.store),
            nl_query._load_daily_payloads(self.store),
            normalize_game=self.normalize_game,
            games=self.GAMES,
            dr=self.dr,
        )
        submitted = [fact for fact in facts.scores if fact.day == "2026-10-07"]
        self.assertEqual({fact.user_id for fact in submitted}, {"U_FAIL", "U_FAIL2"})
        self.assertEqual(facts.result_scores, [fact for fact in facts.result_scores if fact.day != "2026-10-07"])
        fail_user_day = next(fact for fact in facts.daily_users if fact.user_id == "U_FAIL")
        self.assertEqual(fail_user_day.game_days_played, 1)
        self.assertTrue(fail_user_day.strikeout)

        played = self.run_query({
            "measure": "game_days_played", "subject": "user", "user": "U_FAIL", "game": "4x6",
            "aggregation": "count", "output": "scalar",
        })
        active = self.run_query({
            "measure": "active_days", "subject": "user", "user": "U_FAIL", "game": "",
            "aggregation": "count", "output": "scalar",
        })
        strikeouts = self.run_query({
            "measure": "strikeouts", "subject": "user", "user": "U_FAIL", "game": "",
            "aggregation": "count", "output": "scalar",
        })
        losses = self.run_query({
            "measure": "losses", "subject": "user", "user": "U_FAIL", "game": "4x6",
            "aggregation": "count", "output": "scalar",
        })
        self.assertIn("4x6 games played: 1", played)
        self.assertIn("Active days: 1", active)
        self.assertIn("0-win, 0-tie days: 1", strikeouts)
        self.assertIn("Losses: 1", losses)

    def test_ai_schema_advertises_direction_aware_score_stats(self):
        properties = nl_query._query_stats_tool()["parameters"]["properties"]
        self.assertIn("best", properties["aggregation"]["enum"])
        self.assertIn("worst", properties["aggregation"]["enum"])
        self.assertIn("best", properties["stat"]["enum"])
        self.assertIn("worst", properties["stat"]["enum"])

    def test_daily_report_handles_empty_outcomes_and_failed_only_games(self):
        report = self.run_query({
            "measure": "daily_report", "subject": "all_users", "user": "", "game": "",
            "aggregation": "summary", "output": "summary",
        })
        failed_day = report.split("*2026-10-07*", 1)[1].split("*2026-10-08*", 1)[0]
        empty_day = report.split("*2026-10-08*", 1)[1]
        self.assertIn("No completed game scores recorded.", failed_day)
        self.assertIn("No game scores recorded.", empty_day)
        self.assertNotIn("Current leaders", failed_day + empty_day)


if __name__ == "__main__":
    unittest.main()
