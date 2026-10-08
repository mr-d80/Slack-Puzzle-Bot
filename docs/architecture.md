# Architecture and data contracts

Slack-Puzzle-Bot runs one process for one score channel and spreadsheet.
The storage worksheets are the durable records; in-memory caches and locks
only coordinate that process.

## Score ingestion

Live messages, Events replay, and Slack history scans use the same score-day
rule in `day_utils.py`. A date embedded in a supported share is authoritative.
Undated shares start on the message's score day and move forward when their
puzzle number belongs after an already-finalized day. Recovery runs reuse a
`ScoreDayResolver` snapshot so day resolution does not read the whole Scores
sheet for every message. Read failures abort recovery rather than silently
discarding candidates.

`score_identity.py` defines a score identity as:

```text
(canonical score day, canonical Slack user, canonical game, puzzle number)
```

Single and bulk upserts apply the latest incoming value to that identity.
When an identity already has duplicate legacy rows, the last row survives and
older matching rows are cleared; unrelated identities are preserved. A move
merges source and destination identities. A later Slack message takes
precedence; equal Slack timestamps favor the destination, because replaying
an old event can refresh `updated_at` without proving the text is a newer edit.
Scoring and analytics also deduplicate rows defensively before result analysis.

This prevents new duplicate awards; it does not automatically repair awards
already recorded in DailyResults. Recalculate an affected day deliberately
with the existing force command after checking the source scores.

## Ranking

`scoring.rank_finishers` owns placement and tiebreak rules. Scoring, query
placements, and recap race identities consume that ranking rather than
implementing separate winner calculations.

- Time and guesses rank lower values first; provider points rank higher first.
- A tiebreak splits equal results only when every tied player has one.
- True ties share a place and skip occupied places: `1, 1, 3`.
- Failed submissions count toward participation but receive no result rank.
- Recap margins describe the main metric, not an invented tiebreak unit.

Award calculation still selects the historical trophy/necktie rules or the
3/2/1 medal rules according to the score day and `MEDAL_SCORING_START`.

## Ledgers and derived views

| Worksheet | Durable meaning | Consumers |
| --- | --- | --- |
| Scores | Parsed submissions and corrections | Daily scoring and score statistics |
| Events | Original event payloads | Recovery/replay |
| DailyResults | One claimed daily summary and its awards | Totals, weekly/monthly summaries, award queries |
| Totals | Derived aggregate of DailyResults | Standings |
| MonthlyResults | Monthly facts and rendered text | Stored monthly summaries and title queries |

The monthly payload is built and decoded through `ledger_contracts.py`:

```text
schema_version: 1
month: YYYY-MM
facts:
  champion: ...
  standings: ...
standings_text: ...
recap_text: ...
```

The decoder also accepts historical flat facts. The latest nonempty row for
each month is authoritative; superseded champions do not count as titles.
Readers normalize legacy Slack mention wrappers to canonical player IDs.
Query tests should round-trip
the writer's payload, rather than relying only on hand-written reader fixtures.

Structured JSON uses `serialize_json_for_cell`, not text truncation. It is
serialized compactly and checked against a conservative cell budget before
writing. Oversized ledger payloads fail explicitly without claiming a day or
month. Oversized Events retain their full payload in the local event spool.
An update refuses an existing malformed/non-object summary instead of
overwriting its facts with an empty object. Plain display text may still use
`truncate_for_cell`.

## Finalization and delivery

`finalization.finalize_day` owns the daily transition from Scores to
DailyResults and Totals. Its process lock covers the shared operation,
including direct command calls. The storage append returns whether it won
the daily claim. A losing caller stops before rebuilding Totals or sending
messages; recap commands also honor that result. Explicit force/repost
requests retain their separate meanings.

Replay, ledger finalization, and Slack delivery are distinct controls.
`post=False` suppresses delivery while allowing requested finalization.
`post_scores` and `post_recap` control individual messages for that call and
do not mutate process-wide delivery settings. A recap is threaded only when
a standings message was actually posted.

The claim precedes Slack delivery. A Slack failure after a successful claim
therefore needs an explicit repost; automatic retry does not send a second
summary. This is process-level coordination, not distributed locking or a
transaction across Google Sheets and Slack.

## Verification

Tests use fake worksheets and clients. Cross-module regressions cover
live/replay day agreement, destination conflict merging, ranking agreement,
monthly writer/reader compatibility, oversized/corrupt JSON, losing claims,
concurrent finalization, and recovery flag effects. Live compatibility of
Wordle, 4×6, 4×3, and MapTap remains untested, as stated in the README.
