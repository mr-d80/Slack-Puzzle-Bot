# Slack Puzzle Bot

A self-hosted Slack bot for a small group of friends who share daily puzzle
scores. Post a puzzle's share text in one Slack channel; the bot records it in
Google Sheets, compares results, and posts standings and recaps.

Built-in games are **Tango, Zip, Mini Sudoku, Queens, Crossclimb, and Pinpoint**.
Optional support is available for **Wordle, 4×6, 4×3, and MapTap**. Additional
games can use a time, guesses, or points share format. This project does not
supply the puzzles or connect to their providers.

Each group runs its own copy with its own Slack app and spreadsheet. You need
Python **3.10 or newer**, permission to install a Slack app, a Google Cloud
service account, and a computer or server that stays running. AI is optional but recommended for colour commentary in the recaps and full responses to queries.

## Set up your group

### 1. Download and install

Clone this repository, or download and extract the ZIP from GitHub's **Code**
menu. Run commands from the extracted project directory.

```sh
git clone https://github.com/mr-d80/Slack-Puzzle-Bot.git
cd Slack-Puzzle-Bot
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```sh
# macOS / Linux
source .venv/bin/activate
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` in place of
`python` in the following commands. On systems that use `python3`, use that
command when creating the environment.

```sh
python -m pip install -r requirements.txt
```

### 2. Create the Slack app

At [Slack's app dashboard](https://api.slack.com/apps), create an app **from an
app manifest**, choose your workspace, and paste [slack-app-manifest.json](slack-app-manifest.json).
The manifest enables Socket Mode, both slash commands, and message events for
public and private channels. Its permissions let the bot read channel history,
post messages, and resolve player display names.

Under **Basic Information → App-Level Tokens**, create a token with
`connections:write`. Save the `xapp-...` value as `SLACK_APP_TOKEN`.
Install the app to your workspace under **OAuth & Permissions** and save the
`xoxb-...` bot token as `SLACK_BOT_TOKEN`. Invite the bot to your group's score
channel. Copy the channel ID from the channel details into `SCORE_CHANNEL_ID`.
No public HTTP endpoint is needed. See Slack's
[manifest reference](https://docs.slack.dev/reference/app-manifest/) and
[Socket Mode guide](https://docs.slack.dev/apis/events-api/using-socket-mode/).

The default manifest does not request DM access. For the optional
`weekly_summaries.py --dm` feature, add `im:write` to Bot Token Scopes and
reinstall the app.

### 3. Create the spreadsheet

In [Google Cloud Console](https://console.cloud.google.com/), create a project,
enable **Google Sheets API**, and create a service account. Create a JSON key
for that service account and save it privately as `json/service-account.json`
in this project. Create the `json` directory if needed.

Create a **blank Google spreadsheet**, then share it with the service account's
`client_email` as **Editor**. Keep link sharing restricted. Copy the ID from
the spreadsheet URL (the portion between `/d/` and `/edit`).

The bot opens that spreadsheet by ID and creates its worksheets and headers
on first startup: `Scores`, `Events`, `DailyResults`, `MonthlyResults`,
`Totals`, `GameRegistry`, and `NLQueries`. Existing sheets and results are
preserved. It needs the Sheets API scope, not Google Drive access.

### 4. Configure and start

Copy [.env.example](.env.example) to `.env` and fill in the five required values:

| Setting | Value |
| --- | --- |
| `SLACK_BOT_TOKEN` | Your Slack bot token |
| `SLACK_APP_TOKEN` | Your Socket Mode app token |
| `SCORE_CHANNEL_ID` | Your group's channel ID |
| `SPREADSHEET_ID` | Your Google spreadsheet ID |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the private JSON key |

Set `TZ_NAME` and `SCORE_DAY_TZ_NAME` to your group's IANA timezone, such as
`Europe/London`. Relative credential paths resolve from the project directory.
Deployment environment variables override `.env` values.

Set `PLAYERS_EXPECTED` to your initial group size. After the first day, the bot
expects the number of people who posted any score yesterday. A player is
complete when they have submitted every game active for that day. If your group
plays only some of the defaults, remove unwanted rows from `GameRegistry` and
restart the bot **before starting your league**.

Set `ADMIN_USER_IDS` to comma-separated Slack member IDs to restrict both
slash commands. If blank, any member invoking a command in the score channel
can add games, reconcile, finalize, or force a recap. Commands always require
the configured score channel.

```sh
python score-bot.py
```

Keep one instance running per spreadsheet. Socket Mode receives new messages
while the process is online. The bot does not install a background service or
scheduler; use your operating system's service manager for automatic restarts.

## Use it

Post share cards or these equivalent examples, one game per message:

```text
Zip #316 | 0:13
Pinpoint #42 | 3 guesses
```

Editing a score updates its recorded value. Thread replies and replies sent
to the channel are supported. A bare `pinpoint fail` counts as a concession
when the bot can determine the puzzle number; use `pinpoint #42 fail` to make
it explicit. Deleting a Slack message does not delete its score.

