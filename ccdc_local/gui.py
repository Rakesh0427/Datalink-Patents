from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, SecretStr

from .analyzer import analyze_change
from .cli import _clone_repo, _default_base, _git
from .jira import JiraClient, JiraError


ASSET_DIR = Path(__file__).with_name("static")
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


@dataclass
class RepoSession:
    path: Path
    label: str
    issue_key: str
    default_base: str
    refs: list[str]
    branches: list[str]
    commits: list[dict[str, str]]
    tempdir: tempfile.TemporaryDirectory


SESSIONS: dict[str, RepoSession] = {}
JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()

@asynccontextmanager
async def lifespan(_app):
    yield
    with LOCK:
        sessions = list(SESSIONS.values())
        SESSIONS.clear()
    for session in sessions:
        session.tempdir.cleanup()


app = FastAPI(title="CC/DC Local Workbench", docs_url=None, redoc_url=None, lifespan=lifespan)


class IssueListRequest(BaseModel):
    jira_url: str = Field(min_length=5, max_length=600)
    project: str = Field(default="SCR", min_length=2, max_length=20)
    days: int = Field(default=30, ge=1, le=3650)
    auth_mode: Literal["cloud", "datacenter"] = "cloud"
    email: str | None = Field(default=None, max_length=320)
    api_token: SecretStr | None = None
    pat: SecretStr | None = None


class RepoOpenRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=2000)
    issue_key: str = Field(min_length=3, max_length=50)


class AnalyzeRequest(BaseModel):
    session_id: str
    issue: dict[str, Any]
    base_ref: str
    head_ref: str
    config_path: str | None = None


