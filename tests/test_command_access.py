import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import command_access
import recap_commands
import reconcile_day
import update_commands


class _FakeApp:
    def __init__(self):
        self.handlers = {}

    def command(self, name):
        def register(handler):
            self.handlers[name] = handler
            return handler

        return register


class _NoStoreBot:
    SCORE_CHANNEL_ID = "C-SCORES"
    ADMIN_USER_IDS = frozenset()
    SCORE_DAY_TZ = timezone.utc

    @property
    def store(self):
        raise AssertionError("denied command accessed the store")

    @property
    def game_registry(self):
        raise AssertionError("denied command accessed the game registry")


class CommandAccessTests(unittest.TestCase):
    def test_missing_channel_configuration_denies_access(self):
        bot = SimpleNamespace(SCORE_CHANNEL_ID="", ADMIN_USER_IDS=frozenset())

        denial = command_access.command_access_error(
            {"channel_id": "C-SCORES", "user_id": "U1"}, bot
        )

        self.assertEqual(denial, command_access.CHANNEL_DENIED_MESSAGE)

    def test_only_exact_configured_channel_is_allowed(self):
        bot = SimpleNamespace(SCORE_CHANNEL_ID="C-SCORES", ADMIN_USER_IDS=frozenset())

        self.assertIsNone(command_access.command_access_error(
            {"channel_id": "C-SCORES", "user_id": "U-FRIEND"}, bot
        ))
        self.assertEqual(
            command_access.command_access_error(
                {"channel_id": "C-OTHER", "user_id": "U-FRIEND"}, bot
            ),
            command_access.CHANNEL_DENIED_MESSAGE,
        )
        self.assertEqual(
            command_access.command_access_error(
                {"user_id": "U-FRIEND"}, bot
            ),
            command_access.CHANNEL_DENIED_MESSAGE,
        )

    def test_configured_admin_list_restricts_users(self):
        bot = SimpleNamespace(
            SCORE_CHANNEL_ID="C-SCORES",
            ADMIN_USER_IDS=frozenset({"U-ADMIN"}),
        )

        self.assertIsNone(command_access.command_access_error(
            {"channel_id": "C-SCORES", "user_id": "U-ADMIN"}, bot
        ))
        self.assertEqual(
            command_access.command_access_error(
                {"channel_id": "C-SCORES", "user_id": "U-FRIEND"}, bot
            ),
            command_access.USER_DENIED_MESSAGE,
        )

    def test_update_denial_happens_before_command_dispatch_or_store_access(self):
        app = _FakeApp()
        bot = _NoStoreBot()
        update_commands.register_update_commands(app, bot)
        ack = Mock()
        respond = Mock()
        logger = Mock()

        with patch.object(update_commands, "_handle_new_game") as handle_new_game:
            app.handlers["/jw-update"](
                ack=ack,
                respond=respond,
                body={
                    "channel_id": "C-OTHER",
                    "user_id": "U1",
                    "text": "new game: Example, time",
                },
                client=Mock(),
                logger=logger,
            )

        ack.assert_called_once_with()
        respond.assert_called_once_with(command_access.CHANNEL_DENIED_MESSAGE)
        handle_new_game.assert_not_called()

    def test_recap_denial_happens_before_recap_work_or_store_access(self):
        app = _FakeApp()
        bot = _NoStoreBot()
        recap_commands.register_recap_commands(app, bot)
        ack = Mock()
        respond = Mock()

        with patch.object(recap_commands, "_resolve_day_arg", side_effect=AssertionError("parsed denied command")):
            app.handlers["/jw-recap"](
                ack=ack,
                respond=respond,
                body={"channel_id": "", "user_id": "U1", "text": "2026-09-01 --force"},
                client=Mock(),
                logger=Mock(),
            )

        ack.assert_called_once_with()
        respond.assert_called_once_with(command_access.CHANNEL_DENIED_MESSAGE)

    def test_authorized_update_dispatches_new_game_handler(self):
        app = _FakeApp()
        bot = _NoStoreBot()
        update_commands.register_update_commands(app, bot)
        respond = Mock()
        logger = Mock()

        with patch.object(update_commands, "_handle_new_game") as handle_new_game:
            app.handlers["/jw-update"](
                ack=Mock(),
                respond=respond,
                body={
                    "channel_id": "C-SCORES",
                    "user_id": "U-FRIEND",
                    "text": "new game: Example, time",
                },
                client=Mock(),
                logger=logger,
            )

        self.assertEqual(handle_new_game.call_count, 1)
        self.assertEqual(handle_new_game.call_args.args[1], "U-FRIEND")

    def test_authorized_recap_dispatches_background_work(self):
        app = _FakeApp()
        bot = _NoStoreBot()
        recap_commands.register_recap_commands(app, bot)
        started = []

        class CapturedThread:
            def __init__(self, *, target, daemon):
                self.target = target
                self.daemon = daemon

            def start(self):
                started.append(self)

        with patch.object(recap_commands.threading, "Thread", CapturedThread):
            app.handlers["/jw-recap"](
                ack=Mock(),
                respond=Mock(),
                body={"channel_id": "C-SCORES", "user_id": "U-FRIEND", "text": "today"},
                client=Mock(),
                logger=Mock(),
            )

        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].daemon)

    def test_new_game_effective_date_uses_score_day_timezone(self):
        scoring_tz = timezone(timedelta(hours=-8))
        calls = []

        class Store:
            def add_game_to_registry(self, **kwargs):
                calls.append(kwargs)

        class Registry:
            def rebuild(self, _store):
                pass

        class FakeDateTime:
            @staticmethod
            def now(tz=None):
                return datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc).astimezone(tz)

        bot = SimpleNamespace(
            store=Store(),
            game_registry=Registry(),
            SCORE_DAY_TZ=scoring_tz,
            TZ=timezone.utc,
        )

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        match = update_commands._NEW_GAME_RE.match("new game: Example, time")
        self.assertIsNotNone(match)
        with patch.object(update_commands, "datetime", FakeDateTime), patch.object(
            update_commands.threading, "Thread", ImmediateThread
        ):
            update_commands._handle_new_game(match, "U1", Mock(), bot, Mock())

        self.assertEqual(calls[0]["effective_date"], "2026-01-01")

    def test_event_replay_requires_a_source_channel_before_store_access(self):
        class NoChannelBot:
            SCORE_CHANNEL_ID = ""

            @property
            def store(self):
                raise AssertionError("read store")

        with self.assertRaisesRegex(ValueError, "source channel"):
            reconcile_day.replay_events_for_day(NoChannelBot(), "2026-09-01")


if __name__ == "__main__":
    unittest.main()