From score days on/after `MEDAL_SCORING_START` (default `2026-10-01`), places
earn gold/silver/bronze and **3/2/1 points**. Equal scores share a place unless
all tied players supplied supported tiebreak data; two firsts are followed by
third. An unsolved result receives no medal. Older days retain the original
trophy/necktie rules. Avoid changing the cut-over after recording results.

| Command | What it does |
| --- | --- |
| `bot: who won Zip this month?` | Query recorded history; `bot,` and a leading `?` also work |
| `/jw-recap` | Finalize today if ready and post its recap |
| `/jw-recap 2026-10-07 --repost` | Repost a stored recap without recalculation |
| `/jw-recap 2026-10-07 --force` | Recalculate finalized results and post again |
| `/jw-update new game: Patches, time` | Register a game starting on the local score day |
| `/jw-update reconcile 2026-10-07` | Replay saved events and overlay Slack history |

Results finalize when enough players complete all active games, or after the
score day closes. Checks run on incoming score messages; midnight alone does
not trigger a post. Monthly wraps run after a day finalizes. Recap and monthly
posting switches are in `.env.example`. Early finalization can miss later
players; use a forced recap to update finalized results after correcting scores.

For scores missed while offline:

```sh
python scan_slack_day.py 2026-10-07 --dry-run
python scan_slack_day.py 2026-10-07 --finalize
python scan_slack_day.py 2026-10-07 --finalize --no-post
```

| Recovery command | Scores/Events | DailyResults/Totals | Slack delivery |
| --- | --- | --- | --- |
| `scan_slack_day.py DAY --dry-run` | Preview only | No | No |
| `scan_slack_day.py DAY` | Backfill and replay | No | No |
| `scan_slack_day.py DAY --finalize` | Backfill and replay | Finalize if ready | On successful finalization |
| `scan_slack_day.py DAY --finalize --no-post` | Backfill and replay | Finalize if ready | No |
| `reconcile_day.py DAY --no-post` | Replay and history sync | Finalize if ready | No |

`--no-reconcile` and `--no-log-events` skip event replay during a scan;
an explicit `--finalize` still requests finalization of the backfilled scores.
`--force-finalize` recalculates an existing daily ledger, while `--force-repost`
only sends standings again. An explicit `--force-finalize` takes precedence
when both are supplied. `--no-post` suppresses delivery in either case.

