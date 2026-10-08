"""Mocked startup and literal-write tests for SheetStore."""

import threading
import unittest
from unittest.mock import patch

import gspread

from parser import ParsedScore
from sheet_store import SheetStore


class MemoryWorksheet:
    def __init__(self, title, rows=None, row_count=1000, col_count=26):
        self.title = title
        self._rows = [list(row) for row in (rows or [])]
        self.row_count = row_count
        self.col_count = col_count
        self.append_options = []
        self.operations = []

    def row_values(self, row):
        return list(self._rows[row - 1]) if len(self._rows) >= row else []

    def get_all_values(self):
        return [list(row) for row in self._rows]

    def update(self, values=None, range_name=None, **kwargs):
        self.operations.append(("update", range_name))
        if range_name == "A1":
            if self._rows:
                self._rows[0] = list(values[0])
            else:
                self._rows.append(list(values[0]))
            return
        raise AssertionError(f"unexpected update range: {range_name}")

    def add_cols(self, count):
        self.operations.append(("add_cols", count))
        self.col_count += count

    def append_rows(self, rows, value_input_option=None):
        self.append_options.append(value_input_option)
        self._rows.extend([list(row) for row in rows])

    def append_row(self, row, value_input_option=None):
        self.append_options.append(value_input_option)
        self._rows.append(list(row))


class MemorySpreadsheet:
    def __init__(self, worksheets=None):
        self.worksheets = dict(worksheets or {})
        self.created = []

    def worksheet(self, title):
        if title not in self.worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self.worksheets[title]

    def add_worksheet(self, title, rows, cols):
        ws = MemoryWorksheet(title, row_count=rows, col_count=cols)
        self.worksheets[title] = ws
        self.created.append((title, rows, cols))
        return ws


class MemoryClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, spreadsheet_id):
        return self.spreadsheet


