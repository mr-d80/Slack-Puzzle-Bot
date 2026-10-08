"""Tests for medal-era scoring: 3/2/1 points, Olympic ties, and the cut-over from trophies.

Days from MEDAL_SCORING_START (2026-10-01) award a podium; earlier days keep the
trophy/necktie rules. Both have to keep working, side by side, because the ledger
is never rewritten.
"""

import json
import os
import threading
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

import awards
import finalization
import scoring
from awards import AwardTally, render_podium, render_tally, unpack_awards
from sheet_store import SheetStore

# tests/conftest.py disables dotenv and sets AI_REWRITE_ENABLED=0 before imports.
# Keep this explicit setting so finalize_day calls here never use OpenAI.
os.environ["AI_REWRITE_ENABLED"] = "0"

OCT1 = "2026-10-01"
SEP30 = "2026-09-30"


def rec(game, uid, value, *, tb="", day=OCT1, metric_type=None, display=None, raw_text=""):
    """A Scores-sheet row as load_scores_for_day returns it."""
    return {
        "day": day, "user_id": uid, "game": game, "puzzle_id": "1",
        "metric_type": metric_type or ("guesses" if game == "Pinpoint" else "time"),
        "metric_value": str(value),
        "display": display or f"{value // 60}:{value % 60:02d}",
        "slack_ts": "", "raw_text": raw_text, "updated_at": "", "tiebreak_value": tb,
    }


class _DefaultCutover(unittest.TestCase):
    """Run with the cut-over at its default, whatever the developer's environment says."""

    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(awards.MEDAL_SCORING_START_ENV, None)


# ---------------------------------------------------------------------------
# The cut-over
# ---------------------------------------------------------------------------
class TestCutover(_DefaultCutover):
    def test_october_first_is_the_first_medal_day(self):
        self.assertFalse(awards.uses_medal_scoring("2026-09-30"))
        self.assertTrue(awards.uses_medal_scoring("2026-10-01"))
        self.assertTrue(awards.uses_medal_scoring("2026-12-31"))

    def test_accepts_dates_and_treats_junk_as_legacy(self):
        self.assertTrue(awards.uses_medal_scoring(date(2026, 10, 1)))
        self.assertFalse(awards.uses_medal_scoring(date(2026, 9, 30)))
        for junk in ("", None, "garbage", "2026-13-45"):
            self.assertFalse(awards.uses_medal_scoring(junk), junk)

    def test_environment_moves_the_cutover(self):
        with patch.dict(os.environ, {"MEDAL_SCORING_START": "2026-11-01"}):
            self.assertFalse(awards.uses_medal_scoring("2026-10-15"))
            self.assertTrue(awards.uses_medal_scoring("2026-11-01"))

    def test_unparseable_environment_value_falls_back_to_the_default(self):
        with patch.dict(os.environ, {"MEDAL_SCORING_START": "soon"}):
            self.assertEqual(awards.medal_scoring_start(), "2026-10-01")
            self.assertTrue(awards.uses_medal_scoring("2026-10-01"))

    def test_scoring_era_of_a_set_of_days(self):
        self.assertEqual(awards.scoring_era(["2026-09-29", "2026-09-30"]), "legacy")
        self.assertEqual(awards.scoring_era(["2026-10-01", "2026-10-02"]), "medals")
        self.assertEqual(awards.scoring_era(["2026-09-30", "2026-10-01"]), "mixed")
        self.assertEqual(awards.scoring_era([]), "legacy")