`--dry-run` previews score changes, but startup can still create sheets/headers.
Backfills scan thread parents from a two-day lookback and include two days of
later posts for games whose share text names the puzzle date. Posts outside
that window and replies to older threads may require manual recovery. Slack
history access and retention depend
on workspace permissions and plan; see the
[history API](https://docs.slack.dev/reference/methods/conversations.history/)
and [thread API](https://docs.slack.dev/reference/methods/conversations.replies/).
Utility commands load the bot and connect to Sheets even when only printing
output. `reconcile_day.py --no-post` still writes score/results data. Back up
your spreadsheet before forcing or rebuilding results.

## Experimental games

**Wordle, 4×6, 4×3, and MapTap remain untested with real Slack submissions.**
Automated tests cover sample parsing, storage, rankings, and queries, but these
implementations have not been validated end to end in a live group. Try them
with a separate test spreadsheet before relying on their results.

Enable only the games your group plays, using these commands in the score
channel:

```text
/jw-update new game: Wordle, guesses
/jw-update new game: 4x6, points
/jw-update new game: 4x3, points
/jw-update new game: MapTap, points
```

Each becomes required for player completion starting on the registration day.
The original six defaults remain enabled; a new group can remove unwanted
registry rows before starting its league. Names with `×` also work for 4×6 and
4×3.

| Game | Recognized daily share | Ranking |
| --- | --- | --- |
| [Wordle](https://www.nytimes.com/games/wordle/index.html) | `Wordle 1,835 4/6`, including commas and the hard-mode `*`; `X/6` is unsolved | Fewer guesses wins |
| [4×6](https://www.hankgreen.com/4x6/) | Dated share header, six emoji rows, moves ratio, and published `pts` total | More points wins; a rescue is unsolved |
| [4×3](https://4x3.fun/) | Dated share with emoji rows and published `points` total, or an explicit failure | More points wins; exhausted guesses or a wrong hub is unsolved |
| [MapTap](https://www.maptap.gg/) | Site link, puzzle date, and labeled final score | More points wins |

Post one complete native share per message. Practice, custom, beta, and archive
links are outside the supported daily formats. MapTap uses the published final
total, including the flattened date/first-round text in the supplied example;
it does not add individual round scores. Unrecognized or ambiguous formats are
ignored. Edit the message or reconcile after correcting it.

For 4×6, 4×3, and MapTap, the share's puzzle date determines the score day.
Yearless dates use the closest year to the message date. Wordle uses its puzzle
number and the message's local score day. The dated games store a calendar-day
identifier internally; use native shares consistently rather than inventing
numbered puzzle IDs for them.

Published points can include provider bonuses, including streak bonuses. The
tracker records that total without reconstructing it. These game points decide
places within the game; the league still awards **3/2/1 points** for medals.
Provider badges do not award league medals. Failed results count as submissions
but receive no awards or best-score records; completed zero-point and negative
Rule Breaker results are valid. Cross-game time-margin comparisons include only
time-based games.

Custom points games can use `GameName #123 | 900 points` after registration with
the `points` metric. This generic format uses the message's local score day.

## Optional AI and privacy

Leave `OPENAI_API_KEY` blank to use deterministic recaps and supported
rule-based questions. To enable recap rewriting, set a key and
`AI_REWRITE_ENABLED=1`. Supplying a key also allows AI translation of questions
the rules do not recognize, even with recap rewriting disabled. Provider usage
can cost money.

The spreadsheet stores Slack user IDs, raw scores, **full message event
payloads from the configured channel**, and query text/answers. Ordinary chat
in that channel can appear in `Events`; use a dedicated puzzle channel.
The bot filters other channels before saving their messages. Logs and local
failure spools may contain group data and have no automatic retention cleanup.

AI requests can include questions, mentioned user IDs, and score/recap facts.
Tell your group before enabling it. Do not publish your `.env`, service-account
keys, live spreadsheet, logs, spools, or screenshots with personal data. See
[SECURITY.md](SECURITY.md) for credentials and vulnerability reporting.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m pip_audit -r requirements.txt
```

Tests use fake stores/clients and disable local dotenv loading and AI keys;
no live Slack or Google credentials are required. GitHub Actions runs tests on
Linux and Windows. See [CONTRIBUTING.md](CONTRIBUTING.md) and the
[public-release review](docs/public_release_review.md) for scope and known limits.

Licensed under [MIT](LICENSE). This is an independent project and is not
affiliated with Slack, Google, LinkedIn, or the puzzle providers.
