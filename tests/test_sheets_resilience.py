"""Offline retry tests for the Google Sheets wrappers."""

import json
import threading
import unittest
from unittest.mock import patch

from gspread.exceptions import APIError
from requests import Response
from requests.exceptions import Timeout as RequestsTimeout

from game_registry import _DEFAULT_GAMES
from gsheets_safe import gspread_call, is_retriable_sheets_error
from sheet_store import SheetStore


def api_error(status, body=None, headers=None):
    response = Response()
    response.status_code = status
    if body is None:
        body = json.dumps({"error": {"code": status, "message": "offline test"}})
    response._content = body.encode("utf-8")
    response.headers.update(headers or {})
    return APIError(response)


def make_store(*, read_retries=1, write_attempts=2, max_delay=0):
    store = SheetStore.__new__(SheetStore)
    store._sheets_429_max_retries = read_retries
    store._sheets_429_base_delay_s = 0
    store._sheets_429_max_delay_s = max_delay
    store._sheets_write_max_retries = write_attempts
    store._sheets_write_base_delay_s = 0
    store._sheets_write_max_delay_s = max_delay
    store._write_lock = threading.RLock()
    return store


class SheetsResilienceTests(unittest.TestCase):
    def test_gspread_call_retries_http_500_then_succeeds(self):
        calls = 0

        def flaky_call():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise api_error(500)
            return "ok"

        with patch("gsheets_safe.time.sleep"):
            self.assertEqual(gspread_call(flaky_call), "ok")
        self.assertEqual(calls, 2)

    def test_read_retries_html_502_using_response_status_when_gspread_code_is_minus_one(self):
        error = api_error(502, "<html>gateway body that must not be logged</html>")
        self.assertEqual(error.code, -1)
        self.assertFalse(error.response)
        self.assertTrue(is_retriable_sheets_error(error))

        calls = 0

        def flaky_read():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error
            return [["day"], ["2026-10-10"]]

        with patch("sheet_store.time.sleep"), self.assertLogs("sheet_store", level="WARNING") as logs:
            rows = make_store()._read_with_backoff(flaky_read)

        self.assertEqual(rows[1][0], "2026-10-10")
        self.assertEqual(calls, 2)
        self.assertIn("status=502", "\n".join(logs.output))
        self.assertNotIn("gateway body", "\n".join(logs.output))

    def test_network_timeouts_retry_for_reads_and_writes(self):
        store = make_store()
        read_calls = 0

        def flaky_read():
            nonlocal read_calls
            read_calls += 1
            if read_calls == 1:
                raise RequestsTimeout("read timed out")
            return ["read succeeded"]

        write_calls = 0

        def flaky_write():
            nonlocal write_calls
            write_calls += 1
            if write_calls == 1:
                raise RequestsTimeout("write timed out")
            return "write succeeded"

        with patch("sheet_store.time.sleep"):
            self.assertEqual(store._read_with_backoff(flaky_read), ["read succeeded"])
            self.assertEqual(store._write_with_backoff("test.write", flaky_write), "write succeeded")
        self.assertEqual(read_calls, 2)
        self.assertEqual(write_calls, 2)

    def test_read_inside_write_does_not_multiply_retry_budgets(self):
        store = make_store(read_retries=6, write_attempts=2)
        read_calls = 0

        def failing_read():
            nonlocal read_calls
            read_calls += 1
            raise api_error(500)

        with patch("sheet_store.time.sleep"):
            with self.assertRaises(APIError):
                store._write_with_backoff(
                    "test.write_with_read",
                    lambda: store._read_with_backoff(failing_read),
                )

        self.assertEqual(read_calls, 2)

    def test_blank_header_mark_day_does_not_multiply_schema_write_attempts(self):
        class BlankDailyWorksheet:
            def __init__(self):
                self.title = "DailyResults"
                self.col_count = 1
                self.row_values_calls = 0
                self.add_cols_calls = 0

            def row_values(self, _row):
                self.row_values_calls += 1
                return []

            def add_cols(self, _count):
                self.add_cols_calls += 1
                if self.add_cols_calls <= 2:
                    raise api_error(503)
                self.col_count += _count

        store = make_store(read_retries=6, write_attempts=2)
        store.daily = BlankDailyWorksheet()

        with patch("sheet_store.time.sleep"):
            with self.assertRaises(APIError):
                store._write_with_backoff(
                    "DailyResults.mark_day_posted",
                    lambda: store._mark_day_posted_once("2026-10-10", {"ready": True}),
                )

        self.assertEqual(store.daily.row_values_calls, 2)
        self.assertEqual(store.daily.add_cols_calls, 2)

    def test_totals_fixed_range_retry_does_not_duplicate_applied_rows(self):
        class DailyWorksheet:
            def get_all_values(self):
                return [
                    ["day", "posted_at", "summary_json"],
                    ["2026-10-10", "", json.dumps({"awards_by_user": {"U1": 1, "U2": 2}})],
                ]

        class LostResponseTotalsWorksheet:
            def __init__(self):
                self.rows = []
                self.row_count = 1
                self.update_a2_calls = 0

            def get_all_values(self):
                return [list(row) for row in self.rows]

            def row_values(self, row):
                return list(self.rows[row - 1]) if len(self.rows) >= row else []

            def update(self, values=None, range_name=None, raw=True, **_kwargs):
                if range_name == "A1":
                    self.rows = [list(values[0])]
                    return
                if range_name != "A2":
                    raise AssertionError(f"unexpected totals update range: {range_name}")
                self.update_a2_calls += 1
                self.rows = self.rows[:1] + [list(row) for row in values]
                if self.update_a2_calls == 1:
                    raise RequestsTimeout("response lost after update was applied")

            def batch_clear(self, _ranges):
                self.rows = self.rows[:1]

            def resize(self, rows):
                self.row_count = rows

        store = make_store(write_attempts=2)
        store.daily = DailyWorksheet()
        totals = LostResponseTotalsWorksheet()
        store.totals = totals

        with patch("sheet_store.time.sleep"):
            store.rebuild_totals_from_daily()

        output = totals.get_all_values()
        self.assertEqual(totals.update_a2_calls, 2)
        self.assertEqual([row[0] for row in output[1:]], ["U1", "U2"])
        self.assertEqual(len(output), 3)

    def test_registry_seed_retry_rechecks_before_appending(self):
        class LostResponseRegistryWorksheet:
            def __init__(self):
                self.rows = [list(SheetStore.REQUIRED_REGISTRY_COLS)]
                self.append_calls = 0

            def get_all_values(self):
                return [list(row) for row in self.rows]

            def append_rows(self, rows, value_input_option=None):
                self.append_calls += 1
                self.rows.extend([list(row) for row in rows])
                if self.append_calls == 1:
                    raise RequestsTimeout("response lost after append was applied")

        store = make_store(write_attempts=2)
        registry = LostResponseRegistryWorksheet()
        store.registry = registry

        with patch("sheet_store.time.sleep"):
            store._seed_registry_if_empty()

        self.assertEqual(registry.append_calls, 1)
        self.assertEqual(len(registry.rows), 1 + len(_DEFAULT_GAMES))

    def test_registry_add_retry_rechecks_name_before_appending(self):
        class LostResponseRegistryWorksheet:
            def __init__(self):
                self.rows = [list(SheetStore.REQUIRED_REGISTRY_COLS)]
                self.append_calls = 0

            def get_all_values(self):
                return [list(row) for row in self.rows]

            def append_rows(self, rows, value_input_option=None):
                self.append_calls += 1
                self.rows.extend([list(row) for row in rows])
                if self.append_calls == 1:
                    raise RequestsTimeout("response lost after append was applied")

        store = make_store(write_attempts=2)
        registry = LostResponseRegistryWorksheet()
        store.registry = registry

        with patch("sheet_store.time.sleep"):
            store.add_game_to_registry("Custom Game", "time", "U1", "2026-10-10")

        self.assertEqual(registry.append_calls, 1)
        custom_rows = [row for row in registry.rows[1:] if row[0] == "Custom Game"]
        self.assertEqual(len(custom_rows), 1)

    def test_gspread_call_exhaustion_stops_at_its_bounded_attempt_count(self):
        calls = 0

        def always_unavailable():
            nonlocal calls
            calls += 1
            raise api_error(503)

        with patch("gsheets_safe.time.sleep"):
            with self.assertRaises(APIError):
                gspread_call(always_unavailable)
        self.assertEqual(calls, 5)

    def test_http_400_401_and_403_fail_fast(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                error = api_error(status)
                self.assertFalse(is_retriable_sheets_error(error))
                with patch("sheet_store.time.sleep"):
                    for runner in (
                        lambda store, fn: gspread_call(fn),
                        lambda store, fn: store._read_with_backoff(fn),
                        lambda store, fn: store._write_with_backoff("test.denied", fn),
                    ):
                        calls = 0

                        def denied_call():
                            nonlocal calls
                            calls += 1
                            raise error

                        with self.assertRaises(APIError):
                            runner(make_store(read_retries=4, write_attempts=4), denied_call)
                        self.assertEqual(calls, 1)

    def test_posted_days_empty_result_is_cached_for_its_ttl(self):
        class EmptyDailyWorksheet:
            def __init__(self):
                self.reads = 0

            def get_values(self, _range):
                self.reads += 1
                return []

        store = SheetStore.__new__(SheetStore)
        store._posted_days_cache = set()
        store._posted_days_cache_ts = 0
        store._posted_days_cache_loaded = True
        store._posted_days_cache_ttl_s = 60
        store._daily_header_cache = list(SheetStore.REQUIRED_DAILY_COLS)
        store._daily_header_cache_ts = 100
        store._daily_header_cache_ttl_s = 300
        store._daily_cache_lock = threading.Lock()
        store.daily = EmptyDailyWorksheet()

        with patch("sheet_store.time.time", side_effect=[100, 100, 101]):
            store._refresh_posted_days_cache()
            store._refresh_posted_days_cache()

        self.assertEqual(store.daily.reads, 1)


if __name__ == "__main__":
    unittest.main()
