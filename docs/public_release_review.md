# Public release review

Reviewed on **2026-10-08** for independently self-hosted friend groups.
The public source is prepared for a new repository, `mr-d80/Slack-Puzzle-Bot`,
with a fresh initial commit. The original repository stays private and its Git
history is not copied. Each group creates its own Slack app.

## Findings resolved

| Finding | Change |
| --- | --- |
| Events from unrelated channels were saved before filtering | Require the configured score channel before claiming or storing events; ignore bots and unsupported subtypes |
| Slash commands could post a group's ledger into another channel | Gate both commands on exact score-channel ID before any work |
| Any workspace user could run management commands | Optional `ADMIN_USER_IDS` restricts both commands; blank preserves access for members invoking them in the score channel |
| Missing channel configuration collected messages from every channel | Runtime validation requires the channel; message handling and event replay fail closed |
| First startup assumed pre-existing Sheets tabs | Get or create every required worksheet, preserve existing rows, widen narrow empty tabs before writing headers |
| Game registry names could be interpreted as spreadsheet formulas | Use `RAW` writes for registry data, matching existing score/query writes |
| `.env` overrode deployment configuration | Load module-local dotenv files with `override=False` and honor explicit dotenv disabling |
| Runtime imports demanded production credentials | Configuration modules import without credentials; the bot validates before initializing services |
| Tests could load a developer's real AI settings | Pytest disables local dotenv loading and clears API keys before imports |
| Google authorization requested unnecessary Drive access | Use the Sheets scope only; open by spreadsheet ID |
| Dependencies had published advisories | Pin patched python-dotenv 1.2.2, Requests 2.33.0, and urllib3 2.8.0 |
| No reproducible public setup or license | Add README, `.env.example`, Slack manifest, MIT license, and contribution/security docs |
| Machine-local settings were tracked; logs/spools could be added | Untrack the local Claude settings file while retaining it locally; extend ignore rules |

New regression coverage exercises worksheet bootstrap/data preservation,
literal writes, configuration validation/precedence, message privacy, channel
and user authorization, and local game effective dates. Existing scoring and
reporting behavior remains covered by the original suite.

## Verification

- Original baseline: **278 tests and 16 subtests passed**.
- Public-preparation run before experimental games: **303 tests and 16 subtests
  passed** on Windows / Python 3.12.
- Integrated run including experimental games: **354 tests and 21 subtests
  passed**. Coverage includes native sample parsing, failed and signed/zero
  results, points rankings/records/queries, participation, dated live-handler
  ingestion with fake services, and offline replay/history recovery.
- Created a clean source export with no `.env`, credentials, logs, local agent
  settings, or Git history; installed only `requirements-dev.txt` in a fresh
  virtual environment and ran the exported suite: **354 tests and 21 subtests
  passed**. The fresh environment also passes `pip check`.
- Requirements audit, including resolved transitive dependencies:
  **no known vulnerabilities found** after patching. This is a point-in-time
  check, not proof that dependencies are vulnerability-free.
- Independent code review found no remaining correctness/security blockers.
- All Python source files parse under Python 3.10 grammar. The GitHub Actions
  Linux/Windows matrix for Python 3.10, 3.12, and 3.14 awaits its first run.
- Scanned **126 historical Git blobs** for common Slack/API token, Google
  credential, and private-key patterns, with **no matches**. Credential values
  were not printed, and local `.env`/service-account contents were not read.

The history scan is deliberately narrow: it does not establish that every
historical file is safe to publish. Older history retains the previously
tracked machine-specific settings and may include personal fixture IDs or
operational details. Review that history before making it public, or publish
a fresh source export without Git history, as chosen for Slack-Puzzle-Bot.
Current fixtures use fake IDs/names.

## Operating limits

- Experimental Wordle, 4×6, 4×3, and MapTap support is opt-in and remains
  untested with real Slack submissions. Automated sample tests do not establish
  live compatibility. Points totals include provider bonuses; league awards
  remain separate. See the README for activation and supported daily formats.
- One process, one score channel, and one spreadsheet per group. There is no
  multi-workspace OAuth installation flow or distributed locking.
- No scheduler: daily/monthly finalization depends on incoming score traffic.
- Participant count follows yesterday's participants after the first day;
  early finalization and late score changes may need a forced recap.
- Deleted Slack messages do not remove score rows. History recovery depends
  on API permissions/retention, looks back two days for thread parents, and
  includes two days of later posts for dated shares.
- Events include ordinary chat from the configured score channel. Sheets,
  logs, and failure spools have no automatic retention cleanup.
- AI is optional. Supplying an API key can send question/user/recap data to
  the provider; recap rewriting and question translation have separate triggers.
- Force/reconcile/rebuild and some preview utilities can write Sheets even
  when they do not post to Slack. Back up the ledger before using them.
- Live installation against a new Slack workspace and Google spreadsheet has
  not been exercised in this review. Follow the README in a separate test
  workspace/sheet before promoting a public release.

Before publication, confirm the new CI matrix passes and enable GitHub private
vulnerability reporting. No production credentials need to accompany a source
download.
