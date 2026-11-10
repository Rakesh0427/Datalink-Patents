import unittest

from ccdc_local.jira import JiraError, normalize_jira_url


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

    def test_rejects_non_url(self):
        with self.assertRaises(JiraError):
            normalize_jira_url("not-a-jira-url")


if __name__ == "__main__":
    unittest.main()
