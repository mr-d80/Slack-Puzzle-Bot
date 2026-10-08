import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import game_registry
from game_registry import _OPTIONAL_GAMES


REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeStore:
    def __init__(self, lifecycle):
        lifecycle.append("store")
        self.claimed = []
        self.logged = []

    def claim_event(self, event_id):
        self.claimed.append(event_id)
        return True

    def log_event(self, event_id, body):
        self.logged.append((event_id, body))


class MessagePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = []
        module_name = f"score_bot_privacy_test_{id(self)}"

        lifecycle = self.lifecycle

        class FakeApp:
            def __init__(self, token):
                lifecycle.append("app")
                self.handlers = {}

            def event(self, name):
                def register(handler):
                    self.handlers[name] = handler
                    return handler

                return register

            def command(self, name):
                def register(handler):
                    self.handlers[name] = handler
                    return handler

                return register

        class FakeWebClient:
            def __init__(self, token):
                lifecycle.append("client")

        class FakeSocketModeHandler:
            def __init__(self, app, token):
                self.app = app
                self.token = token

        class FakeStore(_FakeStore):
            def __init__(self, spreadsheet_id, service_account_file):
                super().__init__(lifecycle)

        fake_sheet_store = types.ModuleType("sheet_store")
        fake_sheet_store.SheetStore = FakeStore
        fake_slack_bolt = types.ModuleType("slack_bolt")
        fake_slack_bolt.App = FakeApp
        fake_slack_adapter = types.ModuleType("slack_bolt.adapter")
        fake_slack_socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
        fake_slack_socket_mode.SocketModeHandler = FakeSocketModeHandler
        fake_slack_sdk = types.ModuleType("slack_sdk")
        fake_slack_sdk.__path__ = []
        fake_slack_web = types.ModuleType("slack_sdk.web")
        fake_slack_web.WebClient = FakeWebClient
        fake_slack_bolt.adapter = fake_slack_adapter
        fake_slack_adapter.socket_mode = fake_slack_socket_mode
        fake_slack_sdk.web = fake_slack_web

        def validate_runtime_config():
            lifecycle.append("validate")

        self.module_name = module_name
        self.validation_patch = patch.object(config, "validate_runtime_config", validate_runtime_config)
        self.store_patch = patch.dict(sys.modules, {"sheet_store": fake_sheet_store})
        self.slack_patch = patch.dict(
            sys.modules,
            {
                "slack_bolt": fake_slack_bolt,
                "slack_bolt.adapter": fake_slack_adapter,
                "slack_bolt.adapter.socket_mode": fake_slack_socket_mode,
                "slack_sdk": fake_slack_sdk,
                "slack_sdk.web": fake_slack_web,
            },
        )
        self.registry_patch = patch.object(game_registry.game_registry, "rebuild", Mock())
        for patcher in (self.validation_patch, self.store_patch, self.slack_patch, self.registry_patch):
            patcher.start()
            self.addCleanup(patcher.stop)

        path = REPO_ROOT / "score-bot.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(lambda: sys.modules.pop(module_name, None))
        spec.loader.exec_module(module)
        self.bot = module
        self.bot.SCORE_CHANNEL_ID = "C-SCORES"
        self.bot.NL_QUERY_ENABLED = False

        self.assertLess(self.lifecycle.index("validate"), self.lifecycle.index("app"))
        self.assertLess(self.lifecycle.index("validate"), self.lifecycle.index("client"))
        self.assertLess(self.lifecycle.index("validate"), self.lifecycle.index("store"))

    def _message(self, *, event_id, event):
        self.bot.handle_message(
            body={"event_id": event_id},
            event=event,
            logger=Mock(),
        )

    def test_other_channel_message_is_not_claimed_or_stored(self):
        self._message(
            event_id="E-OTHER",
            event={"channel": "C-OTHER", "user": "U1", "ts": "100.0", "text": "private text"},
        )

        self.assertEqual(self.bot.store.claimed, [])
        self.assertEqual(self.bot.store.logged, [])

    def test_missing_configured_channel_fails_closed(self):
        self.bot.SCORE_CHANNEL_ID = ""
        self._message(
            event_id="E-NO-CONFIG",
            event={"channel": "C-SCORES", "user": "U1", "ts": "100.0", "text": "ordinary chat"},
        )

        self.assertEqual(self.bot.store.claimed, [])
        self.assertEqual(self.bot.store.logged, [])

    def test_score_channel_history_keeps_human_messages_edits_and_thread_broadcasts(self):
        events = [
            {"channel": "C-SCORES", "user": "U1", "ts": "100.0", "text": "ordinary chat"},
            {
                "channel": "C-SCORES",
                "subtype": "message_changed",
                "message": {"user": "U2", "ts": "101.0", "text": "edited score"},
            },
            {
                "channel": "C-SCORES",
                "subtype": "thread_broadcast",
                "user": "U3",
                "ts": "102.0",
                "text": "thread score",
            },
        ]
        for index, event in enumerate(events):
            self._message(event_id=f"E-{index}", event=event)

        self.assertEqual(self.bot.store.claimed, ["E-0", "E-1", "E-2"])
        self.assertEqual([item[0] for item in self.bot.store.logged], ["E-0", "E-1", "E-2"])

    def test_bot_and_unsupported_subtype_are_skipped_before_storage(self):
        events = [
            {"channel": "C-SCORES", "subtype": "bot_message", "user": "U-BOT", "ts": "100.0", "text": "bot"},
            {"channel": "C-SCORES", "bot_id": "B1", "user": "U-BOT", "ts": "101.0", "text": "bot"},
            {"channel": "C-SCORES", "subtype": "message_deleted", "user": "U1", "ts": "102.0", "text": "deleted"},
        ]
        for index, event in enumerate(events):
            self._message(event_id=f"E-BOT-{index}", event=event)

        self.assertEqual(self.bot.store.claimed, [])
        self.assertEqual(self.bot.store.logged, [])

    def test_live_handler_persists_native_maptap_date_and_failed_wordle_status(self):
        registry = game_registry.game_registry
        original_games = registry.games_as_tuples()
        self.bot.day_key_from_ts = Mock(return_value="2026-10-09")
        self.bot.bump_day_if_future_puzzle = Mock(side_effect=lambda day, _parsed, store_obj=None: day)
        self.bot.store.upsert_score = Mock()
        self.bot.finalize_due_days = Mock()

        try:
            with patch.object(registry, "games", original_games + list(_OPTIONAL_GAMES)):
                registry._rebuild_regexes()
                self._message(
                    event_id="E-MAPTAP-LATE",
                    event={
                        "channel": "C-SCORES", "user": "U1", "ts": "100.0",
                        "text": (
                            "www.maptap.gg October 8\n"
                            "100:dart: 89:tada: 100:dart: 90:crown: 93:trophy:\n"
                            "Final score: 938"
                        ),
                    },
                )
                self._message(
                    event_id="E-WORDLE-FAILED",
                    event={
                        "channel": "C-SCORES", "user": "U2", "ts": "101.0",
                        "text": "Wordle 1,234 X/6",
                    },
                )
        finally:
            registry._rebuild_regexes()

        self.assertEqual(self.bot.store.upsert_score.call_count, 2)
        map_day, map_user, map_score, _map_ts, _map_text = self.bot.store.upsert_score.call_args_list[0].args
        self.assertEqual((map_day, map_user), ("2026-10-08", "U1"))
        self.assertEqual((map_score.game, map_score.metric_type, map_score.metric_value), ("MapTap", "points", 938))
        self.assertEqual(map_score.status, "solved")

        wordle_day, wordle_user, wordle_score, _wordle_ts, _wordle_text = self.bot.store.upsert_score.call_args_list[1].args
        self.assertEqual((wordle_day, wordle_user), ("2026-10-09", "U2"))
        self.assertEqual((wordle_score.game, wordle_score.metric_type, wordle_score.status), ("Wordle", "guesses", "failed"))
        self.assertEqual(wordle_score.metric_value, 106)
        self.assertEqual(self.bot.finalize_due_days.call_count, 2)


if __name__ == "__main__":
    unittest.main()
