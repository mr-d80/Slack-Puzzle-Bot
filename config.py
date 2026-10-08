"""config.py

Centralized configuration, logging setup, and environment-driven constants
for the Slack Puzzle Tracker bot.
"""

import os
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from awards import TROPHY, NECKTIE, GOLD, SILVER, BRONZE  # noqa: F401  (re-exported)

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv  # type: ignore

    base = Path(__file__).resolve().parent
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        load_dotenv(dotenv_path=base / ".env", override=False)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("justwordle-bot")

logger.info(
    "AI recap config: enabled=%s key=%s model=%s",
    os.environ.get("AI_REWRITE_ENABLED", ""),
    "set" if os.environ.get("OPENAI_API_KEY", "").strip() else "missing",
    os.environ.get("OPENAI_MODEL", ""),
)

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


class _MonthlyRotatingHandler(TimedRotatingFileHandler):
    """Rotate the log file on the 1st of each month."""

    def __init__(self, log_dir: Path, **kwargs):
        self._log_dir = log_dir
        path = self._current_path()
        super().__init__(str(path), when="midnight", interval=1, backupCount=0, encoding="utf-8", **kwargs)

    def _current_path(self) -> Path:
        now = datetime.now()
        return self._log_dir / f"score-bot-{now.strftime('%Y-%m')}.log"

    def shouldRollover(self, record) -> int:
        if self._current_path() != Path(self.baseFilename):
            return 1
        return 0

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        new_path = self._current_path()
        self.baseFilename = str(new_path)
        self.stream = self._open()


root = logging.getLogger()
root.setLevel(logging.INFO)

# A stable marker survives importlib.reload(), unlike an isinstance() check
# against this module's freshly-created handler class.
_FILE_HANDLER_MARKER = "_slack_puzzle_tracker_monthly_handler"
file_handler = next(
    (
        handler
        for handler in root.handlers
        if getattr(handler, _FILE_HANDLER_MARKER, False)
        and Path(getattr(handler, "_log_dir", "")).resolve() == LOG_DIR.resolve()
    ),
    None,
)
if file_handler is None:
    file_handler = _MonthlyRotatingHandler(LOG_DIR)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(file_handler, _FILE_HANDLER_MARKER, True)
    root.addHandler(file_handler)

logging.getLogger(__name__).info("Logging to %s", file_handler.baseFilename)

# ---------------------------------------------------------------------------
# Timezones & formatting
# ---------------------------------------------------------------------------
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Vancouver"))
SCORE_DAY_TZ = ZoneInfo(os.environ.get("SCORE_DAY_TZ_NAME", "America/Vancouver"))
DAY_FMT = "%Y-%m-%d"

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
SCORE_CHANNEL_ID = os.environ.get("SCORE_CHANNEL_ID", "").strip()
ADMIN_USER_IDS = frozenset(
    user_id.strip()
    for user_id in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if user_id.strip()
)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
_GOOGLE_SA_FILE_RAW = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
if _GOOGLE_SA_FILE_RAW:
    _google_sa_path = Path(_GOOGLE_SA_FILE_RAW).expanduser()
    if not _google_sa_path.is_absolute():
        _google_sa_path = Path(__file__).resolve().parent / _google_sa_path
    GOOGLE_SA_FILE = str(_google_sa_path.resolve())
else:
    GOOGLE_SA_FILE = ""

# ---------------------------------------------------------------------------
# Game / scoring
# ---------------------------------------------------------------------------
_PLAYERS_EXPECTED_RAW = os.environ.get("PLAYERS_EXPECTED", "4").strip()
try:
    PLAYERS_EXPECTED = int(_PLAYERS_EXPECTED_RAW)
except ValueError:
    # Keep imports available so startup validation can report this together
    # with any other configuration errors.
    PLAYERS_EXPECTED = 4

# The award emoji and the scoring rules live in awards.py. Days from
# MEDAL_SCORING_START (default 2026-10-01) are scored with medals; earlier days
# keep the trophy/necktie rules. The emoji stay importable from here.

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------
POST_DAILY_RECAP = os.environ.get("POST_DAILY_RECAP", "1").strip().lower() in ("1", "true", "yes")
DAILY_RECAP_IN_THREAD = os.environ.get("DAILY_RECAP_IN_THREAD", "1").strip().lower() in ("1", "true", "yes")

# Monthly summary. Posted once per month, the first time a day is finalized
# after the month's last score day has closed.
POST_MONTHLY_SUMMARY = os.environ.get("POST_MONTHLY_SUMMARY", "1").strip().lower() in ("1", "true", "yes")
MONTHLY_SUMMARY_IN_THREAD = os.environ.get("MONTHLY_SUMMARY_IN_THREAD", "1").strip().lower() in ("1", "true", "yes")
POST_MONTHLY_PLAYER_BREAKDOWNS = os.environ.get("POST_MONTHLY_PLAYER_BREAKDOWNS", "1").strip().lower() in ("1", "true", "yes")

# How stale a month may be and still be auto-posted. The MonthlyResults ledger
# starts empty, so without this the first rollover after deploying would treat
# every month already in DailyResults as unposted and dump the lot into the
# channel. Old months stay available through `monthly_summary.py --month`.
# Set to 0 to disable the limit.
try:
    MONTHLY_SUMMARY_MAX_AGE_DAYS = int(os.environ.get("MONTHLY_SUMMARY_MAX_AGE_DAYS", "14"))
except ValueError:
    MONTHLY_SUMMARY_MAX_AGE_DAYS = 14

NL_QUERY_ENABLED = os.environ.get("NL_QUERY_ENABLED", "1").strip().lower() in ("1", "true", "yes")
NL_QUERY_IN_THREAD = os.environ.get("NL_QUERY_IN_THREAD", "1").strip().lower() in ("1", "true", "yes")


def validate_runtime_config() -> None:
    """Raise one actionable error listing every invalid runtime setting."""
    required = {
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
        "SLACK_APP_TOKEN": SLACK_APP_TOKEN,
        "SCORE_CHANNEL_ID": SCORE_CHANNEL_ID,
        "SPREADSHEET_ID": SPREADSHEET_ID,
        "GOOGLE_SERVICE_ACCOUNT_FILE": _GOOGLE_SA_FILE_RAW,
    }
    errors = [f"{name} must be set to a non-empty value." for name, value in required.items() if not value.strip()]

    if _GOOGLE_SA_FILE_RAW:
        credential_path = Path(GOOGLE_SA_FILE)
        if not credential_path.is_file():
            errors.append(
                "GOOGLE_SERVICE_ACCOUNT_FILE must point to an existing credential file "
                f"(resolved path: {credential_path})."
            )

    try:
        players_expected = int(_PLAYERS_EXPECTED_RAW)
    except ValueError:
        errors.append("PLAYERS_EXPECTED must be a positive integer.")
    else:
        if players_expected <= 0:
            errors.append("PLAYERS_EXPECTED must be a positive integer greater than zero.")

    if errors:
        raise ValueError("Invalid runtime configuration:\n- " + "\n- ".join(errors))
