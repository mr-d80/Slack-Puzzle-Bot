"""Offline checks for score-day resolution and duplicate score identities."""

import threading
from types import SimpleNamespace

import gspread

import day_utils
import scoring
from day_utils import ScoreDayResolver
from parser import ParsedScore
from sheet_store import SheetStore


HEADER = list(SheetStore.REQUIRED_SCORES_COLS)


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.calls = []
        self.read_count = 0

    def get_all_values(self):
        self.read_count += 1
        return [list(row) for row in self.rows]

    def append_row(self, row, **_kwargs):
        self.calls.append("append_row")
        self.rows.append(list(row))

    def append_rows(self, rows, **_kwargs):
        self.calls.append("append_rows")
        self.rows.extend([list(row) for row in rows])

    def update(self, values=None, range_name=None, **_kwargs):
        self.calls.append("update")
        start, end = range_name.split(":")
        row_num, start_col = gspread.utils.a1_to_rowcol(start)
        _end_row, end_col = gspread.utils.a1_to_rowcol(end)
        assert len(values[0]) == end_col - start_col + 1
        self.rows[row_num - 1] = list(values[0])

    def batch_update(self, data, **_kwargs):
        self.calls.append("batch_update")
        for item in data:
            start, end = item["range"].split(":")
            start_row, start_col = gspread.utils.a1_to_rowcol(start)
            end_row, end_col = gspread.utils.a1_to_rowcol(end)
            values = item["values"][0]
            assert start_row == end_row
            assert end_col - start_col + 1 == len(values)
            while len(self.rows) < start_row:
                self.rows.append([""] * len(HEADER))
            self.rows[start_row - 1] = list(values)


def bare_store(rows):
    store = SheetStore.__new__(SheetStore)
    store.scores = FakeWorksheet([HEADER, *rows])
    store._write_lock = threading.RLock()
    store._sheets_write_max_retries = 1
    store._sheets_write_base_delay_s = 0
    store._sheets_write_max_delay_s = 0
    store.day_already_posted = lambda _day: False
    return store


def row(day, user, game, puzzle_id, metric, slack_ts, raw_text, updated_at="2026-10-08T10:00:00+00:00"):
    values = {
        "day": day,
        "user_id": user,
        "game": game,
        "puzzle_id": str(puzzle_id),
        "metric_type": "guesses",
        "metric_value": str(metric),
        "display": str(metric),
        "slack_ts": str(slack_ts),
        "raw_text": raw_text,
        "updated_at": updated_at,
        "tiebreak_value": "",
        "status": "solved",
    }
    return [values.get(column, "") for column in HEADER]


def parsed(metric, display=None, *, game="Zip", puzzle_id=101):
    return ParsedScore(
        game=game,
        puzzle_id=puzzle_id,
        metric_type="guesses",
        metric_value=metric,
        display=str(metric if display is None else display),
    )


def score_rows(store):
    return [
        dict(zip(HEADER, value))
        for value in store.scores.get_all_values()[1:]
        if any(value)
    ]


def test_resolve_score_day_prefers_explicit_date_and_supports_minimal_stores():
    class StoreWithoutPostedDays:
        def get_posted_days_snapshot(self, **_kwargs):
            raise AssertionError("an explicit puzzle date must bypass the live day bump")

    explicit = ParsedScore("MapTap", 42, "points", 900, "900", score_day="2026-10-08")
    assert day_utils.resolve_score_day("2026-10-09", explicit, StoreWithoutPostedDays()) == "2026-10-08"

    no_date = parsed(3)
    assert day_utils.resolve_score_day("2026-10-09", no_date, object()) == "2026-10-09"


class CountingResolverStore:
    def __init__(self, rows, posted_days):
        self.scores = FakeWorksheet([HEADER, *rows])
        self.posted_days = set(posted_days)
        self.posted_days_reads = 0

    def get_posted_days_snapshot(self, force_refresh=False):
        self.posted_days_reads += 1
        return set(self.posted_days)


def test_run_resolver_reads_posted_days_and_scores_once_for_many_messages():
    store = CountingResolverStore(
        [
            row("2026-10-08", "U1", "Zip", 100, 3, "1.0", "day 8"),
            row("2026-10-09", "U2", "Zip", 101, 3, "2.0", "day 9"),
        ],
        posted_days={"2026-10-08", "2026-10-09"},
    )
    resolver = ScoreDayResolver(store)

    assert resolver.resolve("2026-10-08", parsed(3, puzzle_id=101)) == "2026-10-09"
    assert resolver.resolve("2026-10-08", parsed(3, puzzle_id=102)) == "2026-10-10"
    assert store.posted_days_reads == 1
    assert store.scores.read_count == 1


def test_run_resolver_does_not_read_for_explicit_dates():
    store = CountingResolverStore([], posted_days={"2026-10-08"})
    resolver = ScoreDayResolver(store)
    explicit = SimpleNamespace(game="Zip", puzzle_id=101, score_day="2026-10-07")

    assert resolver.resolve("2026-10-08", explicit) == "2026-10-07"
    assert store.posted_days_reads == 0
    assert store.scores.read_count == 0


def test_single_upsert_collapses_legacy_duplicates_and_preserves_unrelated_rows():
    store = bare_store([
        row("2026-10-08", "U1", "Zip", 101, 8, "1.0", "old first"),
        row("2026-10-08", "U2", "Tango", 88, 30, "2.0", "unrelated"),
        row("2026-10-08", "U1", "Zip", 101, 7, "1.1", "old last"),
    ])

    store.upsert_score("2026-10-08", "U1", parsed(4), "1.2", "corrected")

    actual = score_rows(store)
    matching = [r for r in actual if (r["user_id"], r["game"], r["puzzle_id"]) == ("U1", "Zip", "101")]
    unrelated = [r for r in actual if r["user_id"] == "U2"]
    assert len(matching) == 1
    assert matching[0]["metric_value"] == "4"
    assert unrelated[0]["metric_value"] == "30"


