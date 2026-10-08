# Security and group privacy

Run a separate Slack app and spreadsheet for each group. Restrict spreadsheet
sharing to the service account and trusted organizers. Set `ADMIN_USER_IDS`
if score-channel members should not all have access to management commands.
Run only one bot process per spreadsheet; locks and event deduplication are
local to a process, not a distributed security boundary.

Keep secrets in `.env` or deployment environment variables and service-account
keys in the ignored `json/` directory. Logs and `*.jsonl` failure spools can
contain personal group data; restrict filesystem access and choose a retention
policy. The bot has no automatic purge of Sheets events or local logs. Anyone
with spreadsheet access can see its scores, queries, and stored event bodies.

If a key or token has been exposed, revoke/rotate it at the provider first.
Deleting the file or rewriting Git history does not invalidate a credential.
Review all history before making a formerly private repository public.

For a suspected vulnerability, use the repository's GitHub **Security → Report
a vulnerability** feature when available. If it is unavailable, open an issue
asking the maintainer for a private reporting channel, without exploit details,
tokens, message payloads, or personal data. No response-time guarantee is
provided. Keep reports private until a fix or mitigation is available.
