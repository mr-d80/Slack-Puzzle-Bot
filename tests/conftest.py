"""Keep pytest independent of a developer's real deployment and AI account."""
import os

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "AI_REWRITE_ENABLED": "0",
    "OPENAI_API_KEY": "",
    "OPENAI_KEY": "",
    "SLACK_BOT_TOKEN": "",
    "SLACK_APP_TOKEN": "",
    "SPREADSHEET_ID": "",
    "GOOGLE_SERVICE_ACCOUNT_FILE": "",
    "SCORE_CHANNEL_ID": "",
    "ADMIN_USER_IDS": "",
    "TZ_NAME": "America/Vancouver",
    "SCORE_DAY_TZ_NAME": "America/Vancouver",
    "PLAYERS_EXPECTED": "4",
    "MEDAL_SCORING_START": "2026-10-01",
})