# ---------------------------------------------------------------------------
# AwardTally and rendering
# ---------------------------------------------------------------------------
class TestAwardTally(unittest.TestCase):
    def test_unpack_reads_every_stored_shape(self):
        self.assertEqual(unpack_awards(3), AwardTally(wins=3))                      # oldest rows: a bare count
        self.assertEqual(unpack_awards({"wins": 2, "ties": 1}), AwardTally(wins=2, ties=1))
        self.assertEqual(unpack_awards({"trophies": 4}), AwardTally(wins=4))         # legacy key name
        self.assertEqual(
            unpack_awards({"gold": 2, "silver": 1, "bronze": 3, "points": 11}),
            AwardTally(gold=2, silver=1, bronze=3, points=11),
        )
        self.assertEqual(
            unpack_awards({"wins": 5, "ties": 1, "gold": 2, "silver": 0, "bronze": 1, "points": 7}),
            AwardTally(wins=5, ties=1, gold=2, bronze=1, points=7),
        )

    def test_unpack_tolerates_junk(self):
        for junk in (None, "x", [], True, {"wins": None}, {"gold": "lots"}):
            self.assertEqual(unpack_awards(junk), AwardTally(), junk)

    def test_tallies_add_across_both_families(self):
        total = AwardTally(wins=2, ties=1) + AwardTally(gold=1, points=3)
        self.assertEqual(total, AwardTally(wins=2, ties=1, gold=1, points=3))
        self.assertTrue(total.has_legacy)
        self.assertTrue(total.has_medals)
        self.assertEqual(sum([AwardTally(wins=1), AwardTally(wins=2)], AwardTally()), AwardTally(wins=3))

    def test_firsts_count_trophies_and_golds(self):
        self.assertEqual(AwardTally(wins=2, ties=5, gold=3).firsts, 5)

    def test_points_decide_then_gold_silver_bronze_count_back(self):
        three_golds = AwardTally(gold=3, points=9)
        mixed = AwardTally(gold=2, silver=1, bronze=1, points=9)
        more_silver = AwardTally(gold=2, silver=2, points=10)
        ranked = sorted([mixed, more_silver, three_golds], key=lambda t: t.sort_key())
        self.assertEqual(ranked, [more_silver, three_golds, mixed])

    def test_legacy_tallies_still_rank_by_wins_then_ties(self):
        a, b, c = AwardTally(wins=3, ties=0), AwardTally(wins=3, ties=2), AwardTally(wins=1, ties=9)
        self.assertEqual(sorted([a, c, b], key=lambda t: t.sort_key()), [b, a, c])

    def test_medal_tally_for_a_place(self):
        self.assertEqual(awards.medal_tally(1), AwardTally(gold=1, points=3))
        self.assertEqual(awards.medal_tally(2), AwardTally(silver=1, points=2))
        self.assertEqual(awards.medal_tally(3), AwardTally(bronze=1, points=1))
        self.assertEqual(awards.medal_tally(4), AwardTally())


class TestRendering(unittest.TestCase):
    def test_legacy_rendering_matches_the_old_compact_awards(self):
        self.assertEqual(render_tally(AwardTally(wins=6, ties=2)), ":trophy:x6, :necktie:x2")
        self.assertEqual(render_tally(AwardTally(wins=6)), ":trophy:x6")
        self.assertEqual(render_tally(AwardTally()), ":trophy:x0")
        self.assertEqual(render_tally(AwardTally(wins=6), show_zeros=True), ":trophy:x6, :necktie:x0")
        self.assertEqual(
            render_tally(AwardTally(wins=6, ties=2), show_zeros=True, legacy_sep="  "),
            ":trophy:x6  :necktie:x2",
        )

    def test_medal_rendering_leads_with_points(self):
        t = AwardTally(gold=4, silver=1, points=14)
        self.assertEqual(render_tally(t), "14 pts (:first_place_medal:x4, :second_place_medal:x1)")
        self.assertEqual(
            render_tally(t, show_zeros=True),
            "14 pts (:first_place_medal:x4, :second_place_medal:x1, :third_place_medal:x0)",
        )

    def test_one_point_is_singular(self):
        self.assertEqual(render_tally(AwardTally(bronze=1, points=1)), "1 pt (:third_place_medal:x1)")

    def test_an_empty_tally_reads_in_the_requested_era(self):
        self.assertEqual(render_tally(AwardTally(), empty_era="medals"), "0 pts")
        self.assertEqual(
            render_tally(AwardTally(), empty_era="medals", show_zeros=True),
            "0 pts (:first_place_medal:x0, :second_place_medal:x0, :third_place_medal:x0)",
        )
        self.assertEqual(render_tally(AwardTally(), empty_era="legacy"), ":trophy:x0")

    def test_a_window_across_the_cutover_shows_both_families(self):
        t = AwardTally(wins=5, ties=1, gold=2, points=6)
        self.assertEqual(
            render_tally(t),
            ":trophy:x5, :necktie:x1 | 6 pts (:first_place_medal:x2)",
        )

    def test_podium_skips_the_place_a_tie_occupies(self):
        podium = [
            {"place": 1, "user_ids": ["U1", "U2"], "display": "0:13", "tiebreak": False},
            {"place": 3, "user_ids": ["U3"], "display": "0:20", "tiebreak": False},
        ]
        self.assertEqual(
            render_podium(podium),
            ":first_place_medal: <@U1>, <@U2> (0:13) | :third_place_medal: <@U3> (0:20)",
        )

    def test_podium_says_when_the_tiebreak_decided_it(self):
        podium = [{"place": 1, "user_ids": ["U1"], "display": "1:00", "tiebreak": True}]
        self.assertEqual(render_podium(podium), ":first_place_medal: <@U1> (1:00, tiebreak)")

    def test_podium_tolerates_missing_or_junk_data(self):
        self.assertEqual(render_podium(None), "")
        self.assertEqual(render_podium([{"place": 9, "user_ids": ["U1"]}, "x", {"place": 1, "user_ids": []}]), "")


