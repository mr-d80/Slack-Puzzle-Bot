import threading
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import diagnostics
import finalization


DAY = "2026-10-08"


class _ManualTimer:
    instances = []

    def __init__(self, _interval, function):
        self.function = function
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.joined = False
        type(self).instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def join(self):
        self.joined = True

    def fire(self):
        self.function()


class _Store:
    def __init__(self, *, block_load=False):
        self.posted = False
        self.block_load = block_load
        self.load_entered = threading.Event()
        self.release_load = threading.Event()
        self.records = [{"day": DAY, "user_id": "U1", "game": "Wordle", "puzzle_id": "123"}]
        self.rebuilds = 0
        self.updated = []

    def list_days_with_scores(self):
        return [DAY]

    def day_already_posted(self, _day):
        return self.posted

    def load_scores_for_day(self, _day):
        if self.block_load:
            self.load_entered.set()
            if not self.release_load.wait(timeout=5):
                raise TimeoutError("test did not release the fake Sheets operation")
        return list(self.records)

    def load_monthly_totals_map(self, _start, _end):
        return {}

    def mark_day_posted(self, _day, _summary):
        if self.posted:
            return False
        self.posted = True
        return True

    def replace_day_summary(self, _day, _summary):
        self.posted = True
        return True

    def rebuild_totals_from_daily(self):
        self.rebuilds += 1

    def update_day_summary(self, day, updates):
        self.updated.append((day, updates))


class _Client:
    def chat_postMessage(self, **_kwargs):
        return {"ts": "1.000"}


def _patched_dependencies(stack):
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
    stack.enter_context(patch.object(finalization, "POST_DAILY_RECAP", False))


class FinalizationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        _ManualTimer.instances.clear()

    def _patch_timer(self, stack):
        stack.enter_context(patch.object(diagnostics.threading, "Timer", _ManualTimer))

    def test_stalled_sheets_operation_dumps_phase_and_lock_is_reusable(self):
        store = _Store(block_load=True)
        errors = []
        client = _Client()
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            self._patch_timer(stack)
            stack.enter_context(patch.object(finalization, "_PROJECT_DIR", Path(temp_dir)))
            dump = Mock()
            stack.enter_context(patch.object(diagnostics.faulthandler, "dump_traceback", dump))
            _patched_dependencies(stack)

            def run_finalization():
                try:
                    finalization.finalize_day(DAY, "C1", store, client, post=False)
                except Exception as exc:  # surfaced to the test thread below
                    errors.append(exc)

            worker = threading.Thread(target=run_finalization)
            worker.start()
            self.assertTrue(store.load_entered.wait(timeout=2))
            self.assertEqual(len(_ManualTimer.instances), 1)

            with patch.object(finalization.logger, "info") as skip_log:
                finalization.finalize_due_days("C1", store, client, post=False)
            skip_args = skip_log.call_args.args
            self.assertEqual(skip_args[1], DAY)
            self.assertEqual(skip_args[2], "load scores")
            self.assertGreaterEqual(skip_args[3], 0)

            # Invoke the fake timer callback while the worker is blocked. The
            # callback must not need the finalizer lock to write its trace.
            _ManualTimer.instances[0].fire()
            log_path = Path(temp_dir) / "logs" / "finalization-stalls.log"
            content = log_path.read_text(encoding="utf-8")
            self.assertIn(f"day={DAY}", content)
            self.assertIn("phase=load scores", content)
            dump.assert_called_once()
            self.assertIs(dump.call_args.kwargs["all_threads"], True)

            store.release_load.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(_ManualTimer.instances[0].cancelled)
            self.assertTrue(_ManualTimer.instances[0].joined)
            self.assertTrue(finalization._finalize_lock.acquire(blocking=False))
            finalization._finalize_lock.release()

    def test_nested_batch_and_day_finalization_use_one_watchdog(self):
        store = _Store()
        with ExitStack() as stack:
            self._patch_timer(stack)
            _patched_dependencies(stack)
            finalization.finalize_due_days("C1", store, _Client(), post=False)

        self.assertEqual(len(_ManualTimer.instances), 1)
        self.assertTrue(_ManualTimer.instances[0].cancelled)
        self.assertTrue(_ManualTimer.instances[0].joined)
        self.assertEqual(store.rebuilds, 1)

    def test_exception_cancels_watchdog_and_releases_lock(self):
        store = _Store()
        with ExitStack() as stack:
            self._patch_timer(stack)
            dump = Mock()
            stack.enter_context(patch.object(diagnostics.faulthandler, "dump_traceback", dump))
            _patched_dependencies(stack)
            stack.enter_context(patch.object(finalization, "format_summary", side_effect=RuntimeError("format failed")))
            with self.assertRaisesRegex(RuntimeError, "format failed"):
                finalization.finalize_day(DAY, "C1", store, _Client(), post=False)

        self.assertEqual(len(_ManualTimer.instances), 1)
        self.assertTrue(_ManualTimer.instances[0].cancelled)
        self.assertTrue(_ManualTimer.instances[0].joined)
        _ManualTimer.instances[0].fire()
        dump.assert_not_called()
        self.assertTrue(finalization._finalize_lock.acquire(blocking=False))
        finalization._finalize_lock.release()

    def test_timeout_reads_environment_when_tracker_is_constructed(self):
        with patch.dict(diagnostics.os.environ, {"FINALIZATION_STALL_TIMEOUT_S": "0.5"}):
            tracker = diagnostics.FinalizationTracker(timer_factory=_ManualTimer)
        self.assertEqual(tracker.timeout_s, 0.5)
    def test_stall_timeout_must_be_finite_and_positive(self):
        for value in ("0", "-1", "nan", "inf", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                diagnostics.validate_stall_timeout(value)
        self.assertEqual(diagnostics.validate_stall_timeout("0.25"), 0.25)

    def test_runtime_info_uses_bounded_git_probe_and_omits_arguments(self):
        logger = Mock()
        run = Mock(return_value=SimpleNamespace(stdout="a" * 40 + "\n"))
        with patch.object(diagnostics.subprocess, "run", run), \
             patch.object(diagnostics.importlib.metadata, "version", return_value="1.2.3"), \
             patch.object(diagnostics.sys, "executable", "python-test"), \
             patch.object(diagnostics.platform, "python_version", return_value="3.13.1"):
            diagnostics.log_runtime_info(logger, Path("project-root"))

        self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "HEAD"])
        self.assertEqual(run.call_args.kwargs["cwd"], "project-root")
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        logged = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("python-test", logged)
        self.assertIn("3.13.1", logged)
        self.assertIn("slack-bolt=1.2.3", logged)
        self.assertIn("git_head=" + "a" * 40, logged)
        self.assertNotIn("argv", logged.lower())


if __name__ == "__main__":
    unittest.main()



