#!/usr/bin/env python3
"""
Rebuild Totals from DailyResults.summary_json (ledger).

Usage:
  python rebuild_totals.py
"""

from pathlib import Path


def _load_bot_module():
    import importlib.util

    base = Path(__file__).resolve().parent
    candidates = [base / "score-bot_refactor.py", base / "score_bot_refactor.py", base / "score-bot.py", base / "score_bot.py"]

    for p in candidates:
        if p.exists():
            spec = importlib.util.spec_from_file_location("score_bot_loaded", str(p))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[attr-defined]
                return mod

    raise FileNotFoundError("Could not find score-bot.py or score_bot.py next to rebuild_totals.py")


def main():
    bot = _load_bot_module()
    bot.store.rebuild_totals_from_daily()
    print("Rebuilt Totals from DailyResults.summary_json.")


if __name__ == "__main__":
    main()
