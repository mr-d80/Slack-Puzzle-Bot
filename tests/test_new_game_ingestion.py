"""Offline integration checks; these do not validate live provider share formats."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from game_registry import game_registry, _OPTIONAL_GAMES
from parser import parse_score_for_day
from sheet_store import SheetStore
import reconcile_day
import scan_slack_day


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def get_all_values(self):
        return self.rows

    def append_row(self, row, **kwargs):
        self.rows.append(row)


class NewGameIngestionTests(unittest.TestCase):
    def setUp(self):
        self.games_patch = patch.object(game_registry, "games", game_registry.games_as_tuples() + list(_OPTIONAL_GAMES))
        self.games_patch.start()
        game_registry._rebuild_regexes()
        self.addCleanup(game_registry._rebuild_regexes)
        self.addCleanup(self.games_patch.stop)

    def make_bot(self, payloads=()):
        writes = []
        store = SimpleNamespace(
            events=Rows([["event_id", "payload_json"]] + [[str(i), json.dumps(p)] for i, p in enumerate(payloads)]),
            bulk_upsert_scores=lambda rows: writes.extend(rows) or len(rows),
            bulk_log_events=lambda _rows: None,
            seen_event=lambda _event_id: False,
        )
        bot = SimpleNamespace(
            store=store, SCORE_CHANNEL_ID="C1", parse_score_for_day=parse_score_for_day,
            day_key_from_ts=lambda _ts: "2026-10-09",
            resolve_pinpoint_fail_score=lambda *_args, **_kwargs: None,
        )
        return bot, writes

    def test_replay_uses_the_shared_puzzle_date_for_a_late_maptap_post(self):
        text = "www.maptap.gg October 8\n100:dart: 89:tada: 100:dart: 90:crown: 93:trophy:\nFinal score: 938"
        bot, writes = self.make_bot([{"event": {"channel": "C1", "user": "U1", "ts": "1.0", "text": text}}])
        self.assertEqual(reconcile_day.replay_events_for_day(bot, "2026-10-08"), 1)
        self.assertEqual(writes[0][0], "2026-10-08")
        self.assertEqual(writes[0][2].metric_value, 938)
        self.assertEqual(writes[0][2].status, "solved")
        self.assertEqual(reconcile_day.replay_events_for_day(bot, "2026-10-09"), 0)

    def test_history_candidates_use_native_dates_but_keep_timestamp_bucketing_for_zip(self):
        bot, _writes = self.make_bot()
        messages = [
            {"user": "U1", "ts": "1.0", "text": "www.maptap.gg October 8\nFinal score: 938"},
            {"user": "U2", "ts": "2.0", "text": "Zip #123 | 0:13"},
            {"user": "U3", "ts": "3.0", "text": "www.maptap.gg October 9\nFinal score: 900"},
        ]
        candidates = scan_slack_day._extract_candidates(bot, "C1", messages, "2026-10-08")
        self.assertEqual([candidate.user_id for candidate in candidates], ["U1"])

    def test_history_sync_preserves_failed_wordle_status(self):
        bot, writes = self.make_bot()
        messages = [{"user": "U1", "ts": "1.0", "text": "Wordle 1,234 X/6"}]
        with patch.object(scan_slack_day, "_day_window_utc", return_value=(100, 200)):
            with patch.object(scan_slack_day, "_fetch_channel_messages", return_value=messages):
                self.assertEqual(scan_slack_day.sync_slack_history_for_day(bot, "2026-10-09", "C1"), 1)
        self.assertEqual(writes[0][2].status, "failed")

    def test_history_and_replay_ignore_bot_scores(self):
        message = {"channel": "C1", "user": "U1", "ts": "1.0", "text": "Wordle 1,234 4/6", "bot_id": "B1"}
        bot, writes = self.make_bot([{"event": message}])
        self.assertEqual(reconcile_day.replay_events_for_day(bot, "2026-10-09"), 0)
        self.assertEqual(scan_slack_day._extract_candidates(bot, "C1", [message], "2026-10-09"), [])
        self.assertEqual(writes, [])

    def test_score_storage_round_trips_status_and_positive_points(self):
        store = SheetStore.__new__(SheetStore)
        store.scores = Rows([list(SheetStore.REQUIRED_SCORES_COLS)])
        store._write_lock = threading.RLock()
        store._sheets_write_max_retries = 1
        store._sheets_write_base_delay_s = 0
        store._sheets_write_max_delay_s = 0
        parsed = parse_score_for_day("www.maptap.gg October 8\nFinal score: 938", "2026-10-09")
        store.upsert_score(parsed.score_day, "U1", parsed, "1.0", "sample")
        record = store.load_scores_for_day("2026-10-08")[0]
        self.assertEqual(record["status"], "solved")
        self.assertEqual(record["metric_value"], "938")
        self.assertEqual(record["metric_type"], "points")

    def test_native_games_reject_incompatible_registry_metrics(self):
        store = SheetStore.__new__(SheetStore)
        for name, metric in (("Wordle", "points"), ("4×6", "time"), ("MapTap", "guesses")):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    store.add_game_to_registry(name, metric, "U1", "2026-10-08")


if __name__ == "__main__":
    unittest.main()
