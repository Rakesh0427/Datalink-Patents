import base64
import unittest

from ccdc_local.jira import JiraClient, JiraError, normalize_jira_url


class JiraUrlTests(unittest.TestCase):
    def test_site_url_has_no_issue_key(self):
        self.assertEqual(
            normalize_jira_url("https://example.atlassian.net/"),
            ("https://example.atlassian.net", None),
        )

    def test_browse_url_extracts_issue_and_removes_query(self):
        self.assertEqual(
            normalize_jira_url("https://jira.example.test/jira/browse/SCR-123?focusedCommentId=4"),
            ("https://jira.example.test/jira", "SCR-123"),
        )

    def test_atlassian_issue_page_extracts_site_root(self):
        self.assertEqual(
            normalize_jira_url("https://example.atlassian.net/jira/software/projects/SCR/issues/SCR-123"),
            ("https://example.atlassian.net", "SCR-123"),
        )

    def test_cloud_gui_credentials_create_basic_auth_header(self):
        client = JiraClient(
            "https://example.atlassian.net",
            auth_mode="cloud",
            email="user@example.test",
            api_token="dummy-test-token",
        )
        expected = base64.b64encode(b"user@example.test:dummy-test-token").decode("ascii")
        self.assertEqual(client._headers()["Authorization"], f"Basic {expected}")

    def test_data_center_gui_credential_uses_bearer_auth(self):
        client = JiraClient(
            "https://jira.example.test",
            auth_mode="datacenter",
            pat="dummy-test-pat",
        )
        self.assertEqual(client._headers()["Authorization"], "Bearer dummy-test-pat")

    def test_rejects_missing_auth(self):
        with self.assertRaises(JiraError):
            JiraClient("https://example.atlassian.net", auth_mode="cloud")

    def test_rejects_non_url(self):
        with self.assertRaises(JiraError):
            normalize_jira_url("not-a-jira-url")


if __name__ == "__main__":
    unittest.main()
