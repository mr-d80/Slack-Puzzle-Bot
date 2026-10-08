import unittest
from unittest.mock import patch

import game_registry
import insights
import monthly_summary
import scoring


LEGACY_DAY = "2026-09-30"
MEDAL_DAY = "2026-10-01"


def _score(user_id, value, *, status="solved", game="Point Test", display=None):
    return {
        "day": MEDAL_DAY,
        "user_id": user_id,
        "game": game,
        "metric_type": "points" if game == "Point Test" else "time",
        "metric_value": str(value),
        "display": str(value) if display is None else display,
        "status": status,
    }


class _PointGame(unittest.TestCase):
    def setUp(self):
        registry = game_registry.game_registry
        self.original_games = list(registry.games)
        self.games_patch = patch.object(registry, "games", self.original_games + [("Point Test", "points")])
        self.games_patch.start()
        self.addCleanup(self.games_patch.stop)


class TestPointScoring(_PointGame):
    def test_higher_points_win_and_ties_share_the_legacy_award(self):
        records = [
            _score("U1", 40), _score("U2", 80), _score("U3", 80), _score("U4", 60),
        ]
        records = [dict(r, day=LEGACY_DAY) for r in records]
        winners, awards, best = scoring.compute_daily_winners(records, day=LEGACY_DAY)
        self.assertEqual(winners["Point Test"]["result"], "tie")
        self.assertEqual([r["user_id"] for r in winners["Point Test"]["winners"]], ["U2", "U3"])
        self.assertEqual(winners["Point Test"]["best_value"], 80)
        self.assertEqual(awards, {"U2": {"wins": 0, "ties": 1}, "U3": {"wins": 0, "ties": 1}})
        self.assertEqual(best["Point Test"], "80 points")

    def test_high_points_take_the_medal_podium(self):
        winners, awards, best = scoring.compute_daily_winners([
            _score("U1", 70), _score("U2", 90), _score("U3", 80), _score("U4", 20),
        ], day=MEDAL_DAY)
        self.assertEqual([p["user_ids"] for p in winners["Point Test"]["podium"]], [["U2"], ["U3"], ["U1"]])
        self.assertEqual([p["value"] for p in winners["Point Test"]["podium"]], [90, 80, 70])
        self.assertEqual(awards["U2"]["gold"], 1)
        self.assertEqual(awards["U3"]["silver"], 1)
        self.assertEqual(awards["U1"]["bronze"], 1)
        self.assertEqual(best["Point Test"], "90 points")

    def test_failed_entries_never_get_legacy_or_medal_awards(self):
        records = [
            _score("U1", 100, status="failed"),
            _score("U2", 100, status="rescued"),
        ]
        for day in (LEGACY_DAY, MEDAL_DAY):
            with self.subTest(day=day):
                winners, awards, best = scoring.compute_daily_winners(
                    [dict(r, day=day) for r in records], day=day,
                )
                self.assertEqual(winners, {})
                self.assertEqual(awards, {})
                self.assertEqual(best, {})

    def test_zero_is_a_valid_completed_point_result(self):
        winners, awards, _ = scoring.compute_daily_winners([
            _score("U1", 0, status="solved"), _score("U2", 0, status="solved"),
        ], day=MEDAL_DAY)
        self.assertEqual(winners["Point Test"]["result"], "tie")
        self.assertEqual(awards["U1"]["gold"], 1)
        self.assertEqual(awards["U2"]["gold"], 1)

    def test_legacy_time_ranking_default_is_unchanged(self):
        groups = scoring.rank_finishers([(2, {"user_id": "U1"}), (1, {"user_id": "U2"})])
        self.assertEqual([g["records"][0]["user_id"] for g in groups], ["U2", "U1"])


