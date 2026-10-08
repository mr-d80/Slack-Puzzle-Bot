import json
import os
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


DAY = "2026-10-08"


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def get_all_values(self):
        return self.rows


class _Store:
    def __init__(self, *, posted=False):
        self.events = _Rows([["payload_json"]])
        self.posted = posted
        self.finalize_calls = []
        self.upserts = []
        self.events_written = []

    def bulk_upsert_scores(self, rows):
        self.upserts.extend(rows)
        return len(rows)

    def bulk_log_events(self, rows):
        self.events_written.extend(rows)

    def seen_event(self, _event_id):
        return False

    def day_already_posted(self, _day):
        return self.posted

    def load_scores_for_day(self, _day):
        return [{"user_id": "U1", "game": "Wordle", "puzzle_id": "123", "slack_ts": "1.0"}]

    def load_monthly_totals_map(self, _start, _end):
        return {}

    def load_totals_map(self):
        return {}


class _Client:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": str(len(self.posts))}


def _bot(*, posted=False):
    store = _Store(posted=posted)
    client = _Client()

    def finalize_day(day, channel, **kwargs):
        store.finalize_calls.append((day, channel, kwargs))
        if kwargs.get("post"):
            day_ts = ""
            if kwargs.get("post_scores", True):
                day_ts = client.chat_postMessage(channel=channel, text="standings")["ts"]
            post_recap = kwargs.get("post_recap")
            if post_recap is None:
                post_recap = bot.POST_DAILY_RECAP
            if post_recap:
                recap_kwargs = {"channel": channel, "text": "recap"}
                if bot.DAILY_RECAP_IN_THREAD and day_ts:
                    recap_kwargs["thread_ts"] = day_ts
                client.chat_postMessage(**recap_kwargs)
        return "finalized"

    bot = SimpleNamespace(
        store=store,
        client=client,
        SCORE_CHANNEL_ID="C1",
        POST_DAILY_RECAP=True,
        DAILY_RECAP_IN_THREAD=False,
        day_key_from_ts=lambda _ts: DAY,
        parse_score=lambda _text: None,
        normalize_game=lambda game: game,
        finalize_day=finalize_day,
        choose_primary_puzzle_ids=lambda _records: {"Wordle": 123},
        filter_records_to_primary_puzzles=lambda records, _primary: records,
        compute_daily_winners=lambda _records, day: ({}, {}, {}),
        format_summary=lambda *_args, **_kwargs: "standings",
        expected_players_for_day=lambda _day: 1,
        count_complete_players=lambda _records, day: 1,
        build_daily_facts=lambda **_kwargs: {},
        build_daily_recap_text=lambda _facts: "recap",
        _month_range_for_day_key=lambda _day: ("2026-10-01", DAY),
    )
    return bot