# ---------------------------------------------------------------------------
# Ranking and points
# ---------------------------------------------------------------------------
class TestMedalScoring(_DefaultCutover):
    def score(self, records, day=OCT1):
        return scoring.compute_daily_winners(records, day=day)

    def points(self, awards_by_user):
        return {uid: v["points"] for uid, v in awards_by_user.items()}

    def test_podium_is_worth_3_2_1_and_fourth_place_scores_nothing(self):
        _, by_user, _ = self.score([
            rec("Tango", "U1", 30), rec("Tango", "U2", 35), rec("Tango", "U3", 41), rec("Tango", "U4", 50),
        ])
        self.assertEqual(by_user["U1"], {"gold": 1, "silver": 0, "bronze": 0, "points": 3})
        self.assertEqual(by_user["U2"], {"gold": 0, "silver": 1, "bronze": 0, "points": 2})
        self.assertEqual(by_user["U3"], {"gold": 0, "silver": 0, "bronze": 1, "points": 1})
        self.assertNotIn("U4", by_user)

    def test_tie_for_first_gives_each_three_points_and_the_next_player_third(self):
        wbg, by_user, _ = self.score([
            rec("Zip", "U1", 13), rec("Zip", "U2", 13), rec("Zip", "U3", 20), rec("Zip", "U4", 25),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 3, "U3": 1})
        self.assertEqual([p["place"] for p in wbg["Zip"]["podium"]], [1, 3])   # no second place
        self.assertEqual(by_user["U3"]["bronze"], 1)

    def test_tie_for_second_gives_each_two_points_and_nothing_to_fourth(self):
        _, by_user, _ = self.score([
            rec("Zip", "U1", 10), rec("Zip", "U2", 20), rec("Zip", "U3", 20), rec("Zip", "U4", 30),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2, "U3": 2})

    def test_tie_for_third_gives_each_one_point(self):
        _, by_user, _ = self.score([
            rec("Zip", "U1", 10), rec("Zip", "U2", 20), rec("Zip", "U3", 30), rec("Zip", "U4", 30),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2, "U3": 1, "U4": 1})

    def test_three_way_tie_for_first_leaves_no_second_or_third(self):
        _, by_user, _ = self.score([
            rec("Zip", "U1", 10), rec("Zip", "U2", 10), rec("Zip", "U3", 10), rec("Zip", "U4", 20),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 3, "U3": 3})

    def test_everyone_tied_everyone_wins(self):
        _, by_user, _ = self.score([rec("Zip", u, 10) for u in ("U1", "U2", "U3", "U4")])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 3, "U3": 3, "U4": 3})

    def test_a_sole_finisher_takes_gold(self):
        _, by_user, best = self.score([rec("Mini Sudoku", "U4", 90)])
        self.assertEqual(by_user["U4"]["gold"], 1)
        self.assertEqual(best["Mini Sudoku"], "1:30")

    def test_rank_finishers_skips_places_like_the_olympics(self):
        groups = scoring.rank_finishers([
            (value, {"user_id": uid}) for value, uid in
            [(10, "a"), (10, "b"), (20, "c"), (30, "d"), (30, "e"), (40, "f")]
        ])
        self.assertEqual([g["place"] for g in groups], [1, 3, 4, 6])
        self.assertEqual([len(g["records"]) for g in groups], [2, 1, 2, 1])

    # -- tiebreak ----------------------------------------------------------
    def test_tiebreak_splits_a_tie_for_first(self):
        wbg, by_user, _ = self.score([
            rec("Queens", "U1", 60, tb="0"), rec("Queens", "U2", 60, tb="2"), rec("Queens", "U3", 75),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2, "U3": 1})
        podium = wbg["Queens"]["podium"]
        self.assertEqual([p["user_ids"] for p in podium], [["U1"], ["U2"], ["U3"]])
        self.assertEqual([p["tiebreak"] for p in podium], [True, True, False])
        self.assertEqual(wbg["Queens"]["result"], "win")

    def test_tiebreak_applies_below_first_place_too(self):
        _, by_user, _ = self.score([
            rec("Queens", "U1", 10),
            rec("Queens", "U2", 20, tb="0"), rec("Queens", "U3", 20, tb="3"),
            rec("Queens", "U4", 30),
        ])
        # U2 beats U3 for second on the tiebreak, which pushes U3 to third and U4 off the podium.
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2, "U3": 1})

    def test_tiebreak_needs_data_from_every_tied_player(self):
        _, by_user, _ = self.score([rec("Queens", "U1", 60, tb="0"), rec("Queens", "U2", 60)])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 3})

    def test_equal_tiebreaks_leave_the_tie_standing(self):
        wbg, by_user, _ = self.score([rec("Queens", "U1", 60, tb="0"), rec("Queens", "U2", 60, tb="0")])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 3})
        self.assertEqual(wbg["Queens"]["result"], "tie")

    def test_tiebreak_is_read_from_raw_text_for_scores_stored_before_the_column(self):
        _, by_user, _ = self.score([
            rec("Zip", "U1", 13, raw_text="Zip #1 | 0:13 and flawless"),
            rec("Zip", "U2", 13, raw_text="Zip #1 | 0:13 with 3 backtracks"),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2})

    # -- DNF ---------------------------------------------------------------
    def test_a_pinpoint_dnf_cannot_take_a_medal(self):
        wbg, by_user, _ = self.score([
            rec("Pinpoint", "U1", 3, display="3"), rec("Pinpoint", "U2", 4, display="4"),
            rec("Pinpoint", "U3", 105, display="DNF(5)"), rec("Pinpoint", "U4", 105, display="DNF(5)"),
        ])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2})
        self.assertEqual([p["place"] for p in wbg["Pinpoint"]["podium"]], [1, 2])

    def test_a_game_nobody_finished_has_no_result(self):
        wbg, by_user, best = self.score([rec("Pinpoint", "U1", 105, display="DNF(5)")])
        self.assertEqual((wbg, by_user, best), ({}, {}, {}))

    def test_a_slow_time_is_not_mistaken_for_a_dnf(self):
        _, by_user, _ = self.score([rec("Tango", "U1", 150), rec("Tango", "U2", 205)])
        self.assertEqual(self.points(by_user), {"U1": 3, "U2": 2})

    # -- shape -------------------------------------------------------------
    def test_winners_by_game_keeps_the_shape_the_legacy_readers_expect(self):
        wbg, _, best = self.score([rec("Zip", "U1", 13), rec("Zip", "U2", 13), rec("Zip", "U3", 20)])
        zip_ = wbg["Zip"]
        self.assertEqual(zip_["result"], "tie")
        self.assertEqual(zip_["icon"], awards.GOLD)
        self.assertEqual(zip_["best_value"], 13)
        self.assertEqual([w["user_id"] for w in zip_["winners"]], ["U1", "U2"])   # first place only
        self.assertEqual(best["Zip"], "0:13")
        self.assertEqual(
            zip_["podium"][1],
            {"place": 3, "points": 1, "user_ids": ["U3"], "value": 20, "display": "0:20", "tiebreak": False},
        )
        json.dumps(wbg)   # it goes into summary_json

    def test_a_single_winner_is_a_win(self):
        wbg, _, _ = self.score([rec("Zip", "U1", 13), rec("Zip", "U2", 20)])
        self.assertEqual(wbg["Zip"]["result"], "win")


