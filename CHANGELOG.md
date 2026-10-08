# Changelog

## Unreleased

### Repository review fixes
- Share score-day resolution across live ingestion, Events replay, and history
  recovery. Recovery uses one run-local score snapshot and reports read errors.
- Merge duplicate score identities consistently during upserts and day moves;
  filter legacy duplicate rows before scoring or statistics so a submission
  cannot earn duplicate medals.
- Separate recovery replay, ledger finalization, and Slack posting controls.
  Scan requires `--finalize` for ledger changes and honors `--no-post`;
  CLI help no longer initializes Slack/Sheets services.
- Use the scorer's tiebreak rankings in placements and recap facts. Read
  monthly titles through the same versioned payload contract as the writer,
  with support for historical flat records.
- Store complete, size-checked JSON; spool oversized Events and reject
  oversized ledger writes. Preserve existing malformed summaries when a
  timestamp patch cannot decode them.
- Serialize direct daily finalizers, honor lost storage claims, and stop
  concurrent recap commands before duplicate delivery. Recap-only and
  no-recap options apply per call without changing global delivery settings.

### Experimental game support
- Added opt-in native daily-share parsing for Wordle, 4×6, 4×3, and MapTap.
  These implementations remain untested with real Slack submissions; automated
  tests use samples and fake clients/stores.
- Added higher-is-better points metrics throughout rankings, recaps, monthly
  records, and history queries. Provider points remain separate from league
  medal points. Explicit failed results receive no awards or best-score records.
- Dated shares retain their puzzle day during ingestion, replay, and history
  recovery; history scans include up to two days of later posts.

### Public release preparation
- Added the MIT license, setup guide, sample environment, Slack app manifest,
  contributor/security guidance, and automated test/dependency checks.
- New installations create all required worksheet tabs in a blank spreadsheet.
  Google Sheets access no longer requests the Google Drive scope, and game
  registry values are written literally rather than interpreted as formulas.
- Messages from other channels are rejected before event storage. Both slash
  commands require the configured score channel and honor optional
  `ADMIN_USER_IDS`; blank channel configuration fails closed.
- Deployment environment variables take precedence over `.env`, runtime
  configuration errors are reported before service initialization, and tests
  disable local dotenv/AI credentials. New games use the local score day.
- Patched dependency advisories by upgrading python-dotenv, Requests, and
  urllib3. Ignore local credentials, logs, failure spools, and agent settings;
  removed the machine-specific Claude settings file from tracking.

### Added
- Month-end summaries. Once a month is finished, the score channel gets a monthly wrap: final standings, per-game champions, month records, and participation, followed in-thread by AI commentary and a per-player breakdown for everyone who played.
- A month counts as finished as soon as its **last day is finalized**, not when the clock rolls over. A posted day never re-finalizes, so nothing more can land in the month; since `finalize_day` posts a day the moment every expected player completes, the wrap normally goes out on the evening of the last day instead of waiting for the first score of the new month. The wall-clock check stays as a backstop for a final day nobody played, or a restart that missed the fast path.
- New `monthly_summary.py` aggregates `DailyResults.summary_json` — the same source of truth as Totals and the weekly summaries — into a month fact bundle (standings with month-over-month deltas, champion and runner-up, per-game champions, best single result per game, biggest mover, skunk of the month, tightest race, biggest blowout, perfect attendance). It can also be run directly (`--month`, `--post`, `--force`, `--no-ai`) to inspect or backfill a month.
- New `MonthlyResults` worksheet is the posted-month ledger. The row is claimed before anything is sent to Slack, so the rollover check — which re-runs on ordinary score traffic — can never post a month twice, even if a Slack call fails mid-post.
- `insights.py` gained a month-wrap prompt profile that reuses the daily recap voice (`AI_RECAP_STYLE`) at a longer format, with required callouts for the champion and the skunk of the month. If the model drops one, it gets a single revision pass; if the revision still drops it, the deterministic template is posted instead, so the champion is never missing from the wrap.
- Feature flags: `POST_MONTHLY_SUMMARY`, `MONTHLY_SUMMARY_IN_THREAD`, `POST_MONTHLY_PLAYER_BREAKDOWNS` (all default on), `OPENAI_MAX_TOKENS_MONTHLY` (default 700, since the daily 220-token cap truncates a month wrap mid-sentence), and `MONTHLY_SUMMARY_MAX_AGE_DAYS` (default 14).
- `MONTHLY_SUMMARY_MAX_AGE_DAYS` guards the first deploy. `MonthlyResults` starts empty, so without a limit the first rollover would treat every month already in `DailyResults` as unposted and dump the whole back catalogue into the channel at once. Months older than the limit are skipped and logged rather than silently claimed, so `monthly_summary.py --month YYYY-MM --post` can still post them deliberately. Set to `0` to disable.
- A bare `pinpoint fail` message (a player conceding without a shareable score card) is now scored as a worst-case Pinpoint DNF instead of being dropped. An optional `#<id>` pins the concession to a specific puzzle; otherwise the day's primary Pinpoint puzzle is used, and if no puzzle id can be resolved the message is left unscored rather than filed under a guess. Applies to live posts, edits, history scans, and reconcile replays.

