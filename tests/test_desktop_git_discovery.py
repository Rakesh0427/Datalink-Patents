import subprocess
import tempfile
import unittest
from pathlib import Path

from ccdc_local.cli import _normalize_repo_url
from ccdc_local.desktop import DesktopApp, discover_git_issues, flow_node_layers


class GitUrlNormalizationTests(unittest.TestCase):
    def test_github_branch_page_becomes_clone_url(self):
        self.assertEqual(
            _normalize_repo_url("https://github.com/acme/flight/tree/feature/SCR-42?tab=readme-ov-file"),
            "https://github.com/acme/flight.git",
        )

    def test_clone_command_prefix_and_gitlab_branch_page_are_supported(self):
        self.assertEqual(_normalize_repo_url('git clone "https://gitlab.com/acme/team/flight/-/tree/main"'), "https://gitlab.com/acme/team/flight")

    def test_windows_unc_repo_path_is_not_trimmed_or_rewritten(self):
        path = r"\\build-server\flight\controller-repo"
        self.assertEqual(_normalize_repo_url(path), path)

    def test_credentials_in_git_url_are_rejected_but_ssh_user_is_allowed(self):
        with self.assertRaisesRegex(ValueError, "Do not embed credentials"):
            _normalize_repo_url("https://user:secret@example.com/team/repo.git")
        self.assertEqual(_normalize_repo_url("ssh://git@github.com/acme/flight.git"), "ssh://git@github.com/acme/flight.git")


class GitScrDiscoveryTests(unittest.TestCase):
    def test_flow_layout_places_changed_node_before_downstream_calls(self):
        layers = flow_node_layers({
            "changed_functions": ["sensor::convert"],
            "impacted_functions": ["sensor::convert", "control::update", "output::drive"],
            "edges": [
                {"caller": "sensor::convert", "callee": "control::update"},
                {"caller": "control::update", "callee": "output::drive"},
            ],
        })
        self.assertEqual(layers["sensor::convert"], 0)
        self.assertLess(layers["sensor::convert"], layers["control::update"])
        self.assertLess(layers["control::update"], layers["output::drive"])

    def test_scr_keys_are_discovered_from_branch_and_commit_metadata(self):
        commits = [
            {"ref": "abc123", "date": "2026-05-01", "subject": "Fix sensor scaling for scr-42"},
            {"ref": "def456", "date": "2026-04-30", "subject": "Add monitor for SCR-43"},
        ]
        issues, matches = discover_git_issues(["feature/SCR-44-calibration"], commits)
        by_key = {issue["key"]: issue for issue in issues}
        self.assertEqual(set(by_key), {"SCR-42", "SCR-43", "SCR-44"})
        self.assertEqual(by_key["SCR-42"]["summary"], "Fix sensor scaling for scr-42")
        self.assertEqual(len(matches["SCR-44"]["branches"]), 1)

    def test_repository_scan_returns_repo_refs_recent_updates_and_scr_list(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=source, check=True)
            (source / "unit.c").write_text("int value(void) { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "add", "unit.c"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "Initial baseline"], cwd=source, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "feature/SCR-42-scaling"], cwd=source, check=True, capture_output=True)
            (source / "unit.c").write_text("int value(void) { return 42; }\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "Correct scaling for SCR-42"], cwd=source, check=True, capture_output=True)

            session = DesktopApp._inspect_repo(str(source))
            try:
                self.assertTrue(any(issue["key"] == "SCR-42" for issue in session["discovered_issues"]))
                self.assertIn("feature/SCR-42-scaling", session["refs"])
                self.assertIn("Correct scaling for SCR-42", session["recent_commits"][0]["subject"])
                self.assertTrue(session["default_base"])
            finally:
                session["tempdir"].cleanup()


if __name__ == "__main__":
    unittest.main()
