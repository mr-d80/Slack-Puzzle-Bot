"""SlackResponse is not a dict; these guard the helpers that used to assume it was."""

import os
import unittest

import monthly_summary as ms
from slack_safe import dm_channel_id, message_ts

os.environ["AI_REWRITE_ENABLED"] = "0"


class _FakeSlackResponse:
    """Stands in for slack_sdk.web.SlackResponse: .get() works, not a dict."""

    def __init__(self, data):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data[key]


class TestSlackResponseIsNotADict(unittest.TestCase):
    def test_the_real_sdk_type_is_not_a_dict(self):
        """The premise of the bug. If this ever changes, the helper is redundant."""
        from slack_sdk.web import SlackResponse

        self.assertFalse(issubclass(SlackResponse, dict))
        self.assertTrue(hasattr(SlackResponse, "get"))

    def test_the_old_guard_would_have_dropped_the_ts(self):
        resp = _FakeSlackResponse({"ok": True, "ts": "1234.5678"})
        self.assertEqual((resp.get("ts") or "").strip() if isinstance(resp, dict) else "", "")
        self.assertEqual(message_ts(resp), "1234.5678")


class TestMessageTs(unittest.TestCase):
    def test_reads_a_slack_response(self):
        self.assertEqual(message_ts(_FakeSlackResponse({"ts": "1.1"})), "1.1")

    def test_still_reads_a_plain_dict(self):
        self.assertEqual(message_ts({"ts": "2.2"}), "2.2")

    def test_degrades_to_empty_string(self):
        for bad in (None, object(), _FakeSlackResponse({}), _FakeSlackResponse({"ts": None}), {}):
            self.assertEqual(message_ts(bad), "")

    def test_strips_and_stringifies(self):
        self.assertEqual(message_ts({"ts": "  3.3  "}), "3.3")
        self.assertEqual(message_ts({"ts": 4}), "4")


class TestDmChannelId(unittest.TestCase):
    def test_reads_nested_channel_id(self):
        resp = _FakeSlackResponse({"ok": True, "channel": {"id": "D123"}})
        self.assertEqual(dm_channel_id(resp), "D123")

    def test_degrades_to_empty_string(self):
        for bad in (None, object(), _FakeSlackResponse({}),
                    _FakeSlackResponse({"channel": None}),
                    _FakeSlackResponse({"channel": "not-a-dict"})):
            self.assertEqual(dm_channel_id(bad), "")


class _SdkStyleClient:
    """Posts back SlackResponse-alikes, the way the real client does."""

    def __init__(self):
        self.posts = []
        self._n = 0

    def chat_postMessage(self, channel, text, thread_ts=None):
        self._n += 1
        ts = f"{self._n}.000"
        self.posts.append({"text": text, "thread_ts": thread_ts, "ts": ts})
        return _FakeSlackResponse({"ok": True, "ts": ts, "channel": channel})


class TestMonthlyThreadingAgainstSdkResponses(unittest.TestCase):
    """The regression: with a real SlackResponse, everything must still thread."""

    def _store(self):
        # Local import keeps the shared fakes in one place.
        from test_monthly_summary import _Store, _day_payload

        return _Store({
            "2026-06-01": _day_payload("2026-06-01", {"U1": (2, 0), "U2": (0, 1)}),
            "2026-06-30": _day_payload("2026-06-30", {"U1": (1, 0), "U2": (1, 0)}),
        })

    def test_replies_are_threaded_under_the_standings_post(self):
        from datetime import datetime, timezone

        store, client = self._store(), _SdkStyleClient()
        ms.finalize_month(
            "2026-06", "C1", store, client,
            now_utc=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
        )

        self.assertGreater(len(client.posts), 1)
        parent = client.posts[0]
        self.assertIsNone(parent["thread_ts"])
        for reply in client.posts[1:]:
            self.assertEqual(reply["thread_ts"], parent["ts"])

    def test_the_slack_ts_is_recorded_in_the_ledger(self):
        from datetime import datetime, timezone

        store, client = self._store(), _SdkStyleClient()
        ms.finalize_month(
            "2026-06", "C1", store, client,
            now_utc=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(store.updates, "update_month_summary was never called")
        _month, updates = store.updates[-1]
        self.assertEqual(updates["slack_month_results_ts"], client.posts[0]["ts"])
        self.assertTrue(updates["slack_month_recap_ts"])


if __name__ == "__main__":
    unittest.main()