class TestWhichRulesApply(_DefaultCutover):
    SAME_SCORES = [rec("Zip", "U1", 13), rec("Zip", "U2", 20), rec("Zip", "U3", 30)]

    def test_the_same_scores_score_differently_across_the_cutover(self):
        _, legacy, _ = scoring.compute_daily_winners(self.SAME_SCORES, day=SEP30)
        _, medals, _ = scoring.compute_daily_winners(self.SAME_SCORES, day=OCT1)
        self.assertEqual(legacy, {"U1": {"wins": 1, "ties": 0}})
        self.assertEqual(medals["U1"]["points"], 3)
        self.assertEqual(medals["U2"]["points"], 2)

    def test_day_is_read_from_the_records_when_not_given(self):
        sep = [dict(r, day=SEP30) for r in self.SAME_SCORES]
        oct_ = [dict(r, day=OCT1) for r in self.SAME_SCORES]
        self.assertEqual(scoring.compute_daily_winners(sep)[1], {"U1": {"wins": 1, "ties": 0}})
        self.assertIn("points", scoring.compute_daily_winners(oct_)[1]["U1"])

    def test_an_explicit_day_overrides_the_records(self):
        sep = [dict(r, day=SEP30) for r in self.SAME_SCORES]
        self.assertIn("points", scoring.compute_daily_winners(sep, day=OCT1)[1]["U1"])

    def test_records_that_name_no_single_day_get_the_legacy_rules(self):
        undated = [{k: v for k, v in r.items() if k != "day"} for r in self.SAME_SCORES]
        self.assertEqual(scoring.compute_daily_winners(undated)[1], {"U1": {"wins": 1, "ties": 0}})
        mixed = [dict(self.SAME_SCORES[0], day=SEP30), dict(self.SAME_SCORES[1], day=OCT1)]
        self.assertEqual(scoring.compute_daily_winners(mixed)[1], {"U1": {"wins": 1, "ties": 0}})

    def test_legacy_rules_are_unchanged(self):
        wbg, by_user, best = scoring.compute_daily_winners([
            rec("Tango", "U1", 30, day=SEP30), rec("Tango", "U2", 30, day=SEP30),   # tie
            rec("Zip", "U1", 13, day=SEP30), rec("Zip", "U2", 20, day=SEP30),        # win
        ])
        self.assertEqual(by_user, {"U1": {"wins": 1, "ties": 1}, "U2": {"wins": 0, "ties": 1}})
        self.assertEqual(wbg["Tango"]["result"], "tie")
        self.assertEqual(wbg["Tango"]["icon"], ":necktie:")
        self.assertEqual(wbg["Zip"]["result"], "win")
        self.assertEqual(wbg["Zip"]["icon"], ":trophy:")
        self.assertNotIn("podium", wbg["Zip"])
        self.assertEqual(best, {"Tango": "0:30", "Zip": "0:13"})


