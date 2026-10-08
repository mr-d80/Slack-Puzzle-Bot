import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import reconcile_day

try:
    import slack_sdk.errors  # type: ignore[import-not-found]
except ModuleNotFoundError:
    slack_sdk = types.ModuleType("slack_sdk")
    slack_errors = types.ModuleType("slack_sdk.errors")
    slack_errors.SlackApiError = type("SlackApiError", (Exception,), {})
    slack_sdk.errors = slack_errors
    sys.modules["slack_sdk"] = slack_sdk
    sys.modules["slack_sdk.errors"] = slack_errors

import scan_slack_day


class _Worksheet:
    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self):
        return self._rows


class _Store:
    def __init__(self, event_payloads=()):
        self.events = _Worksheet(
            [["event_id", "received_at", "payload_json"]]
            + [[f"E{i}", "", json.dumps(payload)] for i, payload in enumerate(event_payloads)]
        )
        self.upserts = []
        self.logged_events = []

    def upsert_score(self, day, user_id, parsed, slack_ts, text):
        self.upserts.append((day, user_id, parsed.game, slack_ts, text))

    def bulk_upsert_scores(self, upserts):
        for day, user_id, parsed, slack_ts, text in upserts:
            self.upsert_score(day, user_id, parsed, slack_ts, text)
        return len(upserts)

    def seen_event(self, event_id):
        return any(existing_id == event_id for existing_id, _ in self.logged_events)

    def log_event(self, event_id, payload):
        self.logged_events.append((event_id, payload))

    def bulk_log_events(self, events):
        for event_id, payload in events:
            if not self.seen_event(event_id):
                self.log_event(event_id, payload)


def _parsed(text):
    if "Wordle" not in text:
        return None
    return SimpleNamespace(game="Wordle", puzzle_id=123)


class EditedScoreTests(unittest.TestCase):
    def test_events_replay_uses_inner_text_from_message_changed(self):
        payload = {
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C1",
                "message": {"user": "U1", "ts": "100.0", "text": "Wordle 123 4/6"},
            }
        }
        store = _Store([payload])
        bot = SimpleNamespace(
            store=store,
            SCORE_CHANNEL_ID="C1",
            day_key_from_ts=lambda _ts: "2026-07-11",
            parse_score=_parsed,
        )

        rebuilt = reconcile_day.replay_events_for_day(bot, "2026-07-11")

        self.assertEqual(rebuilt, 1)
        self.assertEqual(store.upserts[0][-1], "Wordle 123 4/6")

    @patch.object(scan_slack_day, "_day_window_utc", return_value=(100.0, 200.0))
    @patch.object(scan_slack_day, "_fetch_thread_replies")
    @patch.object(scan_slack_day, "_fetch_channel_messages")
    def test_history_sync_fetches_parent_replies_and_uses_edited_text(
        self, fetch_channel, fetch_replies, _day_window
    ):
        fetch_channel.return_value = [{"ts": "90.0", "reply_count": 1, "text": "daily thread"}]
        fetch_replies.return_value = [
            {
                "user": "U1",
                "ts": "150.0",
                "thread_ts": "90.0",
                "text": "Wordle 123 4/6",
                "edited": {"user": "U1", "ts": "160.0"},
            }
        ]
        store = _Store()
        bot = SimpleNamespace(
            store=store,
            day_key_from_ts=lambda _ts: "2026-07-11",
            parse_score=_parsed,
        )

        rebuilt = scan_slack_day.sync_slack_history_for_day(bot, "2026-07-11", "C1")

        self.assertEqual(rebuilt, 1)
        fetch_replies.assert_called_once_with(bot, "C1", "90.0")
        self.assertEqual(store.upserts[0][-1], "Wordle 123 4/6")
        self.assertEqual(len(store.logged_events), 1)

    def test_history_event_id_changes_when_message_text_is_edited(self):
        before = scan_slack_day.CandidateMsg("C1", "U1", "150.0", "not parseable")
        after = scan_slack_day.CandidateMsg("C1", "U1", "150.0", "Wordle 123 4/6")

        self.assertNotEqual(
            scan_slack_day._history_event_id(before),
            scan_slack_day._history_event_id(after),
        )


if __name__ == "__main__":
    unittest.main()
