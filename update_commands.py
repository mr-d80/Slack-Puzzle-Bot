#!/usr/bin/env python3
"""update_commands.py

Slack-triggered game registry commands for the JustWordle / Slack Puzzle Tracker.

Adds a `/jw-update` slash command that can:
- Add a new game to the dynamic game registry.
- Reconcile (replay) a day's Events through the current parser to pick up
  scores for newly-added games, then repost the recap.

Command usage
- /jw-update new game: GameName, time
- /jw-update new game: GameName, guesses
- /jw-update new game: GameName, points
- /jw-update reconcile              -> reconcile today from Events + Slack history
- /jw-update reconcile 2026-03-18   -> reconcile a specific day from Events + Slack history

Integration (in score-bot.py)
  from update_commands import register_update_commands
  register_update_commands(app, sys.modules[__name__])

Access
- Commands are limited to SCORE_CHANNEL_ID.
- If ADMIN_USER_IDS is configured, only those Slack users may run them. An
  empty allowlist preserves score-channel access for the existing group.

Notes
- Uses a background thread so Slack's 3s ack window isn't missed.
- Assumes the passed bot_module exposes:
    - store (SheetStore with .add_game_to_registry() and .load_game_registry())
    - game_registry (GameRegistryCache with .rebuild())
    - SCORE_DAY_TZ (optional tzinfo)
    - finalize_day(day, channel, post=..., force=...)
"""

import re
import logging
import threading
from datetime import datetime, date
from typing import Any

from command_access import command_access_error

logger = logging.getLogger(__name__)

_NEW_GAME_RE = re.compile(
    r"new\s+game\s*:\s*(.+?)\s*,\s*(time|guesses|points)\s*$",
    re.IGNORECASE,
)

_RECONCILE_RE = re.compile(
    r"reconcile(?:\s+(\d{4}-\d{2}-\d{2}))?\s*$",
    re.IGNORECASE,
)


def register_update_commands(app: Any, bot_module: Any) -> None:
    @app.command("/jw-update")
    def _jw_update(ack, respond, body, client, logger):  # type: ignore[no-redef]
        ack()

        denial = command_access_error(body, bot_module)
        if denial:
            respond(denial)
            return

        user_id = (body.get("user_id") or "").strip()
        channel = (body.get("channel_id") or "").strip()
        text = (body.get("text") or "").strip()

        # --- "new game" subcommand ---
        m = _NEW_GAME_RE.match(text)
        if m:
            _handle_new_game(m, user_id, respond, bot_module, logger)
            return

        # --- "reconcile" subcommand ---
        m = _RECONCILE_RE.match(text)
        if m:
            _handle_reconcile(m, channel, respond, bot_module, logger)
            return

        # --- unknown ---
        respond(
            "Usage:\n"
            "- `/jw-update new game: GameName, time`, `guesses`, or `points`\n"
            "- `/jw-update reconcile` (today) or `/jw-update reconcile 2026-03-18`"
        )


def _handle_new_game(m, user_id, respond, bot_module, logger):
    game_name = m.group(1).strip()
    metric_type = m.group(2).strip().lower()

    if not game_name:
        respond("Game name cannot be empty.")
        return

    respond(f"Adding *{game_name}* ({metric_type}) to the game registry...")

    def worker() -> None:
        try:
            store = getattr(bot_module, "store", None)
            registry = getattr(bot_module, "game_registry", None)

            if store is None or registry is None:
                logger.error("/jw-update new game unavailable: store or game_registry missing")
                respond("The update could not be completed. Contact an administrator if it continues.")
                return

            tz = getattr(bot_module, "SCORE_DAY_TZ", None) or getattr(bot_module, "TZ", None)
            today = datetime.now(tz=tz).strftime("%Y-%m-%d") if tz else datetime.now().strftime("%Y-%m-%d")

            store.add_game_to_registry(
                game_name=game_name,
                metric_type=metric_type,
                added_by=user_id,
                effective_date=today,
            )

            registry.rebuild(store)

            msg = (
                f"Added *{game_name}* ({metric_type}) to the game registry, "
                f"effective {today}.\n"
                f"The bot will now parse and track scores for this game.\n"
            )
            native_game = game_name.casefold().replace("×", "x") in ("wordle", "4x6", "4x3", "maptap")
            if native_game:
                msg += "Players should paste the complete native daily result text."
                msg += "\nThis implementation is experimental and remains untested with real Slack submissions."
            else:
                msg += "Players should post scores in the format: "
                if metric_type == "time":
                    msg += f"`{game_name} #123 | M:SS`"
                elif metric_type == "guesses":
                    msg += f"`{game_name} #123 | N guesses`"
                else:
                    msg += f"`{game_name} #123 | N points`"

            msg += (
                "\n\nTo pick up scores already posted today, run: "
                "`/jw-update reconcile`"
            )

            respond(msg)

        except ValueError as e:
            logger.info("/jw-update new game rejected: %s", e)
            respond("Could not add that game. Check the name and try again.")
        except Exception:
            logger.exception("/jw-update new game failed")
            respond("The update could not be completed. Contact an administrator if it continues.")

    threading.Thread(target=worker, daemon=True).start()


def _handle_reconcile(m, channel, respond, bot_module, logger):
    from reconcile_day import replay_events_for_day, sync_slack_history_for_day

    day_arg = (m.group(1) or "").strip()

    if day_arg:
        day = day_arg
    else:
        tz = getattr(bot_module, "SCORE_DAY_TZ", None) or getattr(bot_module, "TZ", None)
        if tz:
            day = datetime.now(tz).strftime("%Y-%m-%d")
        else:
            day = datetime.utcnow().strftime("%Y-%m-%d")

    respond(
        f"Reconciling scores for *{day}* from Events and current Slack history "
        "(re-parsing with the current game registry)..."
    )

    def worker() -> None:
        try:
            rebuilt = replay_events_for_day(bot_module, day)
            rebuilt += sync_slack_history_for_day(bot_module, day)
            respond(f"Rebuilt/updated *{rebuilt}* score rows for {day}.")

            if not channel:
                respond("No channel available to repost into. Use `/jw-recap` to post.")
                return

            store = bot_module.store
            already_posted = store.day_already_posted(day)

            if already_posted:
                respond(
                    f"{day} was already finalized. Use `/jw-recap {day} --force` "
                    f"to re-finalize with the updated scores and repost."
                )
            else:
                respond(
                    f"Scores are updated. Use `/jw-recap {day}` to finalize and post."
                )

        except Exception:
            logger.exception("/jw-update reconcile failed")
            respond("Reconciliation could not be completed. Contact an administrator if it continues.")

    threading.Thread(target=worker, daemon=True).start()