### Changed
- **Scoring changes on 2026-10-01.** From that score day on a game awards a podium instead of a single trophy: 1st, 2nd and 3rd place are worth **3, 2 and 1 points**, shown as `:first_place_medal:`, `:second_place_medal:` and `:third_place_medal:`. Ties follow Olympic ranking: players with the same result share a place and the places they occupy are skipped, so two players tied for first each get 3 points and the next player is third with 1. The necktie is retired, since a tie now just shares the medal. The cut-over is by *score day*, not by when a day is finalized, so a day that closes after midnight keeps the rules it was played under. Days before it keep the trophy/necktie rules and emoji exactly, including September's month wrap. `MEDAL_SCORING_START` (default `2026-10-01`) moves the date. It is read from the environment on every call, and it assumes the cut-over falls on a month boundary, since standings reset monthly (a month that mixes both scorings logs a warning).
- The daily post lists each game's podium with every medalist's result, then each player's points for the day, then the month's running standings ranked by points. Standings, the MVP and the month champion rank by points, then break ties on the gold, silver and bronze count-back; players level on all of those share a place.
- The existing tiebreak (flawless, backtracks, redraws) now applies to *any* tied place rather than only first, so a flawless run takes silver over a clumsy one on the same time. As before it applies only when every tied player has tiebreak data; otherwise the tie stands and they share the place.
- A conceded Pinpoint (DNF) can't take a medal, in keeping with a DNF never being able to win. Without this a DNF would get bronze whenever fewer than three players solved it.
- The month wrap works in points: champion and margin (a champion level on points but ahead on the count-back is reported as "level on points"), runner-up, best single day, and game champions (most first places in a game; a shared first counts as a gold for each). The AI wrap's photo-finish and runaway cut-offs, tuned in trophies, are scaled by what a first place is worth. October's wrap has no month-over-month comparison, because September counted trophies and a delta between the two would compare unlike things; November's compares points with points. The weekly summary does the same, so a week straddling the cut-over shows both kinds of award and no delta.
- `Totals` gains `gold`, `silver`, `bronze` and `points` columns beside the legacy `trophies` and `ties`, added to the existing header on the next restart or rebuild (the sheet is widened first if it was created narrower than the header, since the Sheets API rejects a write past the grid and this runs at startup). The legacy columns keep their meaning and cover only days before the cut-over. `DailyResults` rows are never rewritten: `awards_by_user` is `{wins, ties}` for a legacy day and `{gold, silver, bronze, points}` for a medal day, and each game's `winners_by_game` entry gains a `podium` list while keeping `winners`, `result` and `best_value` describing first place for the existing readers.
- Natural-language answers read both shapes. A window of medal days answers in medals and points, a legacy window in trophies and neckties exactly as before, and a window across the cut-over shows both. "Wins" are first-place finishes (trophies, or gold medals from the cut-over); a strikeout is a day with no first place. "Who wins Zip most often" and the other wins leaderboards rank by first-place finishes, so a window across the cut-over still counts the trophy years instead of ranking by the few days of points; "who has the most points" ranks by points (and by game when one is named), and defaults to this month because points don't exist before the cut-over. "How many points do I have" routes to the awards summary, and "how are ties decided" describes both rule sets. Asking for the most trophies in a named game now filters to that game instead of ignoring it.
- The medal emoji are Slack's built-in `:first_place_medal:` / `:second_place_medal:` / `:third_place_medal:`. GitHub spells them `:1st_place_medal:` and so on, which Slack doesn't recognise and would post as literal text. They are three constants in `awards.py` if the workspace needs different ones.
- Award parsing and rendering, previously copied into `insights.py`, `monthly_summary.py`, `weekly_summaries.py`, `recap_commands.py` and `nl_query.py`, now live in a new standalone `awards.py` (`AwardTally`, `unpack_awards`, `render_tally`, the cut-over rule). `sheet_store.unpack_award_value`, whose `(wins, ties)` return could not represent medals and would have silently read medal days as zero, is removed.