# ---------------------------------------------------------------------------
# The daily post
# ---------------------------------------------------------------------------
class TestFormatSummary(_DefaultCutover):
    def test_medal_day_lists_the_podium_and_points(self):
        records = [
            rec("Tango", "U1", 30), rec("Tango", "U2", 35), rec("Tango", "U3", 41),
            rec("Zip", "U1", 13), rec("Zip", "U2", 13), rec("Zip", "U3", 20),
        ]
        wbg, by_user, best = scoring.compute_daily_winners(records, day=OCT1)
        totals = {
            "U1": {"gold": 6, "silver": 3, "bronze": 2, "points": 26},
            "U2": {"gold": 2, "silver": 5, "bronze": 4, "points": 20},
        }
        text = scoring.format_summary(OCT1, wbg, by_user, totals, best, players=["U1", "U2", "U3", "U4"])
        self.assertEqual(text.splitlines(), [
            "*Daily results for 2026-10-01*",
            "- Tango: :first_place_medal: <@U1> (0:30) | :second_place_medal: <@U2> (0:35) | :third_place_medal: <@U3> (0:41)",
            "- Zip: :first_place_medal: <@U1>, <@U2> (0:13) | :third_place_medal: <@U3> (0:20)",
            "",
            "*Today's awards*",
            "- <@U1> 6 pts (:first_place_medal:x2, :second_place_medal:x0, :third_place_medal:x0)",
            "- <@U2> 5 pts (:first_place_medal:x1, :second_place_medal:x1, :third_place_medal:x0)",
            "- <@U3> 2 pts (:first_place_medal:x0, :second_place_medal:x0, :third_place_medal:x2)",
            "- <@U4> 0 pts (:first_place_medal:x0, :second_place_medal:x0, :third_place_medal:x0)",
            "",
            "*Running totals (this month)*",
            "- <@U1> 26 pts (:first_place_medal:x6, :second_place_medal:x3, :third_place_medal:x2)",
            "- <@U2> 20 pts (:first_place_medal:x2, :second_place_medal:x5, :third_place_medal:x4)",
        ])

    def test_totals_rank_by_points_not_by_golds(self):
        totals = {
            "U1": {"gold": 9, "silver": 0, "bronze": 0, "points": 27},
            "U2": {"gold": 2, "silver": 12, "bronze": 3, "points": 33},
        }
        text = scoring.format_summary(OCT1, {}, {}, totals, {}, players=[])
        running = text.split("*Running totals (this month)*\n")[1].splitlines()
        self.assertTrue(running[0].startswith("- <@U2> 33 pts"))
        self.assertTrue(running[1].startswith("- <@U1> 27 pts"))

    def test_legacy_day_keeps_trophies_and_neckties(self):
        records = [rec("Zip", "U1", 13, day=SEP30), rec("Zip", "U2", 13, day=SEP30), rec("Tango", "U1", 30, day=SEP30)]
        wbg, by_user, best = scoring.compute_daily_winners(records, day=SEP30)
        totals = {"U1": {"wins": 12, "ties": 2}, "U2": {"wins": 9, "ties": 0}}
        text = scoring.format_summary(SEP30, wbg, by_user, totals, best, players=["U1", "U2"])
        self.assertEqual(text.splitlines(), [
            "*Daily results for 2026-09-30*",
            "- Tango: :trophy: <@U1> (best: 0:30)",
            "- Zip: :necktie: tie between <@U1>, <@U2> (best: 0:13)",
            "",
            "*Today's awards*",
            "- <@U1> :trophy:x1, :necktie:x1",
            "- <@U2> :trophy:x0, :necktie:x1",
            "",
            "*Running totals (this month)*",
            "- <@U1> :trophy:x12, :necktie:x2",
            "- <@U2> :trophy:x9",
        ])

    def test_players_default_to_everyone_in_the_awards_and_totals(self):
        text = scoring.format_summary(
            OCT1, {}, {"U1": {"gold": 1, "silver": 0, "bronze": 0, "points": 3}},
            {"U2": {"gold": 0, "silver": 1, "bronze": 0, "points": 2}}, {},
        )
        self.assertIn("- <@U1> 3 pts", text)
        self.assertIn("- <@U2> 0 pts", text)


