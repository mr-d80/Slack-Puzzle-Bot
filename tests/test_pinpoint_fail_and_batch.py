"""Tests for two fixes:

1. "pinpoint fail" concessions — a player without a shareable score card can
   register a worst-case Pinpoint DNF by posting "pinpoint fail".
2. Batched reconcile writes — sync/replay accumulate upserts and flush them
   with a fixed, tiny number of Sheets writes (was one write per candidate,
   which tripped the Sheets write-per-minute quota).
"""

import sys
import threading
import types
import unittest

import gspread

from parser import (
    ParsedScore,
    parse_pinpoint_fail,
    make_pinpoint_fail_score,
    PINPOINT_DNF_PENALTY,
    PINPOINT_MAX_GUESSES,
)
import day_utils
from sheet_store import SheetStore


# ---------------------------------------------------------------------------
# 1. Pinpoint-fail parsing (pure)
# ---------------------------------------------------------------------------
class PinpointFailParseTests(unittest.TestCase):
    def test_matches_bare_concession_variants(self):
        for text in [
            "pinpoint fail",
            "Pinpoint failed",
            "PINPOINT FAIL",
            "pinpoint - fail",
            "pinpoint: fail",
            "  pinpoint   fail  ",
            "*pinpoint fail*",
            "pinpoint fail.",
            "pinpoint fails",
        ]:
            with self.subTest(text=text):
                result = parse_pinpoint_fail(text)
                self.assertIsNotNone(result, f"should match: {text!r}")
                self.assertIsNone(result.puzzle_id)

    def test_extracts_explicit_puzzle_id(self):
        self.assertEqual(parse_pinpoint_fail("pinpoint #42 fail").puzzle_id, 42)
        self.assertEqual(parse_pinpoint_fail("Pinpoint #7 failed").puzzle_id, 7)
        self.assertEqual(parse_pinpoint_fail("pinpoint#100 fail").puzzle_id, 100)

    def test_rejects_non_concessions(self):
        for text in [
            "I had an epic fail today",
            "pinpoint was great",
            "failing to pinpoint the issue",
            "wordle fail",
            "pinpoint #42 | 5",
            "the pinpoint fail was brutal",
            "",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(parse_pinpoint_fail(text))

    def test_builder_produces_worst_case_dnf(self):
        score = make_pinpoint_fail_score(42)
        self.assertEqual(score.game, "Pinpoint")
        self.assertEqual(score.puzzle_id, 42)
        self.assertEqual(score.metric_type, "guesses")
        self.assertEqual(score.metric_value, PINPOINT_MAX_GUESSES + PINPOINT_DNF_PENALTY)
        self.assertEqual(score.metric_value, 105)
        self.assertEqual(score.display, f"DNF({PINPOINT_MAX_GUESSES})")


# ---------------------------------------------------------------------------
# 2. resolve_pinpoint_fail_score (store-aware puzzle-id resolution)
# ---------------------------------------------------------------------------
class _ResolverStore:
    """Minimal store: returns configured score rows per day for load_scores_for_day."""

    def __init__(self, by_day):
        self._by_day = by_day

    def load_scores_for_day(self, day):
        return list(self._by_day.get(day, []))


def _rec(user_id, game, puzzle_id):
    return {"user_id": user_id, "game": game, "puzzle_id": str(puzzle_id)}


class ResolvePinpointFailTests(unittest.TestCase):
    DAY = "2026-07-15"
    PREV = "2026-07-14"

    def test_uses_todays_primary_puzzle(self):
        store = _ResolverStore({self.DAY: [_rec("U1", "Pinpoint", 100)]})
        score = day_utils.resolve_pinpoint_fail_score("pinpoint fail", self.DAY, store_obj=store)
        self.assertIsNotNone(score)
        self.assertEqual(score.puzzle_id, 100)
        self.assertEqual(score.metric_value, 105)

    def test_falls_back_to_prev_day_plus_one(self):
        store = _ResolverStore({self.PREV: [_rec("U1", "Pinpoint", 99)]})
        score = day_utils.resolve_pinpoint_fail_score("pinpoint fail", self.DAY, store_obj=store)
        self.assertIsNotNone(score)
        self.assertEqual(score.puzzle_id, 100)

    def test_explicit_puzzle_id_wins_over_resolution(self):
        store = _ResolverStore({self.DAY: [_rec("U1", "Pinpoint", 100)]})
        score = day_utils.resolve_pinpoint_fail_score("pinpoint #77 fail", self.DAY, store_obj=store)
        self.assertIsNotNone(score)
        self.assertEqual(score.puzzle_id, 77)

    def test_returns_none_when_puzzle_unresolvable(self):
        store = _ResolverStore({})  # no Pinpoint activity today or yesterday
        self.assertIsNone(
            day_utils.resolve_pinpoint_fail_score("pinpoint fail", self.DAY, store_obj=store)
        )

    def test_returns_none_for_non_concession(self):
        store = _ResolverStore({self.DAY: [_rec("U1", "Pinpoint", 100)]})
        self.assertIsNone(
            day_utils.resolve_pinpoint_fail_score("hello there", self.DAY, store_obj=store)
        )


# ---------------------------------------------------------------------------
# 3. bulk_upsert_scores / bulk_log_events batching
# ---------------------------------------------------------------------------
_HEADER = list(SheetStore.REQUIRED_SCORES_COLS)


class _FakeScores:
    """Records write calls so tests can assert how many Sheets writes happened."""

    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.calls = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, values=None, range_name=None, **kw):
        self.calls.append(("update", range_name))

    def append_row(self, row, **kw):
        self.calls.append(("append_row",))
        self.rows.append(list(row))

    def append_rows(self, rows, **kw):
        self.calls.append(("append_rows", len(rows)))
        self.rows.extend([list(r) for r in rows])

    def batch_update(self, data, **kw):
        self.calls.append(("batch_update", len(data)))
        for d in data:
            a1 = d["range"].split(":")[0]
            r, _c = gspread.utils.a1_to_rowcol(a1)
            while len(self.rows) < r:
                self.rows.append([""] * len(_HEADER))
            self.rows[r - 1] = list(d["values"][0])