### Fixed
- Scores posted as a thread reply with **"Also send to #channel"** checked are now recorded. Slack delivers those as `message` events with `subtype: "thread_broadcast"`, and all three ingestion paths treated any subtype as noise, so the score was dropped on arrival and could never be recovered: `/jw-update reconcile` calls `replay_events_for_day` and `sync_slack_history_for_day`, and both carried the identical filter, so re-running it read the broadcast back out of Events (and out of `conversations.history`) and discarded it again. A `thread_broadcast` message carries the same `user`/`ts`/`text`/`channel` as a plain post, so it is now parsed like one in `score-bot.py`, `reconcile_day.py`, and `scan_slack_day.py`. Edits to a broadcast post are handled too — the inner message of a `message_changed` event keeps the `thread_broadcast` subtype and was being dropped by the same check. `file_share`, `message_deleted`, and the join/leave subtypes stay filtered.
- Threaded replies now actually thread. `chat_postMessage` returns a slack_sdk `SlackResponse`, which supports `.get()` but is **not** a dict subclass, so the `isinstance(resp, dict)` guard used at every call site always fell through to `""`. With no parent `ts` there was no `thread_ts` to reply under, so daily recaps, monthly commentary, and monthly per-player breakdowns all posted as separate top-level messages regardless of `DAILY_RECAP_IN_THREAD` / `MONTHLY_SUMMARY_IN_THREAD`, and the Slack ts was never recorded in the ledger. The same guard made `weekly_summaries.py --dm` resolve every DM channel to `None` and silently send nothing. Extracted into `slack_safe.message_ts` / `slack_safe.dm_channel_id`.
- Pinpoint no longer counts toward the skunk metric (`SKUNK_EXCLUDE_GAMES`, default `Pinpoint`). The skunk is the largest last-place margin across games, but Pinpoint's margin is in guesses plus a +100 DNF penalty while every other game's is in seconds, so it was being ranked against a quantity it isn't comparable to. Applied both at detection and when the monthly rollup reads the ledger, so days finalized before this change no longer contribute a Pinpoint skunk to a month total.
- Trailing whitespace is stripped from AI recap lines. Models end lines with the two-space markdown line break, which Slack does not use, so it survived into the posted message.
- Reconcile and history backfill no longer burst past the Sheets write-per-minute quota on busy days. `SheetStore.bulk_upsert_scores` reads the Scores sheet once and flushes all in-place edits as a single `batch_update` plus all new rows as a single `append_rows`; `bulk_log_events` appends synthetic history events in one write. `reconcile_day.py` and `scan_slack_day.py` now accumulate and flush in bulk instead of writing per candidate.

## v4.6.2 (2026-04-20)