class TestPointRecordsAndRaces(_PointGame):
    def test_month_record_keeps_the_highest_completed_point_value(self):
        payloads = {
            "2026-10-01": {
                "best_display": {"Point Test": "80"},
                "winners_by_game": {"Point Test": {
                    "best_value": 80, "winners": [_score("U1", 80)],
                }},
            },
            "2026-10-02": {
                "best_display": {"Point Test": "99"},
                "winners_by_game": {"Point Test": {
                    "best_value": 99, "winners": [_score("U2", 99, status="failed")],
                }},
            },
            "2026-10-03": {
                "best_display": {"Point Test": "90"},
                "winners_by_game": {"Point Test": {
                    "best_value": 90, "winners": [_score("U3", 90)],
                }},
            },
        }
        record = monthly_summary._collect_records(payloads)["Point Test"]
        self.assertEqual((record["value"], record["day"], record["user_ids"]), (90, "2026-10-03", ["U3"]))
        self.assertEqual(record["display"], "90 points")

    def test_month_record_rendering_names_the_point_unit(self):
        text = monthly_summary.render_monthly_standings_text({
            "month_label": "October 2026", "month": "2026-10", "days_counted": 1,
            "start_day": "2026-10-01", "end_day": "2026-10-31", "standings": [],
            "records": {"Point Test": {
                "display": "938 points", "user_ids": ["U1"], "day": "2026-10-01",
                "value": 938, "metric_type": "points",
            }},
        })
        self.assertIn("Point Test: 938 points by <@U1>", text)

    def test_month_race_extremes_skip_incomparable_point_margins(self):
        payloads = {
            "2026-10-01": {"recap_facts": {
                "tightest_race": {"game": "Point Test", "margin": 1, "metric_type": "points"},
                "blowout": {"game": "Point Test", "margin": 90, "metric_type": "points"},
            }},
            "2026-10-02": {"recap_facts": {
                "tightest_race": {"game": "Zip", "margin": 10, "metric_type": "time"},
                "blowout": {"game": "Tango", "margin": 20, "metric_type": "time"},
            }},
        }
        tightest, blowout = monthly_summary._extremes(payloads)
        self.assertEqual(tightest["game"], "Zip")
        self.assertEqual(blowout["game"], "Tango")

    def test_daily_race_orders_points_high_to_low_with_positive_margin(self):
        races = insights.compute_daily_races([_score("U1", 100), _score("U2", 95), _score("U3", 40)])
        race = races[0]
        self.assertEqual((race.winner_uid, race.runner_up_uid), ("U1", "U2"))
        self.assertEqual((race.best_value, race.runner_up_value, race.margin), (100, 95, 5))
        self.assertEqual((race.last_uids, race.last_value, race.last_margin), (("U3",), 40, 55))

    def test_cross_game_race_and_skunk_callouts_only_compare_time(self):
        records = [
            _score("P1", 101), _score("P2", 100), _score("P3", 0),
            _score("T1", 30, game="Zip"), _score("T2", 40, game="Zip"), _score("T3", 100, game="Zip"),
        ]
        facts = insights.build_daily_facts(
            day=MEDAL_DAY, records=records, awards_by_user={}, best_display_by_game={},
            expected_players=3, complete_players=3,
        )
        self.assertEqual(facts["tightest_race"]["game"], "Zip")
        self.assertEqual(facts["tightest_race"]["margin"], 10)
        self.assertEqual(facts["skunk"]["game"], "Zip")
        self.assertEqual(facts["skunk"]["margin"], 60)

    def test_explicit_point_race_labels_include_units_and_ai_rules(self):
        text = insights.render_daily_recap_text({
            "day": MEDAL_DAY, "players": ["U1", "U2"], "expected_players": 2,
            "complete_players": 2, "mvp": None, "blowout": None, "skunk": None,
            "tightest_race": {
                "game": "Point Test", "winner_uids": ["U1"], "runner_up_uid": "U2",
                "best_value": 100, "margin": 5, "metric_type": "points",
            },
        })
        self.assertIn("by 5 points", text)
        self.assertIn("best 100 points", text)
        system, _, _ = insights.build_ai_prompts("daily recap", {}, "fallback")
        self.assertIn("higher points are better", system)
        self.assertIn("never describe it as faster", system)


if __name__ == "__main__":
    unittest.main()
