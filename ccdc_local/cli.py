from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit

from .analyzer import analyze_change
from .jira import JiraClient, JiraError


def _git(args: Sequence[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _ask(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or (default or "")


def _choose(prompt: str, values: list[dict], default: int = 1) -> dict:
    for index, item in enumerate(values, start=1):
        print(f"  {index}. {item.get('key', item.get('ref', ''))} — {item.get('summary', item.get('subject', ''))}")
        if item.get("updated"):
            print(f"     updated {item['updated']} | {item.get('status', '')}")
    while True:
        answer = input(f"{prompt} [1-{len(values)}] (default {default}): ").strip()
        if not answer:
            return values[default - 1]
        if answer.isdigit() and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        print("Enter one of the listed numbers.")


def _normalize_repo_url(value: str) -> str:
    """Accept clone URLs and common repository-browser URLs without branch/query suffixes."""
    value = value.strip().strip("\\\"")
    if value.startswith("git clone "):
        value = value[len("git clone "):].strip().strip("\\\"")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "github.com" and len(parts) >= 2:
        parts = parts[:2]
    elif host == "gitlab.com" and "-" in parts:
        parts = parts[:parts.index("-")]
    elif host == "bitbucket.org" and "src" in parts[2:]:
        parts = parts[:parts.index("src")]
    if parsed.scheme in {"https", "http", "ssh", "git"} and parsed.netloc:
        path = "/" + "/".join(parts)
        if host == "github.com" and parts and not parts[-1].endswith(".git"):
            path += ".git"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return value


def _clone_repo(repo_url: str, destination: Path) -> None:
    repo_url = _normalize_repo_url(repo_url)
    local = Path(repo_url).expanduser()
    if local.exists():
        source = local.resolve()
        check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=source, text=True, capture_output=True)
        if check.returncode:
            raise RuntimeError(f"Local path is not a Git repository: {source}")
        command = ["git", "clone", "--no-hardlinks", "--no-single-branch", str(source), str(destination)]
    else:
        command = ["git", "clone", "--no-single-branch", repo_url, str(destination)]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Git returned no diagnostic output."
        raise RuntimeError(
            "Git could not clone this repository. Paste the repository URL (not a commit/issue page), "
            "and for private repositories verify Git Credential Manager or SSH access.\n\n"
            f"Git output: {detail}"
        )
    fetch = subprocess.run(["git", "fetch", "--all", "--prune"], cwd=destination, text=True, capture_output=True)
    if fetch.returncode:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "Git returned no diagnostic output."
        raise RuntimeError(f"The repository cloned, but Git could not fetch all refs. Check network/access and retry.\n\nGit output: {detail}")


def _default_base(repo: Path) -> str:
    symbolic = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo, check=False)
    if symbolic.startswith("refs/remotes/"):
        candidate = symbolic.removeprefix("refs/remotes/")
        if _git(["rev-parse", "--verify", candidate], repo, check=False):
            return candidate
    for candidate in ("origin/main", "origin/master", "main", "master"):
        if _git(["rev-parse", "--verify", candidate], repo, check=False):
            return candidate
    return "HEAD"


def _select_refs(repo: Path, issue_key: str) -> tuple[str, str]:
    refs = _git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", "refs/heads"], repo, check=False).splitlines()
    branches = [ref for ref in refs if issue_key.lower() in ref.lower() and not ref.endswith("/HEAD")]
    default_base = _default_base(repo)
    if branches:
        print(f"\nGit branches containing {issue_key}:")
        branch_items = [{"ref": ref, "subject": "branch"} for ref in branches]
        head = _choose("Select the SCR branch", branch_items)["ref"]
        base = _ask("Base branch/ref", default_base)
        return base, head

    matches = _git(["log", "--all", "--regexp-ignore-case", f"--grep={issue_key}", "--format=%H%x09%s"], repo, check=False).splitlines()
    commits = []
    for line in matches[:50]:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            commits.append({"ref": parts[0], "subject": parts[1]})
    if commits:
        print(f"\nCommits mentioning {issue_key} (newest first):")
        chosen = _choose("Select a commit to analyze", commits)
        head = chosen["ref"]
        parent = _git(["rev-parse", f"{head}^"], repo, check=False)
        base = _ask("Base commit/ref", parent or default_base)
        return base, head

    print(f"\nNo branch or commit containing {issue_key} was found.")
    print("Use the SCR-linked pull request's base and head refs, or the commit range for the change.")
    base = _ask("Base branch/ref", default_base)
    head = _ask("Head branch/ref", "HEAD")
    return base, head


def _select_issue(client: JiraClient, project: str, days: int, issue_key: str | None) -> dict:
    if issue_key:
        issue = client.get_issue(issue_key)
        if not issue.get("key"):
            raise JiraError(f"Jira did not return issue {issue_key}")
        return issue
    issues = client.recent_issues(project, days)
    if not issues:
        raise JiraError(f"No recently updated Jira issues found for project {project} in the last {days} days.")
    print(f"\nRecent issues in {project}, updated within {days} days:")
    return _choose("Select the SCR issue to analyze", issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccdc-local",
        description="Local C/C++ CC/DC change-impact report generator; Jira is optional.",
        epilog="Use --issue SCR-123 without --jira-url for Git-only analysis. Jira credentials are read only from JIRA_EMAIL + JIRA_API_TOKEN (Cloud) or JIRA_PAT (Data Center).",
    )
    parser.add_argument("--repo", help="Git URL or local Git working-copy path; prompted if omitted")
    parser.add_argument("--jira-url", help="optional Jira site or direct issue URL; prompted only when --issue is omitted")
    parser.add_argument("--project", default="SCR", help="Jira project key for recent-issue listing (default: SCR)")
    parser.add_argument("--days", type=int, default=30, help="list issues updated in this many days (default: 30)")
    parser.add_argument("--issue", help="analyze an SCR key directly; works without Jira when --jira-url is omitted")
    parser.add_argument("--out", type=Path, default=Path("ccdc-reports"), help="report output directory")
    parser.add_argument("--config", type=Path, help="optional ccdc.json path in the target repository")
    args = parser.parse_args(argv)

    try:
        repo_url = (args.repo or _ask("Git repository URL or local path")).strip()
        if not repo_url:
            raise ValueError("Repository URL or local path is required.")
        jira_url = (args.jira_url or "").strip()
        if args.issue:
            issue_key = args.issue.strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", issue_key):
                raise ValueError("SCR key must look like SCR-123.")
            if jira_url:
                issue = JiraClient(jira_url).get_issue(issue_key)
            else:
                issue = {"key": issue_key, "summary": "SCR identified from Git", "updated": "Git only", "status": "Jira not queried", "url": None}
        else:
            if not jira_url:
                jira_url = _ask("Jira site URL (leave blank for Git-only SCR analysis)").strip()
            if jira_url:
                issue = _select_issue(JiraClient(jira_url), args.project, args.days, None)
            else:
                issue_key = _ask("SCR key from the branch/commit, e.g. SCR-123").strip().upper()
                if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", issue_key):
                    raise ValueError("SCR key must look like SCR-123.")
                issue = {"key": issue_key, "summary": "SCR identified from Git", "updated": "Git only", "status": "Jira not queried", "url": None}
            issue_key = issue["key"]
        print(f"\nSelected {issue_key}: {issue.get('summary', '')}")

        with tempfile.TemporaryDirectory(prefix="ccdc-local-") as temp:
            repo = Path(temp) / "repo"
            print("Cloning/fetching the repository locally…")
            _clone_repo(repo_url, repo)
            base_ref, head_ref = _select_refs(repo, issue_key)
            merge_base = _git(["merge-base", base_ref, head_ref], repo, check=False)
            if not merge_base:
                raise RuntimeError(f"The selected refs do not have a common Git ancestor: {base_ref}, {head_ref}")
            config_path = args.config
            if config_path and not config_path.is_absolute():
                config_path = config_path.resolve()
            destination = args.out.expanduser().resolve() / issue_key
            print(f"\nAnalyzing {base_ref}...{head_ref}; source remains in a temporary local clone.")
            outputs = analyze_change(repo, base_ref, head_ref, issue, destination, config_path, repo_label=repo_url)
            print("\nCC/DC analysis complete. Reports:")
            for kind, path in outputs.items():
                print(f"  {kind}: {path}")
            print("\nOpen ccdc-report.md for findings and impact-flow.mmd in a Mermaid-capable preview.")
        return 0
    except (JiraError, RuntimeError, OSError, ValueError) as exc:
        print(f"ccdc-local: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
