from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class ConfigSubprocessTests(unittest.TestCase):
    """Exercise config in a copied module directory without the repo's .env."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.module_dir = Path(self.temp_dir.name)
        shutil.copy2(REPO_ROOT / "config.py", self.module_dir / "config.py")
        shutil.copy2(REPO_ROOT / "awards.py", self.module_dir / "awards.py")
        self.assertFalse((self.module_dir / ".env").exists())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_config(self, code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        process_env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "WINDIR": os.environ.get("WINDIR", ""),
            "PYTHONIOENCODING": "utf-8",
        }
        if env:
            process_env.update(env)
        bootstrap = f"import sys; sys.path.insert(0, {str(self.module_dir)!r})\n" + code
        return subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=self.module_dir,
            env=process_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_import_succeeds_without_runtime_credentials(self) -> None:
        result = self.run_config(
            "import config; "
            "print(repr(config.SPREADSHEET_ID), repr(config.GOOGLE_SA_FILE), "
            "config.PLAYERS_EXPECTED)"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("'' '' 4", result.stdout)

    def test_environment_values_take_precedence_over_dotenv(self) -> None:
        (self.module_dir / ".env").write_text(
            "SPREADSHEET_ID=dotenv-sheet\n"
            "GOOGLE_SERVICE_ACCOUNT_FILE=dotenv.json\n"
            "ADMIN_USER_IDS=dotenv-user\n",
            encoding="utf-8",
        )
        (self.module_dir / "service-account.json").write_text("{}", encoding="utf-8")
        result = self.run_config(
            "import config; "
            "print(config.SPREADSHEET_ID); print(config.GOOGLE_SA_FILE); "
            "print(sorted(config.ADMIN_USER_IDS))",
            {
                "SPREADSHEET_ID": "environment-sheet",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json",
                "ADMIN_USER_IDS": " U1, ,U2,U1 ",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("environment-sheet", result.stdout)
        self.assertIn(str((self.module_dir / "service-account.json").resolve()), result.stdout)
        self.assertIn("['U1', 'U2']", result.stdout)
        self.assertNotIn("dotenv-sheet", result.stdout)

    def test_python_dotenv_disabled_prevents_local_file_loading(self) -> None:
        (self.module_dir / ".env").write_text("SPREADSHEET_ID=dotenv-sheet\n", encoding="utf-8")
        result = self.run_config(
            "import config; print(repr(config.SPREADSHEET_ID))",
            {"PYTHON_DOTENV_DISABLED": "1"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("''", result.stdout)
        self.assertNotIn("dotenv-sheet", result.stdout)

    def test_validation_aggregates_actionable_errors_without_echoing_tokens(self) -> None:
        result = self.run_config(
            "import config\n"
            "try: config.validate_runtime_config()\n"
            "except ValueError as error: print(error)",
            {
                "SLACK_BOT_TOKEN": "xoxb-secret-test-token",
                "SLACK_APP_TOKEN": "xapp-secret-test-token",
                "SCORE_CHANNEL_ID": " ",
                "SPREADSHEET_ID": "",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "",
                "PLAYERS_EXPECTED": "0",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        for setting in ("SCORE_CHANNEL_ID", "SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE", "PLAYERS_EXPECTED"):
            self.assertIn(setting, result.stdout)
        self.assertIn("positive integer", result.stdout)
        self.assertNotIn("xoxb-secret-test-token", result.stdout)
        self.assertNotIn("xapp-secret-test-token", result.stdout)

    def test_validation_requires_existing_relative_credential_file(self) -> None:
        (self.module_dir / "missing.json").mkdir()
        result = self.run_config(
            "import config\n"
            "try: config.validate_runtime_config()\n"
            "except ValueError as error: print(error)",
            {
                "SLACK_BOT_TOKEN": "bot-token",
                "SLACK_APP_TOKEN": "app-token",
                "SCORE_CHANNEL_ID": "channel-id",
                "SPREADSHEET_ID": "sheet-id",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "missing.json",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("existing credential file", result.stdout)
        self.assertIn(str((self.module_dir / "missing.json").resolve()), result.stdout)

    def test_relative_credential_path_resolves_from_config_directory(self) -> None:
        credential_path = self.module_dir / "credentials" / "service-account.json"
        credential_path.parent.mkdir()
        credential_path.write_text("{}", encoding="utf-8")
        result = self.run_config(
            "import config; config.validate_runtime_config(); "
            "print(config.GOOGLE_SA_FILE)",
            {
                "SLACK_BOT_TOKEN": "bot-token",
                "SLACK_APP_TOKEN": "app-token",
                "SCORE_CHANNEL_ID": "channel-id",
                "SPREADSHEET_ID": "sheet-id",
                "GOOGLE_SERVICE_ACCOUNT_FILE": "credentials/service-account.json",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(credential_path.resolve()), result.stdout)

    def test_logging_handler_is_idempotent_and_base_url_is_not_logged(self) -> None:
        secret_url = "https://user:private-password@example.test/api"
        result = self.run_config(
            "import importlib, logging, config; "
            "importlib.reload(config); "
            "print(sum(bool(getattr(h, '_slack_puzzle_tracker_monthly_handler', False)) "
            "for h in logging.getLogger().handlers))",
            {"OPENAI_BASE_URL": secret_url},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1", result.stdout.splitlines()[-1])
        self.assertNotIn(secret_url, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