# ---------------------------------------------------------------------------
# finalize_day, end to end against a fake store
# ---------------------------------------------------------------------------
class _FinalizeStore:
    def __init__(self, scores_by_day, monthly_totals=None):
        self.scores_by_day = scores_by_day
        self.monthly_totals = monthly_totals or {}
        self.posted = {}
        self.totals_requests = []
        self.rebuilds = 0

    def day_already_posted(self, day):
        return day in self.posted

    def load_scores_for_day(self, day):
        return [dict(r) for r in self.scores_by_day.get(day, [])]

    def load_monthly_totals_map(self, start, end):
        self.totals_requests.append((start, end))
        return {uid: dict(v) for uid, v in self.monthly_totals.items()}

    def mark_day_posted(self, day, summary):
        self.posted[day] = summary
        return True

    def replace_day_summary(self, day, summary):
        self.posted[day] = summary

    def update_day_summary(self, day, updates):
        self.posted[day].update(updates)

    def rebuild_totals_from_daily(self):
        self.rebuilds += 1


class _Client:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, channel, text, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": f"{len(self.posts)}.000"}


class TestFinalizeDay(_DefaultCutover):
    NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)   # every day below has closed

    def finalize(self, day, records, monthly_totals=None):
        store = _FinalizeStore({day: records}, monthly_totals)
        client = _Client()
        status = finalization.finalize_day(day, "C1", store, client, now_utc=self.NOW)
        self.assertEqual(status, "posted")
        return store, client

    def test_a_medal_day_is_scored_and_posted_with_medals(self):
        store, client = self.finalize(OCT1, [
            rec("Tango", "U1", 30), rec("Tango", "U2", 35), rec("Zip", "U2", 13), rec("Zip", "U1", 14),
        ])
        payload = store.posted[OCT1]
        self.assertEqual(payload["awards_by_user"]["U1"], {"gold": 1, "silver": 1, "bronze": 0, "points": 5})
        self.assertEqual(payload["awards_by_user"]["U2"], {"gold": 1, "silver": 1, "bronze": 0, "points": 5})
        self.assertEqual(payload["winners_by_game"]["Tango"]["podium"][0]["user_ids"], ["U1"])
        self.assertEqual(payload["recap_facts"]["scoring"], "medals")
        self.assertEqual(payload["recap_facts"]["mvp"]["points"], 5)
        self.assertIn(":first_place_medal:", client.posts[0]["text"])
        self.assertNotIn(":trophy:", client.posts[0]["text"])
        self.assertEqual(store.rebuilds, 1)

    def test_the_last_legacy_day_is_still_scored_with_trophies(self):
        store, client = self.finalize(SEP30, [
            rec("Tango", "U1", 30, day=SEP30), rec("Tango", "U2", 35, day=SEP30),
        ])
        self.assertEqual(store.posted[SEP30]["awards_by_user"], {"U1": {"wins": 1, "ties": 0}})
        self.assertNotIn("scoring", store.posted[SEP30]["recap_facts"])
        self.assertIn(":trophy:", client.posts[0]["text"])
        self.assertNotIn("medal", client.posts[0]["text"])

    def test_a_day_that_closes_after_the_cutover_keeps_its_own_rules(self):
        # Finalized on October 5th, but the score day is September 30th.
        store, _ = self.finalize(SEP30, [rec("Zip", "U1", 13, day=SEP30)])
        self.assertEqual(store.posted[SEP30]["awards_by_user"], {"U1": {"wins": 1, "ties": 0}})

    def test_todays_awards_are_merged_into_the_running_totals(self):
        prior = {"U1": {"wins": 0, "ties": 0, "gold": 4, "silver": 2, "bronze": 1, "points": 17}}
        _, client = self.finalize(OCT1, [rec("Tango", "U1", 30), rec("Tango", "U2", 35)], monthly_totals=prior)
        running = client.posts[0]["text"].split("*Running totals (this month)*\n")[1].splitlines()
        # U1: 17 + 3 for today's gold, 5 golds in all. U2 only has today's silver.
        self.assertEqual(running[0], "- <@U1> 20 pts (:first_place_medal:x5, :second_place_medal:x2, :third_place_medal:x1)")
        self.assertEqual(running[1], "- <@U2> 2 pts (:second_place_medal:x1)")

    def test_legacy_running_totals_still_merge(self):
        prior = {"U1": {"wins": 10, "ties": 1}}
        _, client = self.finalize(SEP30, [rec("Tango", "U1", 30, day=SEP30)], monthly_totals=prior)
        self.assertIn("- <@U1> :trophy:x11, :necktie:x1", client.posts[0]["text"])


