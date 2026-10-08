import json
import threading
import unittest
from unittest.mock import patch

import gspread

from gsheets_safe import JsonCellTooLargeError, serialize_json_for_cell
from sheet_store import SheetStore


class _Sheet:
    def __init__(self, rows, title):
        self.rows = [list(row) for row in rows]
        self.title = title
        self.calls = []

    @property
    def write_calls(self):
        return [call for call in self.calls if call[0] in {"append_row", "append_rows", "update"}]

    def get_all_values(self):
        self.calls.append(("get_all_values",))
        return [list(row) for row in self.rows]

    def row_values(self, number):
        self.calls.append(("row_values", number))
        return list(self.rows[number - 1]) if len(self.rows) >= number else []

    def append_row(self, row, **kwargs):
        self.calls.append(("append_row", list(row)))
        self.rows.append(list(row))

    def append_rows(self, rows, **kwargs):
        self.calls.append(("append_rows", [list(row) for row in rows]))
        self.rows.extend(list(row) for row in rows)

    def update(self, values=None, range_name=None, **kwargs):
        self.calls.append(("update", range_name, values))
        row_i, col_i = gspread.utils.a1_to_rowcol(range_name)
        while len(self.rows) < row_i:
            self.rows.append([])
        values = values or []
        if col_i == 1 and row_i == 1:
            self.rows[0] = list(values[0]) if values else []
            return
        row = self.rows[row_i - 1]
        value = values[0][0] if values and values[0] else ""
        while len(row) < col_i:
            row.append("")
        row[col_i - 1] = value


def _store(*, daily_rows=None, monthly_rows=None, event_rows=None):
    store = SheetStore.__new__(SheetStore)
    store.daily = _Sheet(
        daily_rows if daily_rows is not None else [list(SheetStore.REQUIRED_DAILY_COLS)],
        "DailyResults",
    )
    store.monthly = _Sheet(
        monthly_rows if monthly_rows is not None else [list(SheetStore.REQUIRED_MONTHLY_COLS)],
        "MonthlyResults",
    )
    store.events = _Sheet(
        event_rows if event_rows is not None else [list(SheetStore.REQUIRED_EVENTS_COLS)],
        "Events",
    )
    store._write_lock = threading.RLock()
    store._sheets_write_max_retries = 1
    store._sheets_write_base_delay_s = 0
    store._sheets_write_max_delay_s = 0
    store._daily_cache_lock = threading.RLock()
    store._posted_days_cache = set()
    store._posted_days_cache_ts = 0.0
    store._daily_header_cache = []
    store._daily_header_cache_ts = 0.0
    store._daily_header_cache_ttl_s = 300
    store._posted_days_cache_ttl_s = 60
    store._event_cache = set()
    store._event_cache_lock = threading.Lock()
    return store