### Fixed
- The score day now defaults to `America/Vancouver` instead of `Etc/GMT+12`, so early-morning Pacific posts are bucketed into the local calendar day instead of rolling back to the previous score day.
- `scan_slack_day.py` now caps the `latest` parameter sent to Slack's `conversations_history` at the current time. Previously, scanning the in-progress GMT-12 day passed a future `latest` (noon UTC tomorrow), which caused Slack to return zero messages and the backfill to silently miss all of today's scores.
- `scan_slack_day.py` now fetches top-level messages with a 2-day lookback beyond the window start. Scores posted as replies to a thread whose parent was created the previous day were silently missed because the parent never appeared in the top-level fetch and its replies were therefore never fetched. `_extract_candidates` still gates on `day_key_from_ts` so the wider lookback introduces no false hits.
- `scan_slack_day.py`'s `_load_bot_module()` now registers the module in `sys.modules` before executing it (matching `reconcile_day.py`). Without this, `game_registry.rebuild()` inside `score-bot.py` could operate on a different singleton instance than `parser.parse_score`, leaving the compiled regexes in their never-matches default state and causing all parse attempts to fail.
- `scan_slack_day.py` now hands off to the shared reconcile flow after logging synthetic history events, so backfills and manual reconciles use the same replay/finalize path.
- The OpenAI auth diagnostic in `insights.py` now logs at `INFO` instead of `WARNING` on successful requests.

## v4.6.1 (2026-04-08)

### Fixed
- Daily recap AI prompts now explicitly require skunk callouts when a player gets buried in last place, and the bot makes one AI-only revision pass if the first draft misses that fact.
- Natural-language query handling now responds only to explicit `bot:` prefixes instead of broad question-mark heuristics, preventing accidental bot replies to ordinary conversation.

## v4.6.0 (2026-03-22)

### Refactored
- Split the 2160-line `score-bot.py` monolith into focused, single-responsibility modules:
  - `config.py` — environment variables, logging setup, and constants
  - `game_registry.py` — dynamic game registry with compiled regex patterns
  - `parser.py` — score parsing (ParsedScore, parse_score, tiebreak detection)
  - `day_utils.py` — day-key arithmetic, puzzle-id filtering, expected-player logic
  - `sheet_store.py` — Google Sheets data layer (SheetStore class)
  - `scoring.py` — winner computation, award tracking, summary formatting
  - `finalization.py` — finalize_day / finalize_due_days orchestration
- `score-bot.py` is now a thin entry point (~230 lines) that wires up the Slack app and re-exports all symbols for backward compatibility with `reconcile_day.py`, `scan_slack_day.py`, and slash-command modules.
- Removed `globals().get("store")` fallback pattern from `expected_players_for_day` and related functions; callers now pass `store_obj` explicitly.
- Extracted shared helpers to eliminate copy-paste: `move_future_puzzle_scores`, `extract_awards_from_summary`, `unpack_award_value`, `_find_daily_row`, `_aggregate_awards_from_rows`.

### Removed
- Deleted legacy one-off scripts: `bot-test.py`, `channel-grabber.py`, `api_key_check.py`, `api_key_check2.py` (all superseded by `ai_smoketest.py` or no longer needed).

## v3.4.0 (2026-02-07)

### Fixed
- NL query user parsing: avoid crashes from uninitialized `user_raw`-style logic by canonicalizing the `user` field safely.
- Pinpoint formatting: stats now respect `metric_type` (guesses are shown as guesses, not `0:01`).

### Improved
- Added cross-game intents:
  - `personal_bests_by_game` for “my best scores (in any game)”.
  - `global_bests_by_game` for “all-time fastest scores for each game”.
  - `game_record` for “fastest time/best score ever for <game>”.
  - `distribution` for basic distribution summaries.
- Better date presets: `last_week` (Mon–Sun) and `last_month` (calendar month).
- Better unprefixed NL trigger heuristic in `score-bot.py` for cross-game questions.