class CliControlTests(unittest.TestCase):
    def test_replay_only_returns_before_ledger_or_slack_effects(self):
        bot = _bot()
        bot.store.events.rows.append([json.dumps({
            "event": {"channel": "C1", "user": "U1", "ts": "1.0", "text": "score"}
        })])
        bot.parse_score = lambda _text: SimpleNamespace(score_day=None)

        rebuilt, status = reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            finalize=False,
            post=True,
            sync_slack_history=False,
        )

        self.assertEqual(rebuilt, 1)
        self.assertIn("finalization skipped", status)
        self.assertEqual(len(bot.store.upserts), 1)
        self.assertEqual(bot.store.upserts[0][:2], (DAY, "U1"))
        self.assertEqual(bot.store.finalize_calls, [])
        self.assertEqual(bot.client.posts, [])

    def test_no_post_still_finalizes_and_honors_force(self):
        bot = _bot()

        _rebuilt, status = reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            post=False,
            force_finalize=True,
            sync_slack_history=False,
        )

        self.assertEqual(status, "finalized")
        self.assertEqual(bot.store.finalize_calls, [(DAY, "", {"post": False, "force": True})])
        self.assertEqual(bot.client.posts, [])

    def test_force_finalize_takes_precedence_over_repost_shortcut(self):
        bot = _bot(posted=True)

        reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            post=True,
            channel="C-override",
            force_repost=True,
            force_finalize=True,
            sync_slack_history=False,
        )

        self.assertEqual(
            bot.store.finalize_calls,
            [(DAY, "C-override", {
                "post": True, "force": True, "post_scores": True, "post_recap": True,
            })],
        )
        self.assertEqual([post["text"] for post in bot.client.posts], ["standings", "recap"])

    def test_force_repost_posts_standings_and_respects_no_recap(self):
        bot = _bot(posted=True)

        reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            force_repost=True,
            recap=False,
            sync_slack_history=False,
        )

        self.assertEqual([post["text"] for post in bot.client.posts], ["standings"])
        self.assertEqual(bot.store.finalize_calls, [])

    def test_recap_only_repost_posts_only_recap_without_global_config_mutation(self):
        bot = _bot(posted=True)

        with patch.dict(os.environ, {"POST_DAILY_SCORES": "1", "POST_DAILY_RECAP": "0"}):
            reconcile_day.run_reconcile(
                bot=bot,
                day=DAY,
                recap_only=True,
                sync_slack_history=False,
            )
            self.assertEqual(os.environ["POST_DAILY_SCORES"], "1")
            self.assertEqual(os.environ["POST_DAILY_RECAP"], "0")

        self.assertEqual([post["text"] for post in bot.client.posts], ["recap"])
        self.assertEqual(bot.store.finalize_calls, [])
        self.assertFalse(hasattr(bot, "POST_DAILY_SCORES"))

    def test_recap_only_repost_uses_explicit_recap_even_when_configured_off(self):
        bot = _bot(posted=True)
        bot.POST_DAILY_RECAP = False

        reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            recap_only=True,
            sync_slack_history=False,
        )

        self.assertEqual([post["text"] for post in bot.client.posts], ["recap"])
        self.assertEqual(bot.store.finalize_calls, [])

    def test_first_finalize_recap_only_uses_per_call_delivery_options(self):
        bot = _bot()

        reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            recap_only=True,
            sync_slack_history=False,
        )

        self.assertEqual(
            bot.store.finalize_calls,
            [(DAY, "C1", {
                "post": True, "force": False, "post_scores": False, "post_recap": True,
            })],
        )
        self.assertEqual([post["text"] for post in bot.client.posts], ["recap"])

    def test_first_finalize_no_recap_uses_per_call_delivery_option(self):
        bot = _bot()

        reconcile_day.run_reconcile(
            bot=bot,
            day=DAY,
            recap=False,
            sync_slack_history=False,
        )

        self.assertEqual(bot.store.finalize_calls[0][2]["post_recap"], False)
        self.assertEqual([post["text"] for post in bot.client.posts], ["standings"])

    def test_default_recap_setting_is_preserved_without_explicit_override(self):
        bot = _bot()
        bot.POST_DAILY_RECAP = False

        reconcile_day.run_reconcile(bot=bot, day=DAY, sync_slack_history=False)

        self.assertEqual(bot.store.finalize_calls[0][2]["post_recap"], False)
        self.assertEqual([post["text"] for post in bot.client.posts], ["standings"])

    def test_reconcile_help_does_not_load_bot(self):
        with patch.object(sys, "argv", ["reconcile_day.py", "--help"]):
            with patch.object(reconcile_day, "_load_bot_module", side_effect=AssertionError("loaded bot")) as load:
                with self.assertRaises(SystemExit) as caught:
                    reconcile_day.main()
        self.assertEqual(caught.exception.code, 0)
        load.assert_not_called()

    def test_scan_help_does_not_load_bot(self):
        with patch.object(sys, "argv", ["scan_slack_day.py", "--help"]):
            with patch.object(scan_slack_day, "_load_bot_module", side_effect=AssertionError("loaded bot")) as load:
                with self.assertRaises(SystemExit) as caught:
                    scan_slack_day.main()
        self.assertEqual(caught.exception.code, 0)
        load.assert_not_called()

    def _run_scan(self, bot, *args):
        argv = ["scan_slack_day.py", DAY, *args]
        with patch.object(sys, "argv", argv):
            with patch.object(scan_slack_day, "_day_window_utc", return_value=(1.0, 2.0)):
                with patch.object(scan_slack_day, "_fetch_channel_messages", return_value=[]):
                    with patch.object(scan_slack_day, "_load_bot_module", return_value=bot):
                        scan_slack_day.main()

    def test_scan_without_finalize_has_no_ledger_or_slack_effects(self):
        bot = _bot()

        self._run_scan(bot)

        self.assertEqual(bot.store.finalize_calls, [])
        self.assertEqual(bot.client.posts, [])

    def test_scan_finalize_no_post_updates_ledger_without_delivery(self):
        bot = _bot()

        self._run_scan(bot, "--finalize", "--no-post")

        self.assertEqual(
            bot.store.finalize_calls,
            [(DAY, "", {"post": False, "force": False})],
        )
        self.assertEqual(bot.client.posts, [])

    def test_scan_no_reconcile_still_honors_explicit_finalize(self):
        bot = _bot()

        self._run_scan(bot, "--no-reconcile", "--finalize")

        self.assertEqual(
            bot.store.finalize_calls,
            [(DAY, "C1", {
                "post": True, "force": False, "post_scores": True, "post_recap": True,
            })],
        )
        self.assertEqual([post["text"] for post in bot.client.posts], ["standings", "recap"])


if __name__ == "__main__":
    unittest.main()
