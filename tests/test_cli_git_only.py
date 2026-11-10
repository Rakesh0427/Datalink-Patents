import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ccdc_local.cli import main


class GitOnlyCliTests(unittest.TestCase):
    def test_issue_key_runs_without_jira_credentials_or_client(self):
        with tempfile.TemporaryDirectory() as output_dir:
            with (
                patch("ccdc_local.cli._clone_repo"),
                patch("ccdc_local.cli._select_refs", return_value=("main", "feature/SCR-123-fix")),
                patch("ccdc_local.cli._git", return_value="deadbeef"),
                patch("ccdc_local.cli.analyze_change", return_value={"markdown": Path(output_dir) / "report.md"}) as analyze,
                patch("ccdc_local.cli.JiraClient") as jira_client,
                redirect_stdout(io.StringIO()),
            ):
                result = main([
                    "--repo", "https://example.test/flight.git",
                    "--issue", "scr-123",
                    "--out", output_dir,
                ])

        self.assertEqual(result, 0)
        jira_client.assert_not_called()
        self.assertEqual(analyze.call_args.args[3]["key"], "SCR-123")
        self.assertEqual(analyze.call_args.args[3]["status"], "Jira not queried")

    def test_invalid_scr_key_is_rejected_before_repository_access(self):
        with patch("ccdc_local.cli._clone_repo") as clone, redirect_stderr(io.StringIO()):
            result = main(["--repo", "https://example.test/flight.git", "--issue", "not-an-issue"])
        self.assertEqual(result, 2)
        clone.assert_not_called()


if __name__ == "__main__":
    unittest.main()