class LedgerJsonSafetyTests(unittest.TestCase):
    def test_json_cell_serialization_round_trips_compact_unicode_and_budgets_utf16(self):
        value = {"winner": "🏆", "points": 5}
        serialized = serialize_json_for_cell(value)
        self.assertEqual(json.loads(serialized), value)
        self.assertNotIn(": ", serialized)

        units = len(serialized.encode("utf-16-le")) // 2
        self.assertEqual(serialize_json_for_cell(value, limit=units), serialized)
        with self.assertRaises(JsonCellTooLargeError):
            serialize_json_for_cell(value, limit=units - 1)

    def test_json_cell_serializer_rejects_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            serialize_json_for_cell({"points": float("nan")})

    def test_bulk_events_spools_complete_oversized_payload_and_writes_valid_rows(self):
        store = _store()
        large_payload = {"text": "🏆" * 26000}
        valid_payload = {"type": "message", "text": "score"}

        with patch("sheet_store.spool_jsonl") as spool:
            store.bulk_log_events([
                ("E-large", large_payload),
                ("E-valid-1", valid_payload),
                ("E-valid-2", {"type": "message", "text": "another score"}),
            ])

        appended = [row for row in store.events.rows[1:]]
        self.assertEqual([row[0] for row in appended], ["E-valid-1", "E-valid-2"])
        self.assertEqual(json.loads(appended[0][2]), valid_payload)
        spool.assert_called_once()
        self.assertEqual(spool.call_args.args[1]["payload"], large_payload)
        self.assertIn("E-large", store._event_cache)

    def test_single_event_oversize_spools_without_writing_a_sheet_row(self):
        store = _store()
        payload = {"text": "x" * 50000}

        with patch("sheet_store.spool_jsonl") as spool:
            store.log_event("E-large", payload)

        self.assertEqual(store.events.write_calls, [])
        spool.assert_called_once()
        self.assertEqual(spool.call_args.args[1]["payload"], payload)

    def test_non_finite_event_is_rejected_without_sheet_write_or_spool(self):
        store = _store()
        with patch("sheet_store.spool_jsonl") as spool:
            with self.assertRaises(ValueError):
                store.log_event("E-nan", {"score": float("nan")})
        self.assertEqual(store.events.write_calls, [])
        spool.assert_not_called()

    def test_daily_and_monthly_claims_return_whether_the_row_was_appended(self):
        store = _store()

        self.assertTrue(store.mark_day_posted("2026-10-08", {"winners": {}}))
        self.assertFalse(store.mark_day_posted("2026-10-08", {"winners": {}}))
        self.assertTrue(store.mark_month_posted("2026-10", {"champion": "U1"}))
        self.assertFalse(store.mark_month_posted("2026-10", {"champion": "U1"}))

    def test_oversized_daily_and_monthly_json_never_writes(self):
        oversized = {"details": "x" * 50000}
        operations = (
            ("mark_day_posted", "daily"),
            ("replace_day_summary", "daily"),
            ("update_day_summary", "daily"),
            ("mark_month_posted", "monthly"),
            ("replace_month_summary", "monthly"),
            ("update_month_summary", "monthly"),
        )
        for method, sheet_name in operations:
            with self.subTest(method=method):
                daily_rows = [
                    list(SheetStore.REQUIRED_DAILY_COLS),
                    ["2026-10-08", "posted", "{}"],
                ]
                monthly_rows = [
                    list(SheetStore.REQUIRED_MONTHLY_COLS),
                    ["2026-10", "posted", "{}"],
                ]
                store = _store(daily_rows=daily_rows, monthly_rows=monthly_rows)
                with self.assertRaises(JsonCellTooLargeError):
                    if method == "mark_day_posted":
                        store.mark_day_posted("2026-10-09", oversized)
                    elif method == "replace_day_summary":
                        store.replace_day_summary("2026-10-08", oversized)
                    elif method == "update_day_summary":
                        store.update_day_summary("2026-10-08", oversized)
                    elif method == "mark_month_posted":
                        store.mark_month_posted("2026-11", oversized)
                    elif method == "replace_month_summary":
                        store.replace_month_summary("2026-10", oversized)
                    else:
                        store.update_month_summary("2026-10", oversized)
                sheet = getattr(store, sheet_name)
                self.assertEqual(sheet.write_calls, [])

    def test_corrupt_or_non_object_daily_and_monthly_patches_leave_original_untouched(self):
        for invalid in ("{broken", "[]"):
            with self.subTest(invalid=invalid):
                daily_rows = [
                    list(SheetStore.REQUIRED_DAILY_COLS),
                    ["2026-10-08", "posted", invalid],
                ]
                monthly_rows = [
                    list(SheetStore.REQUIRED_MONTHLY_COLS),
                    ["2026-10", "posted", invalid],
                ]
                store = _store(daily_rows=daily_rows, monthly_rows=monthly_rows)
                before_daily = [list(row) for row in store.daily.rows]
                before_monthly = [list(row) for row in store.monthly.rows]

                self.assertFalse(store.update_day_summary("2026-10-08", {"slack_ts": "1"}))
                self.assertFalse(store.update_month_summary("2026-10", {"slack_ts": "2"}))

                self.assertEqual(store.daily.rows, before_daily)
                self.assertEqual(store.monthly.rows, before_monthly)
                self.assertEqual(store.daily.write_calls, [])
                self.assertEqual(store.monthly.write_calls, [])


if __name__ == "__main__":
    unittest.main()
