"""Runtime and finalization-stall diagnostics.

The finalization watchdog observes a stuck operation without changing the
worker's locks or interrupting it.  Its tracebacks intentionally contain no
locals; a short, sanitized header records the phase that was active.
"""

from __future__ import annotations

import faulthandler
import importlib.metadata
import logging
import math
import os
import platform
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


DEFAULT_FINALIZATION_STALL_TIMEOUT_S = 120.0
_TIMEOUT_ENV_VAR = "FINALIZATION_STALL_TIMEOUT_S"
_SAFE_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_PHASE_RE = re.compile(r"[^A-Za-z0-9 _/+.-]")


def validate_stall_timeout(value: Any) -> float:
    """Return a finite, positive watchdog timeout or raise an actionable error."""
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{_TIMEOUT_ENV_VAR} must be a finite positive number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{_TIMEOUT_ENV_VAR} must be a finite positive number of seconds")
    return timeout


def _configured_stall_timeout() -> float:
    raw = os.environ.get(_TIMEOUT_ENV_VAR, str(DEFAULT_FINALIZATION_STALL_TIMEOUT_S))
    return validate_stall_timeout(raw)


FINALIZATION_STALL_TIMEOUT_S = DEFAULT_FINALIZATION_STALL_TIMEOUT_S


def get_finalization_stall_timeout_s() -> float:
    """Resolve and validate the timeout after environment-backed config loads."""
    return _configured_stall_timeout()


def _safe_day(day: Any) -> str:
    value = str(day) if day is not None else "unknown"
    return value if _SAFE_DAY_RE.fullmatch(value) else "unknown"


def _safe_phase(phase: Any) -> str:
    value = _SAFE_PHASE_RE.sub("", str(phase or "unknown")).strip()
    return value[:80] or "unknown"