# ---------------------------------------------------------------------------
# The real SheetStore award methods, against fake worksheets
# ---------------------------------------------------------------------------
class _Sheet:
    def __init__(self, rows, title="Sheet"):
        self.title = title
        self._rows = [list(r) for r in rows]

    @property
    def row_count(self):
        return max(len(self._rows), 1)

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def row_values(self, n):
        return list(self._rows[n - 1]) if len(self._rows) >= n else []

    def update(self, values=None, range_name=None, **kwargs):
        assert range_name == "A1", range_name
        if self._rows:
            self._rows[0] = list(values[0])
        else:
            self._rows.append(list(values[0]))

    def delete_rows(self, start, end=None):
        del self._rows[start - 1:end]

    def batch_clear(self, ranges):
        self._rows = self._rows[:1]

    def append_rows(self, rows, value_input_option=None):
        self._rows.extend(list(r) for r in rows)


def _ledger_store(daily_payloads, totals_rows=None):
    store = SheetStore.__new__(SheetStore)
    store.daily = _Sheet(
        [["day", "posted_at", "summary_json"]]
        + [[day, "", json.dumps(p)] for day, p in sorted(daily_payloads.items())],
        title="DailyResults",
    )
    store.totals = _Sheet(totals_rows if totals_rows is not None else [], title="Totals")
    store._write_lock = threading.RLock()
    store._sheets_write_max_retries = 1
    store._sheets_write_base_delay_s = 0.0
    store._sheets_write_max_delay_s = 0.0
    return store


_LEDGER = {
    "2026-09-29": {"awards_by_user": {"U1": {"wins": 2, "ties": 1}, "U2": {"wins": 1, "ties": 0}}},
    "2026-09-30": {"awards_by_user": {"U1": 1}},   # the oldest rows stored a bare count
    "2026-10-01": {"awards_by_user": {
        "U1": {"gold": 2, "silver": 1, "bronze": 0, "points": 8},
        "U2": {"gold": 0, "silver": 1, "bronze": 2, "points": 4},
    }},
    "2026-10-02": {"awards_by_user": {"U2": {"gold": 1, "silver": 0, "bronze": 0, "points": 3}}},
}


