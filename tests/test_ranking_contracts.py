"""Cross-consumer contracts for shared ranking and monthly ledger payloads."""

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import insights
import monthly_summary
import nl_query
from ledger_contracts import monthly_facts_from_summary
from parser import normalize_game
from scoring import rank_finishers


DAY = "2026-10-08"
DR = nl_query.DateRange(date(2026, 10, 1), date(2026, 10, 31))


def _score(user_id, value, *, game="Zip", puzzle_id=800, tiebreak=None, status="solved", day=DAY):
    return {
        "day": day,
        "user_id": user_id,
        "game": game,
        "puzzle_id": str(puzzle_id),
        "metric_type": "points" if game == "MapTap" else "time",
        "metric_value": str(value),
        "display": str(value),
        "tiebreak_value": "" if tiebreak is None else str(tiebreak),
        "status": status,
    }


def _placement_ranks(scores, games=("Zip", "Tango", "MapTap")):
    return nl_query._compute_placements(
        scores,
        normalize_game=normalize_game,
        games=list(games),
        dr=DR,
    )


def _stats(scores, games=("Zip", "Tango", "MapTap"), payloads=None):
    return nl_query._build_stats_facts(
        scores,
        payloads or {},
        normalize_game=normalize_game,
        games=list(games),
        dr=DR,
    )


def _score_store(scores, daily_payloads=()):
    headers = [
        "day", "user_id", "game", "puzzle_id", "metric_type", "metric_value",
        "display", "status", "tiebreak_value",
    ]

    class Worksheet:
        def __init__(self, rows):
            self.rows = rows

        def get_all_values(self):
            return [list(row) for row in self.rows]

    store = SimpleNamespace(
        scores=Worksheet([headers, *[[row.get(key, "") for key in headers] for row in scores]]),
        daily=Worksheet([
            ["day", "summary_json"],
            *[[day, json.dumps(payload)] for day, payload in daily_payloads],
        ]),
    )
    return store


def _run_query(scores, spec, *, daily_payloads=()):
    return nl_query._execute_stats_query(
        spec,
        asker_user_id="U1",
        store=_score_store(scores, daily_payloads=daily_payloads),
        games=["Zip"],
        normalize_game=normalize_game,
        dr=DR,
    )[0]


def _daily_report_query(scores):
    return _run_query(
        scores,
        {
            "measure": "daily_report", "subject": "all_users", "user": "", "game": "",
            "aggregation": "summary", "output": "summary",
        },
    )


def test_tiebreak_rank_groups_agree_in_queries_stats_and_recap():
    scores = [
        _score("U2", 10, tiebreak=2),
        _score("U1", 10, tiebreak=0),
        _score("U3", 20),
    ]

    placements = _placement_ranks(scores)
    facts = _stats(scores)
    race = insights.compute_daily_races(scores)[0]

    assert placements["U1"]["Zip"] == [1]
    assert placements["U2"]["Zip"] == [2]
    assert placements["U3"]["Zip"] == [3]
    assert {fact.user_id: fact.rank for fact in facts.scores} == {"U2": 2, "U1": 1, "U3": 3}
    assert race.winner_uids == ("U1",)
    assert race.runner_up_uid == "U2"
    assert race.runner_up_uids == ("U2",)
    # The first two places were decided on tiebreak while both took 10 seconds.
    assert race.margin is None


def test_true_ties_missing_tiebreak_and_skipped_places_stay_shared():
    scores = [
        _score("U1", 10, tiebreak=0),
        _score("U2", 10),  # Missing data prevents a partial tiebreak comparison.
        _score("U3", 20),
        _score("U4", 30),
    ]

    groups = rank_finishers([(int(r["metric_value"]), r) for r in scores])
    placements = _placement_ranks(scores)
    facts = _stats(scores)
    race = insights.compute_daily_races(scores)[0]

    assert [(g["place"], len(g["records"])) for g in groups] == [(1, 2), (3, 1), (4, 1)]
    assert placements["U1"]["Zip"] == [1]
    assert placements["U2"]["Zip"] == [1]
    assert placements["U3"]["Zip"] == [3]
    assert {fact.user_id: fact.rank for fact in facts.scores} == {"U1": 1, "U2": 1, "U3": 3, "U4": 4}
    assert race.winner_uids == ("U1", "U2")
    assert race.runner_up_uids == ("U3",)
    assert race.margin == 10
    assert race.last_uids == ("U4",)
    assert race.last_margin == 10


