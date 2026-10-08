"""sheet_store.py

Thread-safe, rate-limit-aware Google Sheets data layer.

Manages the worksheets: Scores, Events, DailyResults, MonthlyResults, Totals,
GameRegistry, NLQueries.
"""

import json
import os
import random
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from awards import AwardTally, unpack_awards
from config import TZ, DAY_FMT
from day_utils import _parse_day_key_loose
from game_registry import _DEFAULT_GAMES
from gsheets_safe import (
    JsonCellTooLargeError, gspread_call, utc_now_iso, truncate_for_cell,
    serialize_json_for_cell, spool_jsonl,
)
from parser import ParsedScore, normalize_game, canonical_user_id
from score_identity import score_identity


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_awards_from_summary(payload: Any) -> Optional[Dict[str, Any]]:
    """Extract the awards dict from a summary_json payload (handles legacy key names)."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("awards_by_user"), dict):
        return payload["awards_by_user"]
    if isinstance(payload.get("trophies_by_user"), dict):
        return payload["trophies_by_user"]
    return None


def _api_error_status(exc: Exception) -> Optional[int]:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None

    for attr in ("status_code", "status"):
        raw = getattr(resp, attr, None)
        if raw is None:
            continue
        try:
            return int(raw)
        except Exception:
            continue

    return None


def _api_error_retry_after(exc: Exception) -> Optional[float]:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None

    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None

    try:
        return float(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SheetStore
# ---------------------------------------------------------------------------
class SheetStore:
    REQUIRED_SCORES_COLS = [
        "day", "user_id", "game", "puzzle_id", "metric_type", "metric_value",
        "display", "slack_ts", "raw_text", "updated_at", "tiebreak_value", "status",
    ]
    REQUIRED_EVENTS_COLS = ["event_id", "received_at", "payload_json"]
    REQUIRED_DAILY_COLS = ["day", "posted_at", "summary_json"]
    REQUIRED_MONTHLY_COLS = ["month", "posted_at", "summary_json"]
    # trophies/ties are the legacy awards (days before the medal cut-over);
    # gold/silver/bronze/points are the medal era. Totals is all-time, so it
    # carries both families.
    REQUIRED_TOTALS_COLS = ["user_id", "trophies", "ties", "gold", "silver", "bronze", "points"]
    REQUIRED_REGISTRY_COLS = ["game_name", "metric_type", "effective_date", "added_by"]
    REQUIRED_NLQUERIES_COLS = [
        "received_at", "slack_ts", "asker_user_id", "question", "prefix",
        "intent", "game", "target_user", "stat",
        "preset", "resolved_start", "resolved_end", "response_prefix",
        "schema_version", "translator_source", "raw_spec_json", "validated_spec_json",
        "unsupported_reason", "rows_scanned",
    ]

    def __init__(self, spreadsheet_id: str, sa_file: str):
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(sa_file, scopes=scopes)
        self.gc = gspread.authorize(creds)
        self.ss = self.gc.open_by_key(spreadsheet_id)

        # --- Sheets read throttle protection (prevents 429 storms) ---
        self._posted_days_cache: Set[str] = set()
        self._posted_days_cache_ts: float = 0.0
        self._daily_header_cache: List[str] = []
        self._daily_header_cache_ts: float = 0.0
        self._daily_cache_lock = threading.Lock()

        # Tunables (env overrides)
        self._posted_days_cache_ttl_s: int = int(os.getenv("SHEETS_POSTED_DAYS_CACHE_TTL_S", "60"))
        self._daily_header_cache_ttl_s: int = int(os.getenv("SHEETS_DAILY_HEADER_CACHE_TTL_S", "300"))
        self._sheets_429_max_retries: int = int(os.getenv("SHEETS_429_MAX_RETRIES", "6"))
        self._sheets_429_base_delay_s: float = float(os.getenv("SHEETS_429_BASE_DELAY_S", "0.5"))
        self._sheets_429_max_delay_s: float = float(os.getenv("SHEETS_429_MAX_DELAY_S", "10.0"))

        self._write_lock = threading.RLock()
        self._sheets_write_max_retries: int = int(os.getenv("SHEETS_WRITE_MAX_RETRIES", "6"))
        self._sheets_write_base_delay_s: float = float(os.getenv("SHEETS_WRITE_BASE_DELAY_S", "1.0"))
        self._sheets_write_max_delay_s: float = float(os.getenv("SHEETS_WRITE_MAX_DELAY_S", "10.0"))

        # Create missing tabs for a fresh public spreadsheet, while preserving
        # any existing tabs and their data. The row counts leave room for normal
        # use; Sheets can expand them automatically as more rows are appended.
        self.scores = self._get_or_create_worksheet("Scores", self.REQUIRED_SCORES_COLS, rows=1000)
        self.daily = self._get_or_create_worksheet("DailyResults", self.REQUIRED_DAILY_COLS, rows=1000)
        self.totals = self._get_or_create_worksheet("Totals", self.REQUIRED_TOTALS_COLS, rows=1000)
        self.events = self._get_or_create_worksheet("Events", self.REQUIRED_EVENTS_COLS, rows=1000)
        self.monthly = self._get_or_create_worksheet("MonthlyResults", self.REQUIRED_MONTHLY_COLS, rows=100)
        self.registry = self._get_or_create_worksheet("GameRegistry", self.REQUIRED_REGISTRY_COLS, rows=20)
        self.nl_queries = self._get_or_create_worksheet("NLQueries", self.REQUIRED_NLQUERIES_COLS, rows=200)

        self._ensure_headers()
        self._seed_registry_if_empty()
        self._event_cache: Set[str] = set(self._load_recent_event_ids(500))
        self._event_cache_lock = threading.Lock()

    def _get_or_create_worksheet(self, title: str, required_cols: List[str], rows: int):
        """Return an existing worksheet, creating it only when it is missing."""
        try:
            return self.ss.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            return self.ss.add_worksheet(title=title, rows=rows, cols=len(required_cols))

    def _ensure_headers(self) -> None:
        self._ensure_cols(self.scores, self.REQUIRED_SCORES_COLS)
        self._ensure_cols(self.events, self.REQUIRED_EVENTS_COLS)
        self._ensure_cols(self.daily, self.REQUIRED_DAILY_COLS)
        self._ensure_cols(self.totals, self.REQUIRED_TOTALS_COLS)
        self._ensure_cols(self.registry, self.REQUIRED_REGISTRY_COLS)
        self._ensure_cols(self.nl_queries, self.REQUIRED_NLQUERIES_COLS)
        self._ensure_cols(self.monthly, self.REQUIRED_MONTHLY_COLS)

    # -- GameRegistry helpers ------------------------------------------------

    def _seed_registry_if_empty(self) -> None:
        """Populate the GameRegistry sheet with _DEFAULT_GAMES if it has no data rows."""
        rows = self.registry.get_all_values()
        if len(rows) > 1:
            return  # already has data
        seed_rows = [
            [name, mt, "2020-01-01", "system"]
            for name, mt in _DEFAULT_GAMES
        ]
        self._write_with_backoff(
            "GameRegistry.seed",
            lambda: self.registry.append_rows(seed_rows, value_input_option="RAW"),
        )

    def load_game_registry(self) -> List[Tuple[str, str, str]]:
        """Return list of (game_name, metric_type, effective_date) from the GameRegistry sheet."""
        rows = self.registry.get_all_values()
        if len(rows) <= 1:
            return [(name, mt, "2020-01-01") for name, mt in _DEFAULT_GAMES]
        header = [h.strip().lower() for h in rows[0]]
        gi = header.index("game_name") if "game_name" in header else 0
        mi = header.index("metric_type") if "metric_type" in header else 1
        ei = header.index("effective_date") if "effective_date" in header else 2
        result: List[Tuple[str, str, str]] = []
        for row in rows[1:]:
            if len(row) <= max(gi, mi):
                continue
            name = (row[gi] if gi < len(row) else "").strip()
            mt = (row[mi] if mi < len(row) else "").strip().lower()
            ed = (row[ei] if ei < len(row) else "2020-01-01").strip() or "2020-01-01"
            if name and mt in ("time", "guesses", "points"):
                result.append((name, mt, ed))
        return result if result else [(name, mt, "2020-01-01") for name, mt in _DEFAULT_GAMES]

    def add_game_to_registry(self, game_name: str, metric_type: str,
                              added_by: str, effective_date: str) -> None:
        """Add a new game to the GameRegistry sheet. Raises ValueError if duplicate."""
        from game_registry import canonical_game_name, _OPTIONAL_GAMES

        game_name = canonical_game_name(game_name)
        metric_type = metric_type.strip().lower()
        if metric_type not in ("time", "guesses", "points"):
            raise ValueError("Metric type must be time, guesses, or points.")
        expected_metric = dict(_OPTIONAL_GAMES).get(game_name)
        if expected_metric and expected_metric != metric_type:
            raise ValueError(f"{game_name} uses {expected_metric} scoring.")
        existing = self.load_game_registry()
        for name, _, _ in existing:
            if name.lower() == game_name.strip().lower():
                raise ValueError(f"Game '{game_name}' is already registered.")
        row = [game_name.strip(), metric_type.strip().lower(), effective_date, added_by]
        self._write_with_backoff(
            "GameRegistry.add_game",
            lambda: self.registry.append_rows([row], value_input_option="RAW"),
        )

    def _ensure_cols(self, ws, required: List[str]) -> None:
        header = ws.row_values(1)
        if not header:
            grid_cols = getattr(ws, "col_count", None)
            if isinstance(grid_cols, int) and grid_cols < len(required):
                self._write_with_backoff(
                    f"{ws.title}.add_cols",
                    lambda: ws.add_cols(len(required) - grid_cols),
                )
            self._write_with_backoff(
                f"{ws.title}.ensure_header",
                lambda: ws.update(values=[required], range_name="A1"),
            )
            return

        missing = [c for c in required if c not in header]
        if not missing:
            return

        new_header = header + missing

        # A header wider than the sheet's grid is rejected by the Sheets API
        # ("exceeds grid limits"), and this runs at startup. Widen the grid first
        # so adding a column to a sheet that was created narrow can't stop the bot
        # from starting.
        grid_cols = getattr(ws, "col_count", None)
        if isinstance(grid_cols, int) and grid_cols < len(new_header):
            self._write_with_backoff(
                f"{ws.title}.add_cols",
                lambda: ws.add_cols(len(new_header) - grid_cols),
            )

        self._write_with_backoff(
            f"{ws.title}.extend_header",
            lambda: ws.update(values=[new_header], range_name="A1"),
        )

    def _load_recent_event_ids(self, limit: int) -> List[str]:
        rows = self.events.get_all_values()
        if len(rows) <= 1:
            return []
        header = rows[0]
        if "event_id" not in header:
            return []
        event_i = header.index("event_id")
        ids = []
        for r in rows[-limit:]:
            if len(r) > event_i and r[event_i]:
                ids.append(r[event_i])
        return ids

    # ---- Sheets rate-limit helpers ----
    def _is_sheets_429(self, err: Exception) -> bool:
        s = str(err)
        return ("[429]" in s) or ("Quota exceeded" in s and "Read requests" in s)

    def _read_with_backoff(self, fn):
        """Run a Sheets read callable with exponential backoff on 429s."""
        max_retries = self._sheets_429_max_retries
        base = self._sheets_429_base_delay_s
        max_delay = self._sheets_429_max_delay_s

        for attempt in range(max_retries + 1):
            try:
                return fn()
            except Exception as e:
                if (not self._is_sheets_429(e)) or attempt >= max_retries:
                    raise
                delay = min(max_delay, base * (2 ** attempt))
                jitter = random.uniform(0.0, delay * 0.15)
                time.sleep(delay + jitter)

    def _is_retryable_write_error(self, err: Exception) -> bool:
        status = _api_error_status(err)
        if status in (429, 500, 502, 503, 504):
            return True

        s = str(err or "").lower()
        if "service is currently unavailable" in s:
            return True
        if "quota exceeded" in s and "write requests" in s:
            return True
        return False

    def _write_with_backoff(self, op_name: str, fn):
        max_retries = self._sheets_write_max_retries
        base = self._sheets_write_base_delay_s
        max_delay = self._sheets_write_max_delay_s
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                with self._write_lock:
                    return fn()
            except APIError as e:
                if not self._is_retryable_write_error(e):
                    raise
                last_exc = e
            except Exception as e:
                status = _api_error_status(e)
                if status is None or (not self._is_retryable_write_error(e)):
                    raise
                last_exc = e

            if attempt >= max_retries:
                break

            retry_after = _api_error_retry_after(last_exc) if last_exc else None
            delay = retry_after if (retry_after is not None and retry_after > 0) else min(max_delay, base * (2 ** (attempt - 1)))
            jitter = random.uniform(0.0, min(0.35, delay * 0.15))
            sleep_s = delay + jitter

            logger.warning(
                "Sheets write failed during %s on attempt %s/%s; retrying in %.2fs",
                op_name,
                attempt,
                max_retries,
                sleep_s,
            )
            time.sleep(sleep_s)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Sheets write failed during {op_name}")

    def _col_letter(self, col_1_based: int) -> str:
        n = int(col_1_based)
        out: List[str] = []
        while n > 0:
            n, r = divmod(n - 1, 26)
            out.append(chr(ord("A") + r))
        return "".join(reversed(out))

    def _get_daily_header(self, force: bool = False) -> List[str]:
        now = time.time()
        with self._daily_cache_lock:
            if (not force) and self._daily_header_cache and (now - self._daily_header_cache_ts) < self._daily_header_cache_ttl_s:
                return list(self._daily_header_cache)

        header = self._read_with_backoff(lambda: self.daily.row_values(1)) or []
        header = [str(x or "").strip() for x in header]

        with self._daily_cache_lock:
            self._daily_header_cache = header
            self._daily_header_cache_ts = now
        return list(header)

    def _cache_posted_day_key(self, day: str) -> None:
        k = (day or "").strip()
        if not k:
            return
        with self._daily_cache_lock:
            self._posted_days_cache.add(k)
            self._posted_days_cache_ts = time.time()

    def _refresh_posted_days_cache(self, force: bool = False) -> None:
        now = time.time()
        with self._daily_cache_lock:
            if (not force) and self._posted_days_cache and (now - self._posted_days_cache_ts) < self._posted_days_cache_ttl_s:
                return

        header = self._get_daily_header(force=False)
        if ("day" not in header) or ("summary_json" not in header):
            header = list(self.REQUIRED_DAILY_COLS)

        day_col = header.index("day") + 1
        sum_col = header.index("summary_json") + 1
        lo = min(day_col, sum_col)
        hi = max(day_col, sum_col)
        loL = self._col_letter(lo)
        hiL = self._col_letter(hi)
        day_off = day_col - lo
        sum_off = sum_col - lo

        def _read_rect():
            return self.daily.get_values(f"{loL}2:{hiL}")

        rows = self._read_with_backoff(_read_rect) or []

        posted: Set[str] = set()
        for r in rows:
            if not r:
                continue
            d = (r[day_off] if len(r) > day_off else "").strip()
            s = (r[sum_off] if len(r) > sum_off else "").strip()
            if d and s:
                posted.add(d)

        with self._daily_cache_lock:
            self._posted_days_cache = posted
            self._posted_days_cache_ts = now

    def get_posted_days_snapshot(self, force_refresh: bool = False) -> Set[str]:
        self._refresh_posted_days_cache(force=force_refresh)
        with self._daily_cache_lock:
            return set(self._posted_days_cache)

    # ---- Events ----
    def seen_event(self, event_id: str) -> bool:
        with self._event_cache_lock:
            return event_id in self._event_cache

    def claim_event(self, event_id: str) -> bool:
        """Atomically check-and-claim an event id. Returns True if this thread claimed it."""
        with self._event_cache_lock:
            if event_id in self._event_cache:
                return False
            self._event_cache.add(event_id)
        return True

    def _spool_oversized_event(self, event_id: str, now: str, payload: dict) -> None:
        logger.warning("Events payload exceeded the cell limit; full payload spooled locally")
        spool_jsonl(
            "events_spool.jsonl",
            {"event_id": event_id, "ts": now, "payload": payload},
        )
        self._event_cache.add(event_id)

    def log_event(self, event_id: str, payload: dict) -> None:
        now = utc_now_iso()
        try:
            payload_s = serialize_json_for_cell(payload)
        except JsonCellTooLargeError:
            self._spool_oversized_event(event_id, now, payload)
            return
        row = [event_id, now, payload_s]

        try:
            gspread_call(self.events.append_row, row, value_input_option="RAW")
        except Exception:
            logging.exception("Events append_row failed; spooling locally and continuing")
            spool_jsonl(
                "events_spool.jsonl",
                {"event_id": event_id, "ts": now, "payload": payload},
            )

        self._event_cache.add(event_id)

    def bulk_log_events(self, events: List[Tuple[str, dict]]) -> None:
        """Append many Events rows with a single Sheets write.

        *events* is a list of (event_id, payload). Skips ids already in the
        in-memory cache. On failure, spools each row locally (same fallback as
        log_event) so nothing is lost. This avoids the one-append-per-candidate
        pattern that otherwise contributes to write-quota exhaustion during a
        history-scan reconcile.
        """
        if not events:
            return

        rows: List[List[str]] = []
        staged: List[Tuple[str, str, dict]] = []  # (event_id, ts, payload) for spool fallback
        seen_in_batch: Set[str] = set()
        for event_id, payload in events:
            if not event_id or event_id in self._event_cache or event_id in seen_in_batch:
                continue
            seen_in_batch.add(event_id)
            now = utc_now_iso()
            try:
                payload_s = serialize_json_for_cell(payload)
            except JsonCellTooLargeError:
                self._spool_oversized_event(event_id, now, payload)
                continue
            rows.append([event_id, now, payload_s])
            staged.append((event_id, now, payload))

        if not rows:
            return

        try:
            gspread_call(self.events.append_rows, rows, value_input_option="RAW")
        except Exception:
            logging.exception("Events append_rows failed; spooling locally and continuing")
            for event_id, now, payload in staged:
                spool_jsonl(
                    "events_spool.jsonl",
                    {"event_id": event_id, "ts": now, "payload": payload},
                )

        for event_id, _now, _payload in staged:
            self._event_cache.add(event_id)

    # ---- NLQueries ----
    def log_nl_query(
        self,
        *,
        slack_ts: str,
        asker_user_id: str,
        question: str,
        prefix: str,
        intent: str,
        game: str,
        target_user: str,
        stat: str,
        preset: str,
        resolved_start: str,
        resolved_end: str,
        response_prefix: str,
        schema_version: str = "",
        translator_source: str = "",
        raw_spec_json: str = "",
        validated_spec_json: str = "",
        unsupported_reason: str = "",
        rows_scanned: str = "",
    ) -> None:
        """Append a single row to the NLQueries worksheet. Never raises."""
        now = utc_now_iso()
        row = [
            now,
            slack_ts or "",
            asker_user_id or "",
            truncate_for_cell(question or ""),
            prefix or "",
            intent or "",
            game or "",
            target_user or "",
            stat or "",
            preset or "",
            resolved_start or "",
            resolved_end or "",
            truncate_for_cell((response_prefix or "")[:200]),
            schema_version or "",
            translator_source or "",
            truncate_for_cell(raw_spec_json or ""),
            truncate_for_cell(validated_spec_json or ""),
            truncate_for_cell(unsupported_reason or ""),
            rows_scanned or "",
        ]
        try:
            gspread_call(self.nl_queries.append_row, row, value_input_option="RAW")
        except Exception:
            logging.exception("NLQueries append_row failed; spooling locally and continuing")
            spool_jsonl(
                "nl_query_spool.jsonl",
                {
                    "ts": now,
                    "slack_ts": slack_ts,
                    "asker_user_id": asker_user_id,
                    "question": question,
                    "prefix": prefix,
                    "intent": intent,
                    "game": game,
                    "target_user": target_user,
                    "stat": stat,
                    "preset": preset,
                    "resolved_start": resolved_start,
                    "resolved_end": resolved_end,
                    "response_prefix": (response_prefix or "")[:200],
                    "schema_version": schema_version,
                    "translator_source": translator_source,
                    "raw_spec_json": raw_spec_json,
                    "validated_spec_json": validated_spec_json,
                    "unsupported_reason": unsupported_reason,
                    "rows_scanned": rows_scanned,
                },
            )

    # ---- Scores ----
    @staticmethod
    def _score_upsert_key(day: str, user_id: str, game: str, puzzle_id: Any) -> Optional[Tuple[str, str, str, str]]:
        """Identity of a score row for upsert dedup: (day, user, game, puzzle_id)."""
        return score_identity(day, user_id, game, puzzle_id)

    @staticmethod
    def _score_row_is_newer(
        candidate: List[str], incumbent: List[str], idx: Dict[str, int],
    ) -> bool:
        """Compare score edits using Slack time, then stored update time.

        Sheet order remains the deterministic last-row-wins fallback when the
        available timestamps cannot distinguish two legacy rows.
        """
        def cell(row: List[str], name: str) -> str:
            pos = idx.get(name, -1)
            return str(row[pos]).strip() if 0 <= pos < len(row) else ""

        def slack_time(row: List[str]) -> Optional[float]:
            try:
                return float(cell(row, "slack_ts"))
            except (TypeError, ValueError):
                return None

        def update_time(row: List[str]) -> Optional[float]:
            raw = cell(row, "updated_at")
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                return None

        candidate_slack = slack_time(candidate)
        incumbent_slack = slack_time(incumbent)
        if candidate_slack is not None and incumbent_slack is not None and candidate_slack != incumbent_slack:
            return candidate_slack > incumbent_slack

        candidate_updated = update_time(candidate)
        incumbent_updated = update_time(incumbent)
        if candidate_updated is not None and incumbent_updated is not None and candidate_updated != incumbent_updated:
            return candidate_updated > incumbent_updated
        return True  # Equal or incomparable timestamps: the later sheet row wins.

    def _build_score_row(
        self, header: List[str], idx: Dict[str, int],
        day: str, user_id: str, parsed: ParsedScore, slack_ts: str, raw_text: str,
    ) -> List[str]:
        """Render a full Scores row (aligned to *header*) for one upsert."""
        now = datetime.now(TZ).isoformat()
        row_vals = [""] * len(header)

        def set_col(col: str, val: Any) -> None:
            if col in idx:
                row_vals[idx[col]] = str(val)

        set_col("day", day)
        set_col("user_id", user_id)
        set_col("game", parsed.game)
        set_col("puzzle_id", parsed.puzzle_id)
        set_col("metric_type", parsed.metric_type)
        set_col("metric_value", parsed.metric_value)
        set_col("display", parsed.display)
        set_col("slack_ts", slack_ts)
        set_col("raw_text", raw_text)
        set_col("tiebreak_value", "" if parsed.tiebreak_value is None else str(parsed.tiebreak_value))
        set_col("status", getattr(parsed, "status", ""))
        set_col("updated_at", now)
        return row_vals

    def _upsert_score_once(self, day: str, user_id: str, parsed: ParsedScore, slack_ts: str, raw_text: str) -> None:
        rows = self.scores.get_all_values()
        if not rows:
            self.scores.update(values=[self.REQUIRED_SCORES_COLS], range_name="A1")
            rows = self.scores.get_all_values()

        header = rows[0]
        idx = {name: header.index(name) for name in header if name in header}

        # Last legacy occurrence wins; collapse any older duplicate rows for
        # this identity while updating the survivor in place.
        matching_rows: List[int] = []
        if all(k in idx for k in ("day", "user_id", "game", "puzzle_id")):
            want = self._score_upsert_key(day, user_id, parsed.game, parsed.puzzle_id)
            for r_i in range(1, len(rows)):
                r = rows[r_i]
                try:
                    key = self._score_upsert_key(
                        r[idx["day"]], r[idx["user_id"]], r[idx["game"]], r[idx["puzzle_id"]]
                    )
                    if want is not None and key == want:
                        matching_rows.append(r_i + 1)  # 1-based
                except Exception:
                    continue

        row_vals = self._build_score_row(header, idx, day, user_id, parsed, slack_ts, raw_text)

        if not matching_rows:
            self.scores.append_row(row_vals, value_input_option="RAW")
        else:
            target_row = matching_rows[-1]
            if len(matching_rows) == 1:
                start = gspread.utils.rowcol_to_a1(target_row, 1)
                end = gspread.utils.rowcol_to_a1(target_row, len(header))
                self.scores.update(values=[row_vals], range_name=f"{start}:{end}")
                return
            writes = []
            for row_num in [*matching_rows[:-1], target_row]:
                start = gspread.utils.rowcol_to_a1(row_num, 1)
                end = gspread.utils.rowcol_to_a1(row_num, len(header))
                values = [row_vals] if row_num == target_row else [[""] * len(header)]
                writes.append({"range": f"{start}:{end}", "values": values})
            self.scores.batch_update(writes, value_input_option="RAW")

    def upsert_score(self, day: str, user_id: str, parsed: ParsedScore, slack_ts: str, raw_text: str) -> None:
        self._write_with_backoff(
            "Scores.upsert_score",
            lambda: self._upsert_score_once(day, user_id, parsed, slack_ts, raw_text),
        )

    def bulk_upsert_scores(
        self, upserts: List[Tuple[str, str, ParsedScore, str, str]],
    ) -> int:
        """Upsert many scores with a fixed, tiny number of Sheets writes.

        *upserts* is a list of (day, user_id, parsed, slack_ts, raw_text).
        Reads the Scores sheet once, resolves each upsert against existing rows
        (and against earlier upserts in the same batch — last one wins), then
        flushes all in-place edits as a single values.batch_update and all new
        rows as a single append_rows. This replaces the per-score
        read+update pattern that bursts past the Sheets write-per-minute quota
        during reconcile/backfill of a busy day.

        Returns the number of upserts applied (== len(upserts), minus any that
        collapsed onto the same row earlier in the batch).
        """
        if not upserts:
            return 0

        def _flush() -> int:
            rows = self.scores.get_all_values()
            if not rows:
                self.scores.update(values=[self.REQUIRED_SCORES_COLS], range_name="A1")
                rows = [list(self.REQUIRED_SCORES_COLS)]

            header = rows[0]
            idx = {name: header.index(name) for name in header if name in header}
            ncols = len(header)
            have_key_cols = all(k in idx for k in ("day", "user_id", "game", "puzzle_id"))

            # Map identity -> all 1-based rows; for a touched legacy duplicate,
            # the last occurrence survives and older duplicates are cleared.
            existing_by_key: Dict[Tuple[str, str, str, str], List[int]] = {}
            if have_key_cols:
                for r_i in range(1, len(rows)):
                    r = rows[r_i]
                    try:
                        key = self._score_upsert_key(
                            r[idx["day"]], r[idx["user_id"]], r[idx["game"]], r[idx["puzzle_id"]]
                        )
                    except Exception:
                        continue
                    if key is not None:
                        existing_by_key.setdefault(key, []).append(r_i + 1)

            # Accumulate edits keyed by row number so repeats within the batch
            # collapse onto one write; new rows are appended in arrival order,
            # but a later upsert of the same new identity overwrites the earlier.
            updates_by_row: Dict[int, List[str]] = {}
            duplicate_rows_to_clear: Set[int] = set()
            appends: List[List[str]] = []
            append_pos_by_key: Dict[Tuple[str, str, str, str], int] = {}
            applied = 0

            for day, user_id, parsed, slack_ts, raw_text in upserts:
                row_vals = self._build_score_row(header, idx, day, user_id, parsed, slack_ts, raw_text)
                key = self._score_upsert_key(day, user_id, parsed.game, parsed.puzzle_id) if have_key_cols else None

                applied += 1
                if key is not None and key in existing_by_key:
                    matches = existing_by_key[key]
                    updates_by_row[matches[-1]] = row_vals
                    duplicate_rows_to_clear.update(matches[:-1])
                elif key is not None and key in append_pos_by_key:
                    appends[append_pos_by_key[key]] = row_vals
                else:
                    if key is not None:
                        append_pos_by_key[key] = len(appends)
                    appends.append(row_vals)

            # One batch_update for all in-place edits.
            if updates_by_row:
                data = []
                for row_num, row_vals in sorted(updates_by_row.items()):
                    start = gspread.utils.rowcol_to_a1(row_num, 1)
                    end = gspread.utils.rowcol_to_a1(row_num, ncols)
                    data.append({"range": f"{start}:{end}", "values": [row_vals]})
                for row_num in sorted(duplicate_rows_to_clear):
                    start = gspread.utils.rowcol_to_a1(row_num, 1)
                    end = gspread.utils.rowcol_to_a1(row_num, ncols)
                    data.append({"range": f"{start}:{end}", "values": [[""] * ncols]})
                self.scores.batch_update(data, value_input_option="RAW")
            elif duplicate_rows_to_clear:
                data = []
                for row_num in sorted(duplicate_rows_to_clear):
                    start = gspread.utils.rowcol_to_a1(row_num, 1)
                    end = gspread.utils.rowcol_to_a1(row_num, ncols)
                    data.append({"range": f"{start}:{end}", "values": [[""] * ncols]})
                self.scores.batch_update(data, value_input_option="RAW")

            # One append_rows for all new rows.
            if appends:
                self.scores.append_rows(appends, value_input_option="RAW")

            return applied

        return self._write_with_backoff("Scores.bulk_upsert_scores", _flush)

    def list_days_with_scores(self) -> List[str]:
        rows = self.scores.get_all_values()
        if len(rows) <= 1:
            return []
        header = rows[0]
        if 'day' not in header:
            return []
        day_i = header.index('day')
        days = sorted({(r[day_i] if len(r) > day_i else '').strip() for r in rows[1:]} - {''})
        return days

    def _move_score_day_once(
        self,
        old_day: str,
        new_day: str,
        user_id: str,
        game: str,
        puzzle_id: int,
        slack_ts: Optional[str] = None,
    ) -> bool:
        rows = self.scores.get_all_values()
        if len(rows) <= 1:
            return False
        header = rows[0]
        if not all(c in header for c in ('day', 'user_id', 'game', 'puzzle_id')):
            return False
        idx = {name: header.index(name) for name in header}
        day_i = idx['day']
        uid_i = idx['user_id']
        game_i = idx['game']
        pid_i = idx['puzzle_id']

        source_rows: List[int] = []
        destination_rows: List[int] = []
        source_identity = self._score_upsert_key(old_day, user_id, game, puzzle_id)
        destination_identity = self._score_upsert_key(new_day, user_id, game, puzzle_id)
        for r_i in range(1, len(rows)):
            r = rows[r_i]
            try:
                key = self._score_upsert_key(
                    r[day_i] if len(r) > day_i else '',
                    r[uid_i] if len(r) > uid_i else '',
                    r[game_i] if len(r) > game_i else '',
                    r[pid_i] if len(r) > pid_i else '',
                )
                if key is None or source_identity is None or key[1:] != source_identity[1:]:
                    continue
                if key[0] == source_identity[0]:
                    source_rows.append(r_i + 1)
                elif destination_identity is not None and key[0] == destination_identity[0]:
                    destination_rows.append(r_i + 1)
            except Exception:
                continue

        if not source_rows:
            return False

        if slack_ts:
            slack_i = idx.get('slack_ts', -1)
            if slack_i >= 0 and not any(
                str(rows[row_num - 1][slack_i] if len(rows[row_num - 1]) > slack_i else '').strip()
                == str(slack_ts).strip()
                for row_num in source_rows
            ):
                return False

        # Do not move into a day that's already been posted (would change history).
        if self.day_already_posted(new_day):
            return False

        # Legacy duplicates within each bucket use last-row-wins. Across the
        # source and destination buckets, prefer a distinguishably newer Slack
        # message; equal Slack timestamps favor destination because replay can
        # refresh updated_at without proving that its score is a newer edit.
        candidates = sorted(source_rows + destination_rows)
        source_winner = source_rows[-1]
        if destination_rows:
            destination_winner = destination_rows[-1]
            source_row = rows[source_winner - 1]
            destination_row = rows[destination_winner - 1]
            slack_i = idx.get('slack_ts', -1)
            source_slack = str(source_row[slack_i]).strip() if slack_i >= 0 and len(source_row) > slack_i else ''
            destination_slack = str(destination_row[slack_i]).strip() if slack_i >= 0 and len(destination_row) > slack_i else ''
            try:
                source_slack_time = float(source_slack)
                destination_slack_time = float(destination_slack)
            except (TypeError, ValueError):
                source_slack_time = destination_slack_time = None

            if source_slack and source_slack == destination_slack:
                winner = destination_winner
            elif (
                source_slack_time is not None
                and destination_slack_time is not None
            ):
                if source_slack_time == destination_slack_time:
                    winner = destination_winner
                else:
                    winner = source_winner if source_slack_time > destination_slack_time else destination_winner
            else:
                winner = source_winner if self._score_row_is_newer(source_row, destination_row, idx) else destination_winner
        else:
            winner = source_winner

        merged = list(rows[winner - 1])
        if len(merged) < len(header):
            merged.extend([""] * (len(header) - len(merged)))
        merged[day_i] = new_day

        writes = []
        for row_num in candidates:
            start = gspread.utils.rowcol_to_a1(row_num, 1)
            end = gspread.utils.rowcol_to_a1(row_num, len(header))
            values = [merged] if row_num == winner else [[""] * len(header)]
            writes.append({"range": f"{start}:{end}", "values": values})
        self.scores.batch_update(writes, value_input_option="RAW")
        return True

    def move_score_day(
        self,
        old_day: str,
        new_day: str,
        user_id: str,
        game: str,
        puzzle_id: int,
        slack_ts: Optional[str] = None,
    ) -> bool:
        """Move a single score row to a different day bucket. Returns True if moved."""
        return bool(self._write_with_backoff(
            "Scores.move_score_day",
            lambda: self._move_score_day_once(old_day, new_day, user_id, game, puzzle_id, slack_ts=slack_ts),
        ))

    def load_scores_for_day(self, day: str) -> List[dict]:
        rows = self.scores.get_all_values()
        if len(rows) <= 1:
            return []
        header = rows[0]
        out = []
        for r in rows[1:]:
            if not r:
                continue
            rec = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
            if rec.get("day") == day:
                if "game" in rec:
                    rec["game"] = normalize_game(rec["game"])
                out.append(rec)
        return out

    # ---- Daily ledger helpers ----

    def _find_daily_row(self, day: str) -> Tuple[Optional[int], List[List[str]], List[str]]:
        """Find the last DailyResults row for *day* that has a non-empty summary_json.

        Returns (target_row_1based_or_None, all_rows, header).
        """
        rows = self.daily.get_all_values()
        if len(rows) <= 1:
            return None, rows, []
        header = rows[0]
        if "day" not in header or "summary_json" not in header:
            return None, rows, header

        day_i = header.index("day")
        sum_i = header.index("summary_json")

        target_row: Optional[int] = None
        for r_i in range(1, len(rows)):
            r = rows[r_i]
            if len(r) <= max(day_i, sum_i):
                continue
            if r[day_i] != day:
                continue
            if not (r[sum_i] or "").strip():
                continue
            target_row = r_i + 1  # 1-based

        return target_row, rows, header

    def _aggregate_awards_from_rows(
        self, data_rows: List[List[str]], sum_i: int,
    ) -> Dict[str, AwardTally]:
        """Aggregate awards from summary_json cells in the given rows.

        Returns {user_id: AwardTally}. Legacy days contribute wins/ties and medal
        days contribute gold/silver/bronze/points, so a range that crosses the
        cut-over carries both.
        """
        totals: Dict[str, AwardTally] = {}

        for r in data_rows:
            if len(r) <= sum_i:
                continue
            raw = (r[sum_i] or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            awards = extract_awards_from_summary(payload)
            if not isinstance(awards, dict):
                continue

            for uid, v in awards.items():
                uid = (uid or "").strip()
                if not uid:
                    continue
                totals[uid] = totals.get(uid, AwardTally()) + unpack_awards(v)

        return totals

    # ---- Daily ledger ----
    def day_already_posted(self, day: str) -> bool:
        k = (day or "").strip()
        if not k:
            return False
        self._refresh_posted_days_cache(force=False)
        with self._daily_cache_lock:
            return k in self._posted_days_cache

    def _mark_day_posted_once(self, day: str, summary: dict) -> bool:
        summary_s = serialize_json_for_cell(summary)
        self._ensure_cols(self.daily, self.REQUIRED_DAILY_COLS)
        rows = self.daily.get_all_values()
        header = [str(x or "").strip() for x in (rows[0] if rows else [])]

        if "day" not in header or "posted_at" not in header or "summary_json" not in header:
            self.daily.update(values=[self.REQUIRED_DAILY_COLS], range_name="A1")
            rows = self.daily.get_all_values()
            header = [str(x or "").strip() for x in (rows[0] if rows else [])]

        idx = {name: header.index(name) for name in header}

        day_i = idx["day"]
        sum_i = idx["summary_json"]
        for r in rows[1:]:
            d = (r[day_i] if len(r) > day_i else "").strip()
            s = (r[sum_i] if len(r) > sum_i else "").strip()
            if d == (day or "").strip() and s:
                return False

        row_vals = [""] * len(header)
        row_vals[idx["day"]] = (day or "").strip()
        row_vals[idx["posted_at"]] = datetime.now(TZ).isoformat()
        row_vals[idx["summary_json"]] = summary_s

        self.daily.append_row(row_vals, value_input_option="RAW")
        return True

    def mark_day_posted(self, day: str, summary: dict) -> bool:
        appended = self._write_with_backoff(
            "DailyResults.mark_day_posted",
            lambda: self._mark_day_posted_once(day, summary),
        )

        if appended:
            self._cache_posted_day_key(day)
        return bool(appended)

    def update_day_summary(self, day: str, updates: Dict[str, Any]) -> bool:
        """Patch summary_json for an already-posted day."""
        target_row, rows, header = self._find_daily_row(day)
        if target_row is None:
            return False

        sum_i = header.index("summary_json")
        raw = (rows[target_row - 1][sum_i] or "").strip()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("DailyResults summary_json for %s is invalid; patch skipped", day)
            return False

        if not isinstance(payload, dict):
            logger.warning("DailyResults summary_json for %s is not an object; patch skipped", day)
            return False

        for k, v in (updates or {}).items():
            payload[k] = v

        payload_s = serialize_json_for_cell(payload)
        cell = gspread.utils.rowcol_to_a1(target_row, sum_i + 1)
        self._write_with_backoff(
            "DailyResults.update_day_summary",
            lambda: self.daily.update(values=[[payload_s]], range_name=cell),
        )
        return True

    def replace_day_summary(self, day: str, summary: dict) -> bool:
        """Fully replace summary_json for an already-posted day (used by force re-finalize)."""
        summary_s = serialize_json_for_cell(summary)
        target_row, rows, header = self._find_daily_row(day)
        if target_row is None:
            return False

        sum_i = header.index("summary_json")
        posted_i = header.index("posted_at") if "posted_at" in header else None

        cell = gspread.utils.rowcol_to_a1(target_row, sum_i + 1)
        self._write_with_backoff(
            "DailyResults.replace_day_summary",
            lambda: self.daily.update(values=[[summary_s]], range_name=cell),
        )
        if posted_i is not None:
            ts_cell = gspread.utils.rowcol_to_a1(target_row, posted_i + 1)
            self._write_with_backoff(
                "DailyResults.replace_posted_at",
                lambda: self.daily.update(
                    values=[[datetime.now(TZ).isoformat()]], range_name=ts_cell,
                ),
            )
        return True

    def load_daily_payloads_in_range(self, start_day: str, end_day: str) -> Dict[str, Dict[str, Any]]:
        """Return {day: parsed summary_json} for every posted day in an inclusive range.

        One read of DailyResults serves a whole monthly rollup, instead of the
        per-day reads that load_monthly_totals_map would cost. Days with an
        empty or unparseable summary_json are skipped.
        """
        start = _parse_day_key_loose(start_day)
        end = _parse_day_key_loose(end_day)
        if not start or not end:
            return {}
        if end < start:
            start, end = end, start

        rows = self.daily.get_all_values()
        if len(rows) <= 1:
            return {}

        header = rows[0]
        if "day" not in header or "summary_json" not in header:
            return {}

        day_i = header.index("day")
        sum_i = header.index("summary_json")

        out: Dict[str, Dict[str, Any]] = {}
        for r in rows[1:]:
            if len(r) <= max(day_i, sum_i):
                continue
            rd = _parse_day_key_loose((r[day_i] or "").strip())
            if not rd or rd < start or rd > end:
                continue
            raw = (r[sum_i] or "").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                # Later rows win, matching _find_daily_row's last-wins rule.
                out[rd.isoformat()] = payload
        return out

    # ---- Monthly ledger ----
    def _find_monthly_row(self, month: str) -> Tuple[Optional[int], List[List[str]], List[str]]:
        """Find the last MonthlyResults row for *month* with a non-empty summary_json.

        Returns (target_row_1based_or_None, all_rows, header).
        """
        rows = self.monthly.get_all_values()
        if len(rows) <= 1:
            return None, rows, []
        header = rows[0]
        if "month" not in header or "summary_json" not in header:
            return None, rows, header

        month_i = header.index("month")
        sum_i = header.index("summary_json")

        target_row: Optional[int] = None
        for r_i in range(1, len(rows)):
            r = rows[r_i]
            if len(r) <= max(month_i, sum_i):
                continue
            if (r[month_i] or "").strip() != (month or "").strip():
                continue
            if not (r[sum_i] or "").strip():
                continue
            target_row = r_i + 1  # 1-based

        return target_row, rows, header

    def month_already_posted(self, month: str) -> bool:
        k = (month or "").strip()
        if not k:
            return False
        target_row, _rows, _header = self._read_with_backoff(lambda: self._find_monthly_row(k))
        return target_row is not None

    def list_posted_months(self) -> List[str]:
        rows = self._read_with_backoff(self.monthly.get_all_values)
        if len(rows) <= 1:
            return []
        header = rows[0]
        if "month" not in header or "summary_json" not in header:
            return []
        month_i = header.index("month")
        sum_i = header.index("summary_json")

        months: Set[str] = set()
        for r in rows[1:]:
            if len(r) <= max(month_i, sum_i):
                continue
            m = (r[month_i] or "").strip()
            if m and (r[sum_i] or "").strip():
                months.add(m)
        return sorted(months)

    def _mark_month_posted_once(self, month: str, summary: dict) -> bool:
        summary_s = serialize_json_for_cell(summary)
        self._ensure_cols(self.monthly, self.REQUIRED_MONTHLY_COLS)
        rows = self.monthly.get_all_values()
        header = [str(x or "").strip() for x in (rows[0] if rows else [])]

        if not all(c in header for c in self.REQUIRED_MONTHLY_COLS):
            self.monthly.update(values=[self.REQUIRED_MONTHLY_COLS], range_name="A1")
            rows = self.monthly.get_all_values()
            header = [str(x or "").strip() for x in (rows[0] if rows else [])]

        idx = {name: header.index(name) for name in header}

        month_i = idx["month"]
        sum_i = idx["summary_json"]
        for r in rows[1:]:
            m = (r[month_i] if len(r) > month_i else "").strip()
            s = (r[sum_i] if len(r) > sum_i else "").strip()
            if m == (month or "").strip() and s:
                return False

        row_vals = [""] * len(header)
        row_vals[month_i] = (month or "").strip()
        row_vals[idx["posted_at"]] = datetime.now(TZ).isoformat()
        row_vals[sum_i] = summary_s

        self.monthly.append_row(row_vals, value_input_option="RAW")
        return True

    def mark_month_posted(self, month: str, summary: dict) -> bool:
        """Append the MonthlyResults row for *month*.

        Returns True if this call wrote the row, False if the month was already
        recorded (the claim that makes the rollover hook idempotent).
        """
        return self._write_with_backoff(
            "MonthlyResults.mark_month_posted",
            lambda: self._mark_month_posted_once(month, summary),
        )

    def update_month_summary(self, month: str, updates: Dict[str, Any]) -> bool:
        """Patch summary_json for an already-posted month (e.g. to store Slack ts)."""
        target_row, rows, header = self._find_monthly_row(month)
        if target_row is None:
            return False

        sum_i = header.index("summary_json")
        raw = (rows[target_row - 1][sum_i] or "").strip()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("MonthlyResults summary_json for %s is invalid; patch skipped", month)
            return False

        if not isinstance(payload, dict):
            logger.warning("MonthlyResults summary_json for %s is not an object; patch skipped", month)
            return False

        for k, v in (updates or {}).items():
            payload[k] = v

        payload_s = serialize_json_for_cell(payload)
        cell = gspread.utils.rowcol_to_a1(target_row, sum_i + 1)
        self._write_with_backoff(
            "MonthlyResults.update_month_summary",
            lambda: self.monthly.update(
                values=[[payload_s]],
                range_name=cell,
            ),
        )
        return True

    def replace_month_summary(self, month: str, summary: dict) -> bool:
        """Fully replace summary_json for an already-posted month (force re-finalize)."""
        summary_s = serialize_json_for_cell(summary)
        target_row, _rows, header = self._find_monthly_row(month)
        if target_row is None:
            return False

        sum_i = header.index("summary_json")
        posted_i = header.index("posted_at") if "posted_at" in header else None

        cell = gspread.utils.rowcol_to_a1(target_row, sum_i + 1)
        self._write_with_backoff(
            "MonthlyResults.replace_month_summary",
            lambda: self.monthly.update(
                values=[[summary_s]],
                range_name=cell,
            ),
        )
        if posted_i is not None:
            ts_cell = gspread.utils.rowcol_to_a1(target_row, posted_i + 1)
            self._write_with_backoff(
                "MonthlyResults.replace_posted_at",
                lambda: self.monthly.update(
                    values=[[datetime.now(TZ).isoformat()]], range_name=ts_cell,
                ),
            )
        return True

    # ---- Totals derived view ----

    def backfill_daily_ledger_from_scores(self) -> int:
        """Migration helper: derive daily summaries from Scores and write to DailyResults."""
        # Lazy imports to avoid circular dependencies at module level
        from day_utils import (
            is_day_closed, choose_primary_puzzle_ids, filter_records_to_primary_puzzles,
            expected_players_for_day, count_complete_players,
            move_future_puzzle_scores,
        )
        from scoring import compute_daily_winners
        from insights import build_daily_facts, build_daily_recap_text

        rows = self.scores.get_all_values()
        if len(rows) <= 1:
            return 0
        header = rows[0]
        if "day" not in header:
            return 0

        day_i = header.index("day")
        days = sorted({(r[day_i] if len(r) > day_i else "").strip() for r in rows[1:]} - {""})

        backfilled = 0
        for day in days:
            if self.day_already_posted(day):
                continue
            recs = self.load_scores_for_day(day)
            if not recs:
                continue

            if not is_day_closed(day):
                continue

            primary = choose_primary_puzzle_ids(recs)

            moved_any = move_future_puzzle_scores(day, recs, primary, self)

            if moved_any:
                recs = self.load_scores_for_day(day)
                if not recs:
                    continue
                primary = choose_primary_puzzle_ids(recs)

            recs_primary = filter_records_to_primary_puzzles(recs, primary)
            winners_by_game, awards_by_user, best_display = compute_daily_winners(recs_primary, day=day)
            expected = expected_players_for_day(day, store_obj=self)
            complete = count_complete_players(recs_primary, day=day)

            recap_facts = build_daily_facts(
                day=day,
                records=recs_primary,
                awards_by_user=awards_by_user,
                best_display_by_game=best_display,
                expected_players=expected,
                complete_players=complete,
            )
            recap_text = build_daily_recap_text(recap_facts)

            payload = {
                "day": day,
                "winners_by_game": winners_by_game,
                "awards_by_user": awards_by_user,
                "best_display": best_display,
                "recap_facts": recap_facts,
                "recap_text": recap_text,
                "primary_puzzle_id_by_game": primary,
                "expected_players": expected,
                "complete_players": complete,
                "source": "backfill_from_scores",
            }
            self.mark_day_posted(day, payload)
            backfilled += 1

        return backfilled

    def rebuild_totals_from_daily(self) -> None:
        """Totals is derived from DailyResults.summary_json."""
        daily_rows = self.daily.get_all_values()
        if len(daily_rows) <= 1:
            return

        daily_header = daily_rows[0]
        if "summary_json" not in daily_header:
            return

        sum_i = daily_header.index("summary_json")

        has_any = any((len(r) > sum_i and (r[sum_i] or "").strip()) for r in daily_rows[1:])
        if not has_any:
            n = self.backfill_daily_ledger_from_scores()
            if n:
                daily_rows = self.daily.get_all_values()
                daily_header = daily_rows[0]
                sum_i = daily_header.index("summary_json")

        totals = self._aggregate_awards_from_rows(daily_rows[1:], sum_i)

        # Ensure totals header
        tot_rows = self.totals.get_all_values()
        if not tot_rows:
            self._write_with_backoff("Totals.ensure_header", lambda: self.totals.update(values=[self.REQUIRED_TOTALS_COLS], range_name="A1"))
            tot_rows = self.totals.get_all_values()

        self._ensure_cols(self.totals, self.REQUIRED_TOTALS_COLS)
        tot_header = self.totals.row_values(1)
        idx = {name: tot_header.index(name) for name in tot_header if name in tot_header}

        if self.totals.row_count > 1:
            try:
                self._write_with_backoff("Totals.delete_rows", lambda: self.totals.delete_rows(2, self.totals.row_count))
            except Exception:
                self._write_with_backoff("Totals.batch_clear", lambda: self.totals.batch_clear(["A2:Z1000"]))

        out_rows = []
        for uid in sorted(totals):
            t = totals[uid]
            row = [""] * len(tot_header)
            row[idx["user_id"]] = uid
            row[idx["trophies"]] = str(t.wins)
            row[idx["ties"]] = str(t.ties)
            row[idx["gold"]] = str(t.gold)
            row[idx["silver"]] = str(t.silver)
            row[idx["bronze"]] = str(t.bronze)
            row[idx["points"]] = str(t.points)
            out_rows.append(row)

        if out_rows:
            self._write_with_backoff("Totals.append_rows", lambda: self.totals.append_rows(out_rows, value_input_option="RAW"))

    def load_monthly_totals_map(self, month_start_day: str, month_end_day: str) -> Dict[str, Dict[str, int]]:
        """Aggregate awards from DailyResults.summary_json for an inclusive day-key range.

        Returns {user_id: AwardTally.as_dict()}: wins/ties/gold/silver/bronze/points.
        """
        start = _parse_day_key_loose(month_start_day)
        end = _parse_day_key_loose(month_end_day)
        if not start or not end:
            return {}
        if end < start:
            start, end = end, start

        rows = self.daily.get_all_values()
        if len(rows) <= 1:
            return {}

        header = rows[0]
        if "day" not in header or "summary_json" not in header:
            return {}

        day_i = header.index("day")
        sum_i = header.index("summary_json")

        # Filter to rows within the date range
        filtered_rows = []
        for r in rows[1:]:
            if len(r) <= max(day_i, sum_i):
                continue
            rk = (r[day_i] or "").strip()
            rd = _parse_day_key_loose(rk)
            if not rd or rd < start or rd > end:
                continue
            filtered_rows.append(r)

        return {uid: t.as_dict() for uid, t in self._aggregate_awards_from_rows(filtered_rows, sum_i).items()}

    def load_totals_map(self) -> Dict[str, Dict[str, int]]:
        """All-time totals from the Totals sheet, as {user_id: AwardTally.as_dict()}.

        Columns the sheet doesn't have yet (the medal columns, before the first
        rebuild after the cut-over) read as zero.
        """
        rows = self.totals.get_all_values()
        if len(rows) <= 1:
            return {}
        header = rows[0]
        if "user_id" not in header or "trophies" not in header:
            return {}
        user_i = header.index("user_id")
        # Totals column -> tally field.
        columns = {
            name: header.index(name)
            for name in ("trophies", "ties", "gold", "silver", "bronze", "points")
            if name in header
        }

        def cell(r: List[str], name: str) -> int:
            i = columns.get(name, -1)
            raw = r[i] if 0 <= i < len(r) else ""
            return int(raw) if str(raw).strip() else 0

        out: Dict[str, Dict[str, int]] = {}
        for r in rows[1:]:
            if len(r) <= user_i:
                continue
            uid = (r[user_i] or "").strip()
            if not uid:
                continue
            out[uid] = AwardTally(
                wins=cell(r, "trophies"), ties=cell(r, "ties"),
                gold=cell(r, "gold"), silver=cell(r, "silver"), bronze=cell(r, "bronze"),
                points=cell(r, "points"),
            ).as_dict()
        return out