def test_storage_identity_normalizes_day_user_and_puzzle_id():
    store = bare_store([
        row("2026-10-8", "<@U1>", "zip", "0101", 8, "1.0", "legacy spelling"),
    ])

    store.upsert_score("2026-10-08", "U1", parsed(4), "1.2", "corrected")

    matching = [r for r in score_rows(store) if r["user_id"] in ("<@U1>", "U1")]
    assert len(matching) == 1
    assert matching[0]["metric_value"] == "4"


def test_single_upsert_matches_zero_puzzle_id():
    store = bare_store([
        row("2026-10-08", "U1", "Zip", 0, 8, "1.0", "legacy zero puzzle"),
    ])

    store.upsert_score("2026-10-08", "U1", parsed(4, puzzle_id=0), "1.2", "corrected zero")

    matching = [r for r in score_rows(store) if r["user_id"] == "U1" and r["puzzle_id"] == "0"]
    assert len(matching) == 1
    assert matching[0]["metric_value"] == "4"


def test_bulk_upsert_matches_zero_puzzle_id():
    store = bare_store([
        row("2026-10-08", "U1", "Zip", 0, 8, "1.0", "legacy zero puzzle"),
    ])

    store.bulk_upsert_scores([
        ("2026-10-08", "U1", parsed(4, puzzle_id=0), "1.2", "corrected zero"),
    ])

    matching = [r for r in score_rows(store) if r["user_id"] == "U1" and r["puzzle_id"] == "0"]
    assert len(matching) == 1
    assert matching[0]["metric_value"] == "4"


def test_bulk_upsert_collapses_duplicates_and_latest_incoming_write_wins():
    store = bare_store([
        row("2026-10-08", "U1", "Zip", 101, 8, "1.0", "old first"),
        row("2026-10-08", "U1", "Zip", 101, 7, "1.1", "old last"),
        row("2026-10-08", "U2", "Tango", 88, 30, "2.0", "unrelated"),
    ])

    store.bulk_upsert_scores([
        ("2026-10-08", "U1", parsed(5), "1.2", "earlier in batch"),
        ("2026-10-08", "U1", parsed(4), "1.3", "latest in batch"),
    ])

    actual = score_rows(store)
    matching = [r for r in actual if (r["user_id"], r["game"], r["puzzle_id"]) == ("U1", "Zip", "101")]
    unrelated = [r for r in actual if r["user_id"] == "U2"]
    assert len(matching) == 1
    assert matching[0]["metric_value"] == "4"
    assert matching[0]["raw_text"] == "latest in batch"
    assert len(unrelated) == 1


def test_move_merges_source_and_destination_using_newer_slack_score():
    store = bare_store([
        row("2026-10-08", "U1", "Zip", 101, 3, "100.0", "old day"),
        row("2026-10-09", "U1", "Zip", 101, 1, "200.0", "newer destination"),
        row("2026-10-09", "U2", "Tango", 88, 20, "201.0", "unrelated"),
    ])

    moved = store.move_score_day("2026-10-08", "2026-10-09", "U1", "Zip", 101, slack_ts="100.0")

    actual = score_rows(store)
    matching = [r for r in actual if (r["user_id"], r["game"], r["puzzle_id"]) == ("U1", "Zip", "101")]
    assert moved is True
    assert len(matching) == 1
    assert matching[0]["day"] == "2026-10-09"
    assert matching[0]["metric_value"] == "1"
    assert matching[0]["raw_text"] == "newer destination"
    assert len([r for r in actual if r["user_id"] == "U2"]) == 1


def test_replay_then_move_awards_a_merged_score_only_once():
    store = bare_store([
        row("2026-10-08", "U1", "Zip", 101, 3, "100.0", "replay source", "2026-10-09T10:00:00+00:00"),
        row("2026-10-09", "U1", "Zip", 101, 1, "100.0", "newer edit", "2026-10-09T11:00:00+00:00"),
    ])
    # Replaying history writes to its original message day before rebucketing.
    store.upsert_score("2026-10-08", "U1", parsed(3), "100.0", "old replay")
    store.move_score_day("2026-10-08", "2026-10-09", "U1", "Zip", 101, slack_ts="100.0")

    rows = store.load_scores_for_day("2026-10-09")
    _, awards, _ = scoring.compute_daily_winners(rows, day="2026-10-09")
    assert len(rows) == 1
    assert rows[0]["metric_value"] == "1"
    assert awards["U1"] == {"gold": 1, "silver": 0, "bronze": 0, "points": 3}


def test_scoring_deduplicates_legacy_rows_by_day_user_game_and_puzzle():
    duplicate = {
        "day": "2026-10-09",
        "user_id": "<@U1>",
        "game": "Zip",
        "puzzle_id": "101",
        "metric_type": "guesses",
        "metric_value": "4",
        "display": "4",
        "status": "solved",
    }
    newer = {**duplicate, "user_id": "U1", "metric_value": "1", "display": "1"}
    _, awards, _ = scoring.compute_daily_winners([duplicate, newer], day="2026-10-09")
    assert awards["U1"] == {"gold": 1, "silver": 0, "bronze": 0, "points": 3}