class TestSheetStoreAwards(_DefaultCutover):
    def test_monthly_totals_for_a_medal_month_are_points_and_medals(self):
        totals = _ledger_store(_LEDGER).load_monthly_totals_map("2026-10-01", "2026-10-31")
        self.assertEqual(totals["U1"], {"wins": 0, "ties": 0, "gold": 2, "silver": 1, "bronze": 0, "points": 8})
        self.assertEqual(totals["U2"], {"wins": 0, "ties": 0, "gold": 1, "silver": 1, "bronze": 2, "points": 7})

    def test_monthly_totals_for_a_legacy_month_are_unchanged(self):
        totals = _ledger_store(_LEDGER).load_monthly_totals_map("2026-09-01", "2026-09-30")
        self.assertEqual(totals["U1"]["wins"], 3)     # 2 + the bare 1
        self.assertEqual(totals["U1"]["ties"], 1)
        self.assertEqual(totals["U1"]["points"], 0)

    def test_a_range_across_the_cutover_carries_both_families(self):
        totals = _ledger_store(_LEDGER).load_monthly_totals_map("2026-09-01", "2026-10-31")
        self.assertEqual(totals["U1"], {"wins": 3, "ties": 1, "gold": 2, "silver": 1, "bronze": 0, "points": 8})

    def test_rebuild_writes_legacy_and_medal_columns(self):
        store = _ledger_store(_LEDGER, totals_rows=[["user_id", "trophies", "ties"], ["OLD", "9", "9"]])
        store.rebuild_totals_from_daily()
        rows = store.totals.get_all_values()
        # The medal columns are appended to the existing header; the old rows are replaced.
        self.assertEqual(rows[0], ["user_id", "trophies", "ties", "gold", "silver", "bronze", "points"])
        self.assertEqual(rows[1], ["U1", "3", "1", "2", "1", "0", "8"])
        self.assertEqual(rows[2], ["U2", "1", "0", "1", "1", "2", "7"])
        self.assertEqual(len(rows), 3)

    def test_rebuild_creates_the_header_on_an_empty_sheet(self):
        store = _ledger_store(_LEDGER, totals_rows=[])
        store.rebuild_totals_from_daily()
        self.assertEqual(store.totals.get_all_values()[0], list(SheetStore.REQUIRED_TOTALS_COLS))

    def test_totals_map_round_trips_the_sheet(self):
        store = _ledger_store(_LEDGER, totals_rows=[])
        store.rebuild_totals_from_daily()
        loaded = store.load_totals_map()
        self.assertEqual(loaded["U1"], {"wins": 3, "ties": 1, "gold": 2, "silver": 1, "bronze": 0, "points": 8})

    def test_totals_map_reads_a_sheet_that_predates_the_medal_columns(self):
        store = _ledger_store({}, totals_rows=[["user_id", "trophies", "ties"], ["U1", "12", "3"], ["U2", "", ""]])
        loaded = store.load_totals_map()
        self.assertEqual(loaded["U1"], {"wins": 12, "ties": 3, "gold": 0, "silver": 0, "bronze": 0, "points": 0})
        self.assertEqual(loaded["U2"]["wins"], 0)

    def test_the_header_grows_a_sheet_that_was_created_narrow(self):
        # The Sheets API rejects a header wider than the grid, and this runs at startup.
        class NarrowSheet(_Sheet):
            def __init__(self, rows, col_count):
                super().__init__(rows, title="Totals")
                self.col_count = col_count
                self.added = []

            def add_cols(self, n):
                self.added.append(n)
                self.col_count += n

        store = _ledger_store({})
        narrow = NarrowSheet([["user_id", "trophies", "ties"]], col_count=3)
        store._ensure_cols(narrow, SheetStore.REQUIRED_TOTALS_COLS)
        self.assertEqual(narrow.added, [4])
        self.assertEqual(narrow.get_all_values()[0], list(SheetStore.REQUIRED_TOTALS_COLS))

        roomy = NarrowSheet([["user_id", "trophies", "ties"]], col_count=26)
        store._ensure_cols(roomy, SheetStore.REQUIRED_TOTALS_COLS)
        self.assertEqual(roomy.added, [])
        self.assertEqual(roomy.get_all_values()[0], list(SheetStore.REQUIRED_TOTALS_COLS))

        # A sheet object that doesn't report its width is simply written to.
        plain = _Sheet([["user_id", "trophies", "ties"]], title="Totals")
        store._ensure_cols(plain, SheetStore.REQUIRED_TOTALS_COLS)
        self.assertEqual(plain.get_all_values()[0], list(SheetStore.REQUIRED_TOTALS_COLS))

    def test_a_complete_header_is_left_alone(self):
        class Spy(_Sheet):
            def update(self, *a, **k):
                raise AssertionError("header should not be rewritten")

        store = _ledger_store({})
        store._ensure_cols(Spy([list(SheetStore.REQUIRED_TOTALS_COLS)], title="Totals"), SheetStore.REQUIRED_TOTALS_COLS)

    def test_backfill_scores_each_day_under_its_own_rules(self):
        # compute_daily_winners is called with the day being backfilled.
        store = _ledger_store({})
        store.scores = _Sheet([["day", "user_id"]] + [[d, "U1"] for d in (SEP30, OCT1)], title="Scores")
        seen = {}

        def fake_scores(day):
            return [rec("Zip", "U1", 13, day=day), rec("Zip", "U2", 20, day=day)]

        def fake_winners(records, day=None):
            seen[day] = True
            return {}, {}, {}

        with patch.object(store, "load_scores_for_day", side_effect=fake_scores), \
                patch.object(store, "day_already_posted", return_value=False), \
                patch.object(store, "mark_day_posted"), \
                patch("scoring.compute_daily_winners", side_effect=fake_winners), \
                patch("day_utils.is_day_closed", return_value=True), \
                patch("day_utils.expected_players_for_day", return_value=2), \
                patch("insights.build_daily_recap_text", return_value=""):
            store.backfill_daily_ledger_from_scores()
        self.assertEqual(set(seen), {SEP30, OCT1})


if __name__ == "__main__":
    unittest.main()