class FinalizationTracker:
    """Context-managed operation tracker with one daemon watchdog timer.

    Use :meth:`operation` around each remote operation. Nested finalization
    callers should share their outer tracker so a batch has only one watchdog.
    """

    def __init__(
        self,
        *,
        project_dir: Optional[os.PathLike[str] | str] = None,
        timeout_s: Optional[float] = None,
        logger: Optional[logging.Logger] = None,
        timer_factory: Optional[Callable[..., Any]] = None,
        dump_traceback: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.timeout_s = validate_stall_timeout(_configured_stall_timeout() if timeout_s is None else timeout_s)
        self.project_dir = Path(project_dir) if project_dir is not None else Path(__file__).resolve().parent
        self.log_path = self.project_dir / "logs" / "finalization-stalls.log"
        self.logger = logger or logging.getLogger(__name__)
        self._timer_factory = timer_factory or threading.Timer
        self._dump_traceback = dump_traceback or faulthandler.dump_traceback
        self._guard = threading.Lock()
        self._timer: Any = None
        self._timer_started = False
        self._started_at: Optional[float] = None
        self._phase_started_at: Optional[float] = None
        self._day = "unknown"
        self._phase = "starting"
        self._closed = False
        self._fired = False

    def __enter__(self) -> "FinalizationTracker":
        self._started_at = time.monotonic()
        self._phase_started_at = self._started_at
        self._timer = self._timer_factory(self.timeout_s, self._watchdog_fired)
        self._timer.daemon = True
        try:
            self._timer.start()
            self._timer_started = True
        except BaseException:
            self._timer.cancel()
            raise
        day, phase, elapsed, _phase_elapsed = self.snapshot()
        self.logger.info(
            "finalization started day=%s phase=%s elapsed=%.3fs",
            day, phase, elapsed,
        )
        return self

    def __exit__(self, exc_type: Any, _exc: Any, _tb: Any) -> bool:
        self.close()
        day, phase, elapsed, _phase_elapsed = self.snapshot()
        outcome = "failed" if exc_type is not None else "complete"
        self.logger.info(
            "finalization ended day=%s phase=%s elapsed=%.3fs outcome=%s",
            day, phase, elapsed, outcome,
        )
        return False

    def close(self) -> None:
        """Cancel and join the watchdog, synchronizing with an in-flight dump."""
        with self._guard:
            if self._closed:
                return
            self._closed = True
            timer = self._timer
            timer_started = self._timer_started
        if timer is not None:
            timer.cancel()
            if timer_started and threading.current_thread() is not timer:
                timer.join()

    def set_day(self, day: Any) -> None:
        with self._guard:
            if not self._closed:
                self._day = _safe_day(day)

    @contextmanager
    def operation(self, phase: str, *, day: Any = None) -> Iterator[None]:
        """Record the active phase while an operation is running."""
        safe_phase = _safe_phase(phase)
        with self._guard:
            previous_phase = self._phase
            previous_phase_started_at = self._phase_started_at
            if day is not None:
                self._day = _safe_day(day)
            self._phase = safe_phase
            self._phase_started_at = time.monotonic()
            active_day = self._day
        # Historical batch checks are frequent; retain their tracker state
        # without flooding the normal log for every previously posted day.
        log_operation = self.logger.debug if safe_phase in {"batch list", "batch check"} else self.logger.info
        log_operation("finalization operation started day=%s phase=%s", active_day, safe_phase)
        operation_started = time.monotonic()
        try:
            yield
        finally:
            operation_elapsed = max(0.0, time.monotonic() - operation_started)
            with self._guard:
                self._phase = previous_phase
                self._phase_started_at = previous_phase_started_at
                active_day = self._day
            log_operation(
                "finalization operation ended day=%s phase=%s elapsed=%.3fs",
                active_day, safe_phase, operation_elapsed,
            )

    def snapshot(self) -> tuple[str, str, float, float]:
        """Return sanitized day/phase and total/phase elapsed seconds."""
        with self._guard:
            now = time.monotonic()
            started_at = self._started_at
            phase_started_at = self._phase_started_at
            day = self._day
            phase = self._phase
            elapsed = max(0.0, now - started_at) if started_at is not None else 0.0
            phase_elapsed = max(0.0, now - phase_started_at) if phase_started_at is not None else 0.0
            return day, phase, elapsed, phase_elapsed

    def _watchdog_fired(self) -> None:
        # Hold only the tracker guard while dumping. close() waits for this
        # callback before finalization returns, preventing late stale dumps.
        # The finalization RLock is never acquired by the watchdog.
        with self._guard:
            if self._closed or self._fired:
                return
            self._fired = True
            now = time.monotonic()
            elapsed = max(0.0, now - self._started_at) if self._started_at is not None else 0.0
            phase_elapsed = max(0.0, now - self._phase_started_at) if self._phase_started_at is not None else 0.0
            day = self._day
            phase = self._phase
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        f"\nFinalization stall detected at {datetime.now(timezone.utc).isoformat()} "
                        f"day={day} phase={phase} elapsed={elapsed:.3f}s "
                        f"phase_elapsed={phase_elapsed:.3f}s\n"
                    )
                    stream.flush()
                    self._dump_traceback(file=stream, all_threads=True)
                    stream.flush()
            except Exception:
                self.logger.exception(
                    "failed to write finalization stall diagnostics day=%s phase=%s elapsed=%.3fs",
                    day, phase, elapsed,
                )
            self.logger.warning(
                "finalization stall detected day=%s phase=%s elapsed=%.3fs diagnostics=%s",
                day, phase, elapsed, self.log_path,
            )


def log_runtime_info(logger: logging.Logger, project_dir: os.PathLike[str] | str) -> None:
    """Log safe Python/runtime versions and the local Git HEAD for diagnosis.

    This deliberately omits command-line arguments, environment variables,
    credentials, and subprocess error output.
    """
    distributions = (
        ("slack-bolt", "slack-bolt"),
        ("slack-sdk", "slack-sdk"),
        ("gspread", "gspread"),
        ("google-auth", "google-auth"),
        ("requests", "requests"),
    )
    versions = []
    for label, distribution in distributions:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = "not-installed"
        except Exception:
            version = "unknown"
        versions.append(f"{label}={version}")

    head = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.fspath(project_dir),
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        candidate = result.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate):
            head = candidate
    except Exception:
        pass

    logger.info(
        "runtime info executable=%s python=%s distributions=%s git_head=%s",
        sys.executable,
        platform.python_version(),
        ",".join(versions),
        head,
    )