def test_points_rank_high_to_low_and_games_are_not_mixed():
    scores = [
        _score("P1", 100, game="MapTap"),
        _score("P2", 90, game="MapTap"),
        _score("P3", 80, game="MapTap"),
        _score("T1", 100, game="Tango"),
        _score("T2", 90, game="Tango"),
    ]

    placements = _placement_ranks(scores)
    facts = _stats(scores)
    races = {race.game: race for race in insights.compute_daily_races(scores)}

    assert placements["P1"]["MapTap"] == [1]
    assert placements["P2"]["MapTap"] == [2]
    assert placements["P3"]["MapTap"] == [3]
    assert {fact.user_id: fact.rank for fact in facts.scores if fact.game == "MapTap"} == {
        "P1": 1, "P2": 2, "P3": 3,
    }
    assert races["MapTap"].margin == 10
    assert races["Tango"].margin == 10
    assert races["MapTap"].winner_uid == "P1"
    assert races["Tango"].winner_uid == "T2"


def test_duplicate_solved_then_dnf_keeps_participation_without_a_rank():
    scores = [
        _score("U1", 5, tiebreak=0),
        _score("U1", 0, status="failed"),
        _score("U2", 15),
    ]

    facts = _stats(scores)
    placements = _placement_ranks(scores)
    race = insights.compute_daily_races(scores)[0]
    by_user = {fact.user_id: fact for fact in facts.scores}

    assert by_user["U1"].status == "failed"
    assert by_user["U1"].rank == 0
    assert by_user["U1"].players == 1
    assert by_user["U2"].rank == 1
    assert "U1" not in placements
    assert race.winner_uids == ("U2",)


def test_unfinalized_daily_report_uses_shared_rank_groups_for_current_leaders():
    split = [
        _score("U1", 10, tiebreak=0),
        _score("U2", 10, tiebreak=2),
    ]
    tied = [
        _score("U1", 10, tiebreak=0),
        _score("U2", 10, tiebreak=0),
    ]

    split_report = _daily_report_query(split)
    tied_report = _daily_report_query(tied)

    assert "Current leaders (provisional until the day is finalized):" in split_report
    assert "- Zip: <@U1> (0:10)" in split_report
    assert "<@U2>" not in split_report
    assert "- Zip: <@U1>, <@U2> (0:10)" in tied_report


def test_finalized_winner_readers_canonicalize_stored_slack_mentions():
    outcomes = {
        "Zip": {"result": "win", "winners": [{"user_id": "<@U1>"}]},
        "Tango": {"result": "win", "winners": [{"user_id": "<@U1>"}]},
    }
    daily_payloads = [(DAY, {"winners_by_game": outcomes})]

    daily_report = _run_query([], {
        "measure": "daily_report", "subject": "all_users", "user": "", "game": "",
        "aggregation": "summary", "output": "summary",
    }, daily_payloads=daily_payloads)
    clean_sweeps = _run_query([], {
        "measure": "clean_sweep", "subject": "all_users", "user": "", "game": "",
        "aggregation": "summary", "output": "summary",
    }, daily_payloads=daily_payloads)

    assert "- Zip: winner <@U1>" in daily_report
    assert "<@<@U1>>" not in daily_report
    assert f"- {DAY}: <@U1> won all 2 recorded games" in clean_sweeps
    assert "<@<@U1>>" not in clean_sweeps


def test_score_facts_and_recap_canonicalize_latest_duplicate_user_row():
    records = [
        _score("U1", 20),
        _score("<@U1>", 10),  # Same identity; the later row is authoritative.
    ]
    facts = _stats(records, games=("Zip",))

    assert len(facts.scores) == 1
    assert facts.scores[0].user_id == "U1"
    assert facts.scores[0].metric_value == 10
    assert facts.scores[0].players == 1

    report = _daily_report_query(records)
    assert "- Zip: <@U1> (0:10)" in report
    assert "<@<@U1>>" not in report

    user_query = _run_query(records, {
        "measure": "score_value", "subject": "user", "user": "U1", "game": "Zip",
        "aggregation": "best", "stat": "best", "group_by": "", "output": "scalar", "limit": 10,
    })
    assert "N=1  best=0:10" in user_query

    recap_facts = insights.build_daily_facts(DAY, records, {}, {}, 1, 1)
    assert recap_facts["players"] == ["U1"]


