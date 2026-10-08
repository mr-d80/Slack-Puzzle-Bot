import threading
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import finalization
import recap_commands


DAY = "2026-10-08"


class _Store:
    def __init__(self, *, posted=False, claim_succeeds=True, block_rebuild=False):
        self.posted = posted
        self.claim_succeeds = claim_succeeds
        self._state_lock = threading.Lock()
        self.mark_calls = 0
        self.replace_calls = 0
        self.rebuilds = 0
        self.updates = []
        self.summary = {DAY: {"prior": True}} if posted else {}
        self.records = [{"day": DAY, "user_id": "U1", "game": "Wordle", "puzzle_id": "123"}]
        self.block_rebuild = block_rebuild
        self.rebuild_entered = threading.Event()
        self.release_rebuild = threading.Event()

    def day_already_posted(self, day):
        with self._state_lock:
            return self.posted

    def load_scores_for_day(self, day):
        return [dict(row) for row in self.records]

    def load_monthly_totals_map(self, _start, _end):
        return {}

    def mark_day_posted(self, day, summary):
        with self._state_lock:
            self.mark_calls += 1
            if self.posted or not self.claim_succeeds:
                self.posted = True
                return False
            self.posted = True
            self.summary[day] = dict(summary)
            return True

    def replace_day_summary(self, day, summary):
        with self._state_lock:
            self.replace_calls += 1
            self.posted = True
            self.summary[day] = dict(summary)
            return True

    def rebuild_totals_from_daily(self):
        self.rebuilds += 1
        if self.block_rebuild and self.rebuilds == 1:
            self.rebuild_entered.set()
            if not self.release_rebuild.wait(timeout=5):
                raise TimeoutError("test did not release blocked finalizer")

    def update_day_summary(self, day, updates):
        self.updates.append(dict(updates))
        self.summary.setdefault(day, {}).update(updates)


class _Client:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"{len(self.posts)}.000"}


