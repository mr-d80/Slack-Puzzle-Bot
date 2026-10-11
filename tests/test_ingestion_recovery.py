"""Offline regression tests for live score ingestion recovery."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import game_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeStore:
    def __init__(self):
        self.claimed = []
        self.logged = []
        self.upsert_score = Mock()

    def claim_event(self, event_id):
        self.claimed.append(event_id)
        return True

    def log_event(self, event_id, body):
        self.logged.append((event_id, body))


class IngestionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.lifecycle = []
        self.games_patch = patch.object(
            game_registry.game_registry,
            "games",
            game_registry.game_registry.games_as_tuples() + [("Wordle", "guesses")],
        )
        self.games_patch.start()
        game_registry.game_registry._rebuild_regexes()
        self.addCleanup(game_registry.game_registry._rebuild_regexes)
        self.addCleanup(self.games_patch.stop)
        module_name = f"score_bot_ingestion_test_{id(self)}"
        lifecycle = self.lifecycle
        store = _FakeStore()

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

        fake_sheet_store = types.ModuleType("sheet_store")
        fake_sheet_store.SheetStore = lambda *_args, **_kwargs: store
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

        self.validation_patch = patch.object(
            config, "validate_runtime_config", lambda: lifecycle.append("validate")
        )
        self.modules_patch = patch.dict(
            sys.modules,
            {
                "sheet_store": fake_sheet_store,
                "slack_bolt": fake_slack_bolt,
                "slack_bolt.adapter": fake_slack_adapter,
                "slack_bolt.adapter.socket_mode": fake_slack_socket_mode,
                "slack_sdk": fake_slack_sdk,
                "slack_sdk.web": fake_slack_web,
            },
        )
        self.registry_patch = patch.object(game_registry.game_registry, "rebuild", Mock())
        for patcher in (self.validation_patch, self.modules_patch, self.registry_patch):
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
        self.bot.day_key_from_ts = Mock(return_value="2026-10-09")
        self.bot.finalize_due_days = Mock()
        self.store = self.bot.store
        self.logger = Mock()

    def _message(self, event_id, text):
        event = {"channel": "C-SCORES", "user": "U1", "ts": "100.0", "text": text}
        body = {"event_id": event_id, "token": "secret-token", "event": event}
        self.bot.handle_message(body=body, event=event, logger=self.logger)
        return body

    def test_day_resolution_failure_spools_before_save_and_success_uses_resolved_day(self):
        score_text = "Wordle #1,234 - 4/6"
        self.bot.resolve_score_day = Mock(side_effect=RuntimeError("provider failure"))
        with patch.object(self.bot, "spool_jsonl") as spool:
            self._message("E-RESOLVE", score_text)

        self.store.upsert_score.assert_not_called()
        self.bot.finalize_due_days.assert_not_called()
        spool.assert_called_once()
        path, record = spool.call_args.args
        self.assertEqual(Path(path).resolve().parent, REPO_ROOT.resolve())
        self.assertEqual(record["message_day"], "2026-10-09")
        self.assertIsNone(record["resolved_day"])
        self.assertIsNone(record["day"])
        self.assertEqual(record["event_id"], "E-RESOLVE")
        self.assertEqual(record["raw_text"], score_text)
        self.assertEqual(record["failed_stage"], "day_resolution")
        self.assertEqual(record["parsed"]["game"], "Wordle")
        self.assertNotIn("token", record["payload"])
        self.assertEqual(self.store.claimed, ["E-RESOLVE"])
        self.assertEqual([arg for arg in self.logger.error.call_args.args if isinstance(arg, str) and score_text in arg], [])

        self.bot.resolve_score_day = Mock(return_value="2026-10-08")
        self.logger.reset_mock()
        with patch.object(self.bot, "spool_jsonl") as spool:
            self._message("E-SUCCESS", score_text)
        spool.assert_not_called()
        self.store.upsert_score.assert_called_once()
        self.assertEqual(self.store.upsert_score.call_args.args[0], "2026-10-08")
        self.bot.finalize_due_days.assert_called_once_with("C-SCORES", post=True)

    def test_pinpoint_store_lookup_failure_is_preserved(self):
        self.bot.resolve_pinpoint_fail_score = Mock(side_effect=RuntimeError("lookup failed"))
        with patch.object(self.bot, "spool_jsonl") as spool:
            self._message("E-PINPOINT", "Pinpoint fail")

        self.store.upsert_score.assert_not_called()
        self.bot.finalize_due_days.assert_not_called()
        spool.assert_called_once()
        record = spool.call_args.args[1]
        self.assertEqual(record["message_day"], "2026-10-09")
        self.assertIsNone(record["resolved_day"])
        self.assertEqual(record["failed_stage"], "pinpoint_lookup")
        self.assertEqual(record["raw_text"], "Pinpoint fail")
        self.assertEqual(record["event_id"], "E-PINPOINT")

    def test_score_parse_failure_is_spooled_without_logging_raw_text(self):
        score_text = "Wordle #1,234 - 4/6"
        self.bot.parse_score_for_day = Mock(side_effect=ValueError("parser failure"))
        with patch.object(self.bot, "spool_jsonl") as spool:
            self._message("E-PARSE", score_text)

        self.store.upsert_score.assert_not_called()
        self.bot.finalize_due_days.assert_not_called()
        spool.assert_called_once()
        record = spool.call_args.args[1]
        self.assertEqual(record["failed_stage"], "score_parse")
        self.assertEqual(record["raw_text"], score_text)
        self.assertIsNone(record["parsed"])
        self.assertNotIn(score_text, repr(self.logger.error.call_args.args))


if __name__ == "__main__":
    unittest.main()