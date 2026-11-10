from __future__ import annotations

import base64
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


class JiraError(RuntimeError):
    pass


def normalize_jira_url(value: str) -> tuple[str, str | None]:
    """Accept a Jira site URL or /browse/SCR-123 issue URL."""
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    match = re.search(r"/(?:browse|issues)/([A-Z][A-Z0-9]+-\d+)/?$", parsed.path, re.I)
    issue_key = match.group(1).upper() if match else None
    if match:
        base_path = parsed.path[:match.start()].rstrip("/")
        if (parsed.hostname or "").endswith("atlassian.net"):
            base_path = ""
        parsed = parsed._replace(path=base_path, params="", query="", fragment="")
        value = urlunparse(parsed).rstrip("/")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise JiraError("Enter a Jira HTTPS URL (HTTP is allowed only for localhost testing).")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise JiraError("Jira credentials are only sent over HTTPS (HTTP is allowed only for localhost testing).")
    return value, issue_key


class JiraClient:
    def __init__(self, site_url: str, auth_mode: str | None = None, email: str | None = None, api_token: str | None = None, pat: str | None = None):
        self.site_url, self.direct_issue = normalize_jira_url(site_url)
        self.auth_mode = auth_mode or ("datacenter" if pat or os.environ.get("JIRA_PAT") else "cloud")
        self.email = (email if email is not None else os.environ.get("JIRA_EMAIL", "")).strip()
        self.api_token = (api_token if api_token is not None else os.environ.get("JIRA_API_TOKEN", "")).strip()
        self.pat = (pat if pat is not None else os.environ.get("JIRA_PAT", "")).strip()
        if self.auth_mode == "cloud" and not (self.email and self.api_token):
            raise JiraError("For Jira Cloud, provide an email and API token in the GUI or set JIRA_EMAIL and JIRA_API_TOKEN locally.")
        if self.auth_mode == "datacenter" and not self.pat:
            raise JiraError("For Jira Data Center, provide a PAT in the GUI or set JIRA_PAT locally.")
        if self.auth_mode not in {"cloud", "datacenter"}:
            raise JiraError("Jira authentication mode must be cloud or datacenter.")
        self.cloud = urlparse(self.site_url).hostname.endswith("atlassian.net") if urlparse(self.site_url).hostname else False
        self.api_version = "3" if self.cloud else "2"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_mode == "datacenter":
            headers["Authorization"] = f"Bearer {self.pat}"
        else:
            raw = f"{self.email}:{self.api_token}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.site_url}/rest/api/{self.api_version}/{path.lstrip('/')}"
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:700]
            raise JiraError(f"Jira API returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise JiraError(f"Could not reach Jira: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise JiraError("Jira returned a response that was not valid JSON") from exc

    def recent_issues(self, project: str, days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
        if self.direct_issue:
            return [self.get_issue(self.direct_issue)]
        project = project.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", project):
            raise JiraError("Project key should contain letters/numbers/underscores, e.g. SCR")
        jql = f'project = "{project}" AND updated >= -{int(days)}d ORDER BY updated DESC'
        fields = "summary,updated,status,issuetype"
        if self.cloud:
            data = self._get("search/jql", {"jql": jql, "maxResults": str(limit), "fields": fields})
        else:
            data = self._get("search", {"jql": jql, "maxResults": str(limit), "startAt": "0", "fields": fields})
        return [self._with_url(self._normalize_issue(issue)) for issue in data.get("issues", [])[:limit]]

    def get_issue(self, key: str) -> dict[str, Any]:
        data = self._get(f"issue/{key}", {"fields": "summary,updated,status,issuetype"})
        return self._with_url(self._normalize_issue(data))

    def _with_url(self, issue: dict[str, Any]) -> dict[str, Any]:
        issue["url"] = f"{self.site_url}/browse/{issue['key']}"
        return issue

    @staticmethod
    def _normalize_issue(issue: dict[str, Any]) -> dict[str, Any]:
        fields = issue.get("fields", {})
        status = fields.get("status") or {}
        return {
            "key": issue.get("key", ""),
            "summary": fields.get("summary", ""),
            "updated": fields.get("updated", ""),
            "status": status.get("name", "") if isinstance(status, dict) else str(status),
            "issue_type": (fields.get("issuetype") or {}).get("name", ""),
            "id": issue.get("id"),
        }
