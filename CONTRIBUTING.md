# Contributing

Please open an issue describing the problem or proposed change before a large
refactor. Include Python version, operating system, and a minimal example with
fake user IDs and scores. Never attach credentials, live event payloads, or
private spreadsheets.

Install `requirements-dev.txt` in a virtual environment and run
`python -m pytest -q`. Keep tests offline with fake Slack clients and Sheets
stores. Tests should verify behavior, especially edits, scoring across the
medal cut-over, tied results, and preservation of existing ledger rows.

The entry point is `score-bot.py`. Parsing, game registration, date bucketing,
scoring, Sheets storage, and finalization live in their respective modules.
`insights.py`, `monthly_summary.py`, and `nl_query.py` render and query results.
Maintenance utilities dynamically load the entry point, so preserve its
re-exported interfaces unless updating those callers too.

Submit focused pull requests with a description of the resulting behavior and
validation. For manual integration testing, create a separate Slack app,
channel, and spreadsheet. Do not run force/rebuild utilities against your
group's production sheet without a backup.

By submitting a contribution, you agree to license it under this project's
MIT license. Report security issues using [SECURITY.md](SECURITY.md).