def _safe_repo_input(value: str) -> None:
    candidate = Path(value).expanduser()
    if candidate.exists():
        return
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "ssh", "git"}:
        raise HTTPException(400, "Use an HTTPS/SSH Git URL or a local Git folder path.")
    if parsed.username or parsed.password:
        raise HTTPException(400, "Do not place Git credentials in the URL. Configure Git Credential Manager or SSH instead.")


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse((ASSET_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/assets/{asset_name}")
def asset(asset_name: str):
    if asset_name not in {"app.js", "styles.css"}:
        raise HTTPException(404, "Asset not found")
    return FileResponse(ASSET_DIR / asset_name)


@app.get("/api/health")
def health() -> dict[str, Any]:
    auth_ready = bool(os.environ.get("JIRA_PAT") or (os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN")))
    return {
        "ok": True,
        "product": "CC/DC Local Workbench",
        "jira_auth_configured": auth_ready,
        "jira_auth_hint": "Jira credentials are read from local environment variables; they are never sent back to the browser.",
    }


@app.post("/api/issues")
def list_issues(payload: IssueListRequest) -> dict[str, Any]:
    try:
        api_token = payload.api_token.get_secret_value() if payload.api_token else None
        pat = payload.pat.get_secret_value() if payload.pat else None
        client = JiraClient(
            payload.jira_url,
            auth_mode=payload.auth_mode,
            email=payload.email,
            api_token=api_token,
            pat=pat,
        )
        issues = client.recent_issues(payload.project, payload.days)
        return {"issues": issues, "count": len(issues)}
    except JiraError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/repo/open")
def open_repo(payload: RepoOpenRequest) -> dict[str, Any]:
    issue_key = payload.issue_key.upper()
    if not ISSUE_KEY_RE.fullmatch(issue_key):
        raise HTTPException(400, "Select a Jira issue key such as SCR-123.")
    _safe_repo_input(payload.repo_url)
    tmp = tempfile.TemporaryDirectory(prefix="ccdc-gui-")
    repo = Path(tmp.name) / "repo"
    try:
        _clone_repo(payload.repo_url, repo)
        refs = _git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", "refs/heads"], repo, check=False).splitlines()
        refs = sorted({ref for ref in refs if ref and not ref.endswith("/HEAD")})
        branches = [ref for ref in refs if issue_key.lower() in ref.lower()]
        log = _git(["log", "--all", "--regexp-ignore-case", f"--grep={issue_key}", "--format=%H%x09%s"], repo, check=False)
        commits = []
        for line in log.splitlines()[:50]:
            parts = line.split("\t", 1)
            if len(parts) == 2:
                commits.append({"ref": parts[0], "subject": parts[1]})
        default_base = _default_base(repo)
        session_id = uuid.uuid4().hex
        session = RepoSession(repo, payload.repo_url, issue_key, default_base, refs, branches, commits, tmp)
        with LOCK:
            SESSIONS[session_id] = session
        return {
            "session_id": session_id,
            "repo_label": payload.repo_url,
            "default_base": default_base,
            "refs": refs,
            "matching_branches": branches,
            "matching_commits": commits,
            "matched_issue_key": bool(branches or commits),
        }
    except Exception as exc:
        tmp.cleanup()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(400, f"Could not prepare repository: {exc}") from exc


@app.post("/api/analyze")
def start_analysis(payload: AnalyzeRequest) -> dict[str, str]:
    if not ISSUE_KEY_RE.fullmatch(str(payload.issue.get("key", ""))):
        raise HTTPException(400, "The selected issue key is invalid.")
    with LOCK:
        session = SESSIONS.get(payload.session_id)
    if not session:
        raise HTTPException(404, "Repository session expired. Open the repository again.")
    if session.issue_key != payload.issue["key"].upper():
        raise HTTPException(400, "The selected issue does not match the inspected repository session.")
    for ref in (payload.base_ref, payload.head_ref):
        if not _git(["rev-parse", "--verify", ref], session.path, check=False):
            raise HTTPException(400, f"Git ref was not found: {ref}")
    if not _git(["merge-base", payload.base_ref, payload.head_ref], session.path, check=False):
        raise HTTPException(400, "Base and head refs have no common ancestor.")
    job_id = uuid.uuid4().hex
    output_root = Path(os.environ.get("CCDC_REPORTS_DIR", Path.cwd() / "ccdc-reports"))
    output = output_root / session.issue_key / job_id[:8]
    with LOCK:
        JOBS[job_id] = {"state": "running", "phase": "Analyzing source and comparing coupling flows", "error": None, "report_id": job_id}
    config = Path(payload.config_path).expanduser().resolve() if payload.config_path else None
    thread = threading.Thread(
        target=_run_analysis,
        args=(job_id, session, payload.base_ref, payload.head_ref, payload.issue, output, config),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


def _run_analysis(job_id: str, session: RepoSession, base_ref: str, head_ref: str, issue: dict[str, Any], output: Path, config: Path | None) -> None:
    try:
        files = analyze_change(session.path, base_ref, head_ref, issue, output, config, repo_label=session.label)
        with LOCK:
            JOBS[job_id].update({"state": "complete", "phase": "Report ready", "files": {k: str(v) for k, v in files.items()}, "report_id": job_id})
    except Exception as exc:
        with LOCK:
            JOBS[job_id].update({"state": "failed", "phase": "Analysis failed", "error": str(exc)})
    finally:
        session.tempdir.cleanup()
        with LOCK:
            for session_id, candidate in list(SESSIONS.items()):
                if candidate is session:
                    SESSIONS.pop(session_id, None)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with LOCK:
        record = JOBS.get(job_id)
    if not record:
        raise HTTPException(404, "Analysis job not found.")
    return {k: v for k, v in record.items() if k != "files"}


@app.get("/api/reports/{report_id}")
def report(report_id: str) -> dict[str, Any]:
    with LOCK:
        record = JOBS.get(report_id)
    if not record or record.get("state") != "complete":
        raise HTTPException(404, "Report is not ready.")
    paths = record["files"]
    try:
        return {
            "markdown": Path(paths["markdown"]).read_text(encoding="utf-8"),
            "json": json.loads(Path(paths["json"]).read_text(encoding="utf-8")),
            "mermaid": Path(paths["mermaid"]).read_text(encoding="utf-8"),
            "diff": Path(paths["diff"]).read_text(encoding="utf-8"),
            "files": paths,
        }
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Report files could not be read: {exc}") from exc


def main() -> None:
    host = os.environ.get("CCDC_GUI_HOST", "127.0.0.1")
    port = int(os.environ.get("CCDC_GUI_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    if os.environ.get("CCDC_GUI_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"CC/DC Local Workbench: {url}")
    print("Local GUI. Jira credentials are read from the process environment and are not displayed.")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