def test_stats_facts_canonicalize_legacy_and_medal_payload_user_ids():
    legacy_day = "2026-10-07"
    medal_day = "2026-10-08"
    payloads = {
        legacy_day: {
            "awards_by_user": {
                "U1": {"wins": 9, "ties": 0},
                "<@U1>": {"wins": 1, "ties": 0},  # Alias collision: last entry wins.
                "<@U3>": {"wins": 0, "ties": 1},
                "<@U4>": {"wins": 0, "ties": 1},
            },
            "winners_by_game": {
                "Zip": {"result": "win", "winners": [{"user_id": "<@U1>"}]},
                "Tango": {
                    "result": "tie",
                    "winners": [{"user_id": "<@U3>"}, {"user_id": "<@U4>"}],
                },
            },
        },
        medal_day: {
            "awards_by_user": {"<@U2>": {"gold": 1, "silver": 0, "bronze": 0, "points": 3}},
            "winners_by_game": {
                "Tango": {
                    "podium": [{"place": 1, "user_ids": ["<@U2>"]}],
                },
            },
        },
    }
    scores = [
        _score("U1", 300, game="Zip", day=legacy_day),
        _score("U3", 100, game="Tango", day=legacy_day),
        _score("U4", 120, game="Tango", day=legacy_day),
        _score("U2", 10, game="Tango", day=medal_day),
    ]

    facts = _stats(scores, payloads=payloads)
    daily = {(fact.day, fact.user_id): fact for fact in facts.daily_users}
    awards = {(fact.day, fact.game, fact.user_id): fact.tally for fact in facts.game_awards}

    assert daily[(legacy_day, "U1")].tally.wins == 1
    assert daily[(legacy_day, "U3")].tally.ties == 1
    assert daily[(legacy_day, "U4")].tally.ties == 1
    assert daily[(medal_day, "U2")].tally.gold == 1
    assert awards[(legacy_day, "Zip", "U1")].wins == 1
    assert awards[(legacy_day, "Tango", "U3")].ties == 1
    assert awards[(legacy_day, "Tango", "U4")].ties == 1
    assert awards[(medal_day, "Tango", "U2")].gold == 1


class _MonthlyWorksheet:
    def __init__(self):
        self.rows = [["month", "posted_at", "summary_json"]]

    def get_all_values(self):
        return [list(row) for row in self.rows]


class _MonthlyWriterStore:
    def __init__(self):
        self.monthly = _MonthlyWorksheet()

    def month_already_posted(self, _month):
        return False

    def mark_month_posted(self, month, summary):
        self.monthly.rows.append([month, "now", json.dumps(summary)])
        return True


def _monthly_titles(store, uid):
    dr = nl_query.DateRange(date(2026, 6, 1), date(2026, 6, 30))
    return nl_query._format_monthly_titles(store, uid=uid, dr=dr)


def test_monthly_writer_payload_round_trips_tied_champions_to_query_reader():
    facts = {
        "month": "2026-06",
        "days_counted": 30,
        "standings": [
            {"user_id": "U1", "place": 1, "wins": 8, "ties": 1},
            {"user_id": "U2", "place": 1, "wins": 8, "ties": 1},
        ],
        "champion": {"user_ids": ["U1", "U2"], "shared": True, "wins": 8},
    }
    store = _MonthlyWriterStore()

    with patch.object(monthly_summary, "month_is_final", return_value=True), \
         patch.object(monthly_summary, "load_month_facts", return_value=facts), \
         patch.object(monthly_summary, "render_monthly_standings_text", return_value="standings"), \
         patch.object(monthly_summary, "build_monthly_recap_text", return_value="recap"):
        status = monthly_summary.finalize_month("2026-06", "C1", store, None, post=False)

    assert status == "posted"
    payload = json.loads(store.monthly.rows[1][2])
    assert payload["schema_version"] == 1
    assert payload["facts"] == facts
    assert "Recorded titles: 1" in _monthly_titles(store, "U1")
    assert "Recorded titles: 1" in _monthly_titles(store, "U2")
    assert "Recorded titles: 0" in _monthly_titles(store, "U3")

    store.monthly.rows.append(list(store.monthly.rows[1]))
    assert "Recorded titles: 1" in _monthly_titles(store, "U1")


def test_monthly_titles_use_latest_nonempty_row_per_month():
    store = _MonthlyWriterStore()
    store.monthly.rows.extend([
        ["2026-06", "older", json.dumps({"champion": {"user_ids": ["U1"]}})],
        ["2026-06", "newer", json.dumps({"champion": {"user_ids": ["U2"]}})],
    ])

    assert "Recorded titles: 0" in _monthly_titles(store, "U1")
    assert "Recorded titles: 1" in _monthly_titles(store, "U2")

    store.monthly.rows.append(["2026-06", "corrupt-newest", "not json"])
    assert "Recorded titles: 0" in _monthly_titles(store, "U2")