class SheetStoreSetupTests(unittest.TestCase):
    def construct_store(self, spreadsheet):
        with patch("sheet_store.Credentials.from_service_account_file", return_value=object()) as credentials:
            with patch("sheet_store.gspread.authorize", return_value=MemoryClient(spreadsheet)):
                store = SheetStore("fake-spreadsheet-id", "fake-service-account.json")
        return store, credentials

    def test_blank_spreadsheet_bootstraps_all_seven_sheets_with_schema_width(self):
        spreadsheet = MemorySpreadsheet()

        store, credentials = self.construct_store(spreadsheet)

        expected = {
            "Scores": (1000, SheetStore.REQUIRED_SCORES_COLS),
            "DailyResults": (1000, SheetStore.REQUIRED_DAILY_COLS),
            "Totals": (1000, SheetStore.REQUIRED_TOTALS_COLS),
            "Events": (1000, SheetStore.REQUIRED_EVENTS_COLS),
            "MonthlyResults": (100, SheetStore.REQUIRED_MONTHLY_COLS),
            "GameRegistry": (20, SheetStore.REQUIRED_REGISTRY_COLS),
            "NLQueries": (200, SheetStore.REQUIRED_NLQUERIES_COLS),
        }
        self.assertEqual(
            spreadsheet.created,
            [(name, rows, len(cols)) for name, (rows, cols) in expected.items()],
        )
        for name, (_rows, columns) in expected.items():
            self.assertEqual(spreadsheet.worksheets[name].row_values(1), columns)
        self.assertEqual(store.scores, spreadsheet.worksheets["Scores"])
        self.assertEqual(store.nl_queries, spreadsheet.worksheets["NLQueries"])
        self.assertEqual(credentials.call_args.kwargs["scopes"], [
            "https://www.googleapis.com/auth/spreadsheets",
        ])

    def test_existing_worksheets_and_data_are_preserved(self):
        schemas = {
            "Scores": SheetStore.REQUIRED_SCORES_COLS,
            "DailyResults": SheetStore.REQUIRED_DAILY_COLS,
            "Totals": SheetStore.REQUIRED_TOTALS_COLS,
            "Events": SheetStore.REQUIRED_EVENTS_COLS,
            "MonthlyResults": SheetStore.REQUIRED_MONTHLY_COLS,
            "GameRegistry": SheetStore.REQUIRED_REGISTRY_COLS,
            "NLQueries": SheetStore.REQUIRED_NLQUERIES_COLS,
        }
        originals = {
            name: MemoryWorksheet(name, [list(columns), [f"kept-{name}"]])
            for name, columns in schemas.items()
        }
        before = {name: ws.get_all_values() for name, ws in originals.items()}
        spreadsheet = MemorySpreadsheet(originals)

        store, _ = self.construct_store(spreadsheet)

        self.assertEqual(spreadsheet.created, [])
        for name, rows in before.items():
            self.assertIs(spreadsheet.worksheets[name], originals[name])
            self.assertEqual(spreadsheet.worksheets[name].get_all_values(), rows)
        self.assertIs(store.registry, originals["GameRegistry"])

    def test_non_missing_worksheet_errors_propagate_without_creation(self):
        class BrokenSpreadsheet(MemorySpreadsheet):
            def worksheet(self, title):
                if title == "Scores":
                    raise PermissionError("spreadsheet access denied")
                return super().worksheet(title)

        spreadsheet = BrokenSpreadsheet()
        with patch("sheet_store.Credentials.from_service_account_file", return_value=object()):
            with patch("sheet_store.gspread.authorize", return_value=MemoryClient(spreadsheet)):
                with self.assertRaisesRegex(PermissionError, "access denied"):
                    SheetStore("fake-spreadsheet-id", "fake-service-account.json")
        self.assertEqual(spreadsheet.created, [])

    def test_empty_header_widens_narrow_grid_before_writing(self):
        class NarrowSheet(MemoryWorksheet):
            def update(self, values=None, range_name=None, **kwargs):
                if self.col_count < len(values[0]):
                    raise AssertionError("header write attempted before grid widening")
                super().update(values=values, range_name=range_name, **kwargs)

        store = SheetStore.__new__(SheetStore)
        store._write_lock = threading.RLock()
        store._sheets_write_max_retries = 1
        store._sheets_write_base_delay_s = 0
        store._sheets_write_max_delay_s = 0
        sheet = NarrowSheet("Totals", col_count=2)

        store._ensure_cols(sheet, SheetStore.REQUIRED_TOTALS_COLS)

        self.assertEqual(sheet.operations, [("add_cols", 5), ("update", "A1")])
        self.assertEqual(sheet.row_values(1), SheetStore.REQUIRED_TOTALS_COLS)

    def test_formula_like_game_names_and_slack_text_are_written_as_raw_values(self):
        spreadsheet = MemorySpreadsheet()
        store, _ = self.construct_store(spreadsheet)
        formula = '=HYPERLINK("https://example.com","click")'

        store.add_game_to_registry(formula, "time", "U123", "2026-10-08")
        store.upsert_score(
            "2026-10-08", "U123", ParsedScore("Crossclimb", 1, "time", 123, "2:03"),
            "=1+1", formula,
        )
        store.log_nl_query(
            slack_ts="=1+2", asker_user_id="U123", question=formula,
            prefix="", intent="", game="", target_user="", stat="", preset="",
            resolved_start="", resolved_end="", response_prefix="",
        )

        registry = spreadsheet.worksheets["GameRegistry"]
        scores = spreadsheet.worksheets["Scores"]
        queries = spreadsheet.worksheets["NLQueries"]
        self.assertEqual(registry.append_options, ["RAW", "RAW"])
        self.assertEqual(registry.get_all_values()[-1][0], formula)
        self.assertEqual(scores.append_options, ["RAW"])
        self.assertEqual(scores.get_all_values()[-1][7:9], ["=1+1", formula])
        self.assertEqual(queries.append_options, ["RAW"])
        self.assertEqual(queries.get_all_values()[-1][1:4], ["=1+2", "U123", formula])


if __name__ == "__main__":
    unittest.main()