class _ObservedRLock:
    """Signal when a second finalizer reaches the lock while the first holds it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.guard = threading.Lock()
        self.attempts = 0
        self.second_attempt = threading.Event()

    def __enter__(self):
        with self.guard:
            self.attempts += 1
            if self.attempts == 2:
                self.second_attempt.set()
        self.lock.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.lock.release()


@contextmanager
def _patched_finalization_dependencies():
    with ExitStack() as stack:
        stack.enter_context(patch.object(finalization, "is_day_closed", return_value=True))
        stack.enter_context(patch.object(finalization, "choose_primary_puzzle_ids", return_value={"Wordle": 123}))
        stack.enter_context(patch.object(finalization, "filter_records_to_primary_puzzles", side_effect=lambda rows, _primary: rows))
        stack.enter_context(patch.object(finalization, "expected_players_for_day", return_value=1))
        stack.enter_context(patch.object(finalization, "count_complete_players", return_value=1))
        stack.enter_context(patch.object(finalization, "move_future_puzzle_scores", return_value=False))
        stack.enter_context(patch.object(finalization, "compute_daily_winners", return_value=({}, {}, {})))
        stack.enter_context(patch.object(finalization, "build_daily_facts", return_value={}))
        stack.enter_context(patch.object(finalization, "build_daily_recap_text", return_value="daily recap"))
        stack.enter_context(patch.object(finalization, "format_summary", return_value="daily standings"))
        stack.enter_context(patch.object(finalization, "month_range_for_day_key", return_value=("2026-10-01", "2026-10-31")))
        stack.enter_context(patch.object(finalization, "finalize_due_months", return_value=None))
        yield


class FinalizationClaimTests(unittest.TestCase):
    def test_lost_daily_claim_stops_before_totals_slack_and_status_posted(self):
        store = _Store(claim_succeeds=False)
        client = _Client()

        with _patched_finalization_dependencies():
            status = finalization.finalize_day(DAY, "C1", store, client, post=True)

        self.assertEqual(status, "already_posted")
        self.assertEqual(store.mark_calls, 1)
        self.assertEqual(store.rebuilds, 0)
        self.assertEqual(store.updates, [])
        self.assertEqual(client.posts, [])

    def test_overlapping_finalizers_claim_once_and_only_one_posts(self):
        store = _Store(block_rebuild=True)
        client = _Client()
        observed_lock = _ObservedRLock()
        results = []
        errors = []

        def run():
            try:
                results.append(finalization.finalize_day(
                    DAY, "C1", store, client, post=True, post_recap=False
                ))
            except Exception as exc:  # surfaced to the test thread below
                errors.append(exc)

        with _patched_finalization_dependencies():
            with patch.object(finalization, "_finalize_lock", observed_lock):
                first = threading.Thread(target=run)
                second = threading.Thread(target=run)
                first.start()
                self.assertTrue(store.rebuild_entered.wait(timeout=3))
                second.start()
                self.assertTrue(observed_lock.second_attempt.wait(timeout=3))
                store.release_rebuild.set()
                first.join(timeout=3)
                second.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ["posted", "already_posted"])
        self.assertEqual(store.mark_calls, 1)
        self.assertEqual(store.rebuilds, 1)
        self.assertEqual([post["text"] for post in client.posts], ["daily standings"])

    def test_explicit_force_replaces_existing_summary_and_per_call_delivery_controls(self):
        store = _Store(posted=True)
        client = _Client()

        with _patched_finalization_dependencies():
            with patch.object(finalization, "DAILY_RECAP_IN_THREAD", True):
                status = finalization.finalize_day(
                    DAY,
                    "C1",
                    store,
                    client,
                    post=True,
                    force=True,
                    post_scores=False,
                    post_recap=True,
                    now_utc=datetime(2026, 10, 9, tzinfo=timezone.utc),
                )

        self.assertEqual(status, "posted")
        self.assertEqual(store.replace_calls, 1)
        self.assertEqual(store.mark_calls, 0)
        self.assertEqual(store.rebuilds, 1)
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0]["text"], "daily recap")
        self.assertNotIn("thread_ts", client.posts[0])

    def test_default_recap_setting_and_no_post_delivery(self):
        store = _Store()
        client = _Client()

        with _patched_finalization_dependencies():
            with patch.object(finalization, "POST_DAILY_RECAP", False):
                status = finalization.finalize_day(DAY, "C1", store, client, post=True)

        self.assertEqual(status, "posted")
        self.assertEqual([post["text"] for post in client.posts], ["daily standings"])

        store = _Store()
        client = _Client()
        with _patched_finalization_dependencies():
            status = finalization.finalize_day(
                DAY, "C1", store, client, post=False, post_scores=True, post_recap=True
            )
        self.assertEqual(status, "posted")
        self.assertEqual(store.mark_calls, 1)
        self.assertEqual(store.rebuilds, 1)
        self.assertEqual(client.posts, [])

    def test_finalization_lock_is_released_after_an_exception(self):
        store = _Store()
        client = _Client()

        with _patched_finalization_dependencies():
            with patch.object(finalization, "_finalize_lock", threading.Lock()):
                with patch.object(finalization, "format_summary", side_effect=RuntimeError("format failed")):
                    with self.assertRaisesRegex(RuntimeError, "format failed"):
                        finalization.finalize_day(DAY, "C1", store, client)

                self.assertEqual(finalization.finalize_day(DAY, "C1", store, client), "posted")

    def test_forced_recap_command_does_not_post_payload_when_claim_was_lost(self):
        class App:
            def __init__(self):
                self.handler = None

            def command(self, _name):
                def decorator(handler):
                    self.handler = handler
                return decorator

        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        app = App()
        finalize_calls = []
        def lose_claim(day, channel, post=True, force=False):
            finalize_calls.append((day, channel, post, force))
            return "already_posted"
        bot = SimpleNamespace(finalize_day=lose_claim)
        recap_commands.register_recap_commands(app, bot)
        respond = Mock()
        client = SimpleNamespace(chat_postMessage=Mock())

        with patch.object(recap_commands, "command_access_error", return_value=None), \
             patch.object(
                 recap_commands,
                 "_resolve_day_arg",
                 return_value=SimpleNamespace(day=DAY, repost=False, force=True, with_scores=True),
             ), \
             patch.object(recap_commands.threading, "Thread", ImmediateThread), \
             patch.object(recap_commands, "_load_daily_summary_payload") as load_payload:
            app.handler(
                ack=Mock(),
                respond=respond,
                body={"channel_id": "C1", "user_id": "U1", "text": f"{DAY} --force"},
                client=client,
                logger=Mock(),
            )

        self.assertEqual(finalize_calls, [(DAY, "C1", False, True)])
        load_payload.assert_not_called()
        client.chat_postMessage.assert_not_called()
        self.assertTrue(any(
            "finalized while this request was running" in call.args[0]
            for call in respond.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