def test_monthly_facts_canonicalize_stored_winner_mentions_and_record_holders():
    day = "2026-06-15"
    outcome = {
        "result": "win",
        "metric_type": "time",
        "best_value": 300,
        "winners": [
            {"user_id": "<@U1>"},
            "U1",
            "<@U1|player>",
        ],
    }
    payload = {
        "awards_by_user": {"U1": {"wins": 1, "ties": 0}},
        "winners_by_game": {"Zip": outcome},
        "best_display": {"Zip": "5:00"},
    }

    assert monthly_summary._winner_uids(outcome) == ["U1"]
    facts = monthly_summary.build_monthly_facts("2026-06", {day: payload})

    assert facts["players"] == ["U1"]
    assert facts["game_champions"]["Zip"]["user_ids"] == ["U1"]
    assert facts["records"]["Zip"]["user_ids"] == ["U1"]
    rendered = monthly_summary.render_monthly_standings_text(facts)
    assert "- Zip: <@U1>" in rendered
    assert "by <@U1>" in rendered
    assert "<@<@U1>>" not in rendered
    assert monthly_summary._tags(["<@U1>", "U1"]) == "<@U1>"


def test_monthly_reader_supports_legacy_flat_rows_and_skips_malformed_entries():
    store = _MonthlyWriterStore()
    legacy_flat = {
        "month": "2026-06",
        "champion": {"user_ids": [None, {"bad": "id"}]},
        "standings": [
            None,
            {"user_id": "U_BAD", "place": "first"},
            {"user_id": "U3", "place": "1"},
            {"user_id": "U4", "place": 1.5},
        ],
    }
    store.monthly.rows.append(["2026-06", "now", json.dumps(legacy_flat)])

    assert "Recorded titles: 1" in _monthly_titles(store, "U3")
    assert "Recorded titles: 0" in _monthly_titles(store, "U_BAD")
    assert monthly_facts_from_summary({"month": "2026-06", "facts": {"standings": []}, "standings": ["ignored"]}) == {"standings": []}


def test_monthly_titles_normalize_legacy_mention_ids_in_champions_and_standings():
    legacy_summaries = [
        {"champion": {"user_ids": ["<@U1>"]}},
        {"champion": {"user_ids": []}, "standings": [{"user_id": "<@U1>", "place": 1}]},
    ]
    for summary in legacy_summaries:
        store = _MonthlyWriterStore()
        store.monthly.rows.append(["2026-06", "legacy", json.dumps(summary)])
        assert "Recorded titles: 1" in _monthly_titles(store, "U1")


def test_monthly_accumulation_unifies_legacy_aliases_across_payload_sections():
    day = "2026-06-15"
    payload = {
        "recap_facts": {
            "players": ["U1", "<@U1>"],
            "skunk": {"game": "Zip", "last_uids": ["<@U1>", "U1"]},
        },
        "awards_by_user": {
            "U1": {"wins": 8, "ties": 0},
            "<@U1>": {"wins": 1, "ties": 0},
        },
        "winners_by_game": {
            "Zip": {
                "result": "win",
                "best_value": 300,
                "winners": [{"user_id": "<@U1>"}, "U1"],
            },
        },
    }

    facts = monthly_summary.build_monthly_facts("2026-06", {day: payload})

    assert facts["players"] == ["U1"]
    assert len(facts["standings"]) == 1
    assert facts["standings"][0]["user_id"] == "U1"
    assert facts["standings"][0]["days_played"] == 1
    assert facts["standings"][0]["wins"] == 1
    assert facts["standings"][0]["skunks"] == 1
    assert facts["game_champions"]["Zip"]["user_ids"] == ["U1"]


def test_monthly_recap_normalizes_nested_historical_race_ids():
    facts = {
        "month": "2026-06",
        "champion": {"user_ids": ["<@U1>", "U1"], "shared": False, "wins": 1, "ties": 0},
        "runner_up": {"user_ids": ["<@U2>"]},
        "records": {"Zip": {"user_ids": ["<@U1>"], "display": "5:00"}},
        "tightest_race": {
            "game": "Zip", "day": "2026-06-15", "winner_uids": ["<@U1>"],
            "runner_up_uids": ["<@U2>"], "margin": 2, "metric_type": "time",
        },
    }

    rendered = insights.render_monthly_recap_text(facts)

    assert "Champion: <@U1>" in rendered
    assert "<@U1>, <@U1>" not in rendered
    assert "<@<@U1>>" not in rendered
    assert "Tightest race: Zip on 2026-06-15, <@U1> over <@U2> by 2s" in rendered