class _FakeEvents:
    def __init__(self):
        self.appended = []
        self.calls = []

    def append_rows(self, rows, **kw):
        self.calls.append(("append_rows", len(rows)))
        self.appended.extend(rows)

    def append_row(self, row, **kw):
        self.calls.append(("append_row",))
        self.appended.append(row)


def _bare_store():
    """A SheetStore instance without running __init__ (no Google auth)."""
    st = SheetStore.__new__(SheetStore)
    st._write_lock = threading.RLock()
    st._sheets_write_max_retries = 3
    st._sheets_write_base_delay_s = 0.001
    st._sheets_write_max_delay_s = 0.002
    return st


def _P(game, pid, mv, disp):
    return ParsedScore(game=game, puzzle_id=pid, metric_type="guesses", metric_value=mv, display=disp)


class BulkUpsertTests(unittest.TestCase):
    def _op_counts(self, calls):
        ops = [c[0] for c in calls]
        return {op: ops.count(op) for op in set(ops)}

    def test_collapses_to_batch_update_plus_append_rows(self):
        existing = [
            _HEADER,
            ["2026-07-15", "U1", "Pinpoint", "100", "guesses", "3", "3", "ts1", "r1", "", ""],
        ]
        st = _bare_store()
        st.scores = _FakeScores(existing)

        upserts = [
            ("2026-07-15", "U1", _P("Pinpoint", 100, 105, "DNF(5)"), "ts1b", "pinpoint fail"),
            ("2026-07-15", "U2", _P("Pinpoint", 100, 2, "2"), "ts2", "Pinpoint #100 | 2"),
            ("2026-07-15", "U3", _P("Pinpoint", 100, 4, "4"), "ts3", "x"),
        ]
        applied = st.bulk_upsert_scores(upserts)

        self.assertEqual(applied, 3)
        counts = self._op_counts(st.scores.calls)
        # Exactly one batched edit (U1) and one batched append (U2, U3).
        self.assertEqual(counts.get("batch_update"), 1)
        self.assertEqual(counts.get("append_rows"), 1)
        self.assertNotIn("update", counts)
        self.assertNotIn("append_row", counts)

    def test_existing_row_is_updated_in_place(self):
        existing = [
            _HEADER,
            ["2026-07-15", "U1", "Pinpoint", "100", "guesses", "3", "3", "ts1", "r1", "", ""],
        ]
        st = _bare_store()
        st.scores = _FakeScores(existing)

        st.bulk_upsert_scores(
            [("2026-07-15", "U1", _P("Pinpoint", 100, 105, "DNF(5)"), "ts1b", "pinpoint fail")]
        )

        u1 = [r for r in st.scores.rows if len(r) > 1 and r[1] == "U1"]
        self.assertEqual(len(u1), 1)  # updated, not duplicated
        self.assertEqual(u1[0][5], "105")
        self.assertEqual(u1[0][6], "DNF(5)")

    def test_last_write_wins_within_batch(self):
        st = _bare_store()
        st.scores = _FakeScores([_HEADER])

        # Same identity twice in one batch: the later value must win.
        st.bulk_upsert_scores(
            [
                ("2026-07-15", "U3", _P("Pinpoint", 100, 4, "4"), "ts3", "x"),
                ("2026-07-15", "U3", _P("Pinpoint", 100, 105, "DNF(5)"), "ts3b", "pinpoint fail"),
            ]
        )

        u3 = [r for r in st.scores.rows if len(r) > 1 and r[1] == "U3"]
        self.assertEqual(len(u3), 1)
        self.assertEqual(u3[0][5], "105")

    def test_empty_upserts_writes_nothing(self):
        st = _bare_store()
        st.scores = _FakeScores([_HEADER])
        self.assertEqual(st.bulk_upsert_scores([]), 0)
        self.assertEqual(st.scores.calls, [])


class BulkLogEventsTests(unittest.TestCase):
    def test_appends_all_new_events_in_one_write(self):
        st = _bare_store()
        st.events = _FakeEvents()
        st._event_cache = set()
        st._event_cache_lock = threading.Lock()

        st.bulk_log_events([("E1", {"a": 1}), ("E2", {"b": 2})])

        self.assertEqual([c[0] for c in st.events.calls], ["append_rows"])
        self.assertEqual(len(st.events.appended), 2)
        self.assertIn("E1", st._event_cache)
        self.assertIn("E2", st._event_cache)

    def test_skips_already_cached_and_intra_batch_duplicates(self):
        st = _bare_store()
        st.events = _FakeEvents()
        st._event_cache = {"E1"}
        st._event_cache_lock = threading.Lock()

        st.bulk_log_events([("E1", {"a": 1}), ("E2", {"b": 2}), ("E2", {"b": 2})])

        # E1 already seen; E2 deduped within the batch -> one appended row.
        self.assertEqual(len(st.events.appended), 1)

    def test_empty_events_writes_nothing(self):
        st = _bare_store()
        st.events = _FakeEvents()
        st._event_cache = set()
        st._event_cache_lock = threading.Lock()

        st.bulk_log_events([])
        self.assertEqual(st.events.calls, [])


if __name__ == "__main__":
    unittest.main()
