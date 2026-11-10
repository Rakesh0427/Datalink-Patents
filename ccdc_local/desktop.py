from __future__ import annotations

import os
import re
import subprocess
import tempfile
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:  # Tkinter ships with python.org Python on Windows/macOS; Linux may package it separately.
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = ttk = None  # type: ignore[assignment]

from .analyzer import analyze_change
from .cli import _clone_repo, _default_base, _git
from .jira import JiraClient

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
ISSUE_KEY_SCAN_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b", re.IGNORECASE)


def discover_git_issues(refs: list[str], commits: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, list[Any]]]]:
    """Collect SCR-like keys from refs and recent commit subjects without Jira."""
    matches: dict[str, dict[str, list[Any]]] = {}

    def ensure(key: str) -> dict[str, list[Any]]:
        normalized = key.upper()
        return matches.setdefault(normalized, {"branches": [], "commits": []})

    for ref in refs:
        for key in ISSUE_KEY_SCAN_RE.findall(ref):
            branches = ensure(key)["branches"]
            if ref not in branches:
                branches.append(ref)
    for commit in commits:
        for key in ISSUE_KEY_SCAN_RE.findall(commit.get("subject", "")):
            bucket = ensure(key)["commits"]
            if commit not in bucket:
                bucket.append(commit)

    issues = []
    for key, found in matches.items():
        newest = found["commits"][0] if found["commits"] else {}
        issues.append({
            "key": key,
            "summary": newest.get("subject") or f"SCR key in {found['branches'][0]}",
            "updated": newest.get("date", ""),
            "status": "Git commit" if newest else "Git branch",
            "url": None,
        })
    issues.sort(key=lambda issue: (issue.get("updated", ""), issue["key"]), reverse=True)
    return issues, matches


class DesktopApp:
    """Native Tk desktop UI for local-only C/C++ Control/Data Coupling analysis."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CC/DC Local Workbench")
        self.root.geometry("1120x790")
        self.root.minsize(900, 640)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ccdc")
        self.mode = tk.StringVar(value="git")
        self.repo_value = tk.StringVar()
        self.scr_key = tk.StringVar()
        self.jira_url = tk.StringVar()
        self.project = tk.StringVar(value="SCR")
        self.days = tk.StringVar(value="30")
        self.auth_mode = tk.StringVar(value="datacenter" if os.environ.get("JIRA_PAT") else "cloud")
        self.email = tk.StringVar()
        self.api_token = tk.StringVar()
        self.pat = tk.StringVar()
        self.config_path = tk.StringVar()
        self.base_ref = tk.StringVar()
        self.head_ref = tk.StringVar()
        self.status = tk.StringVar(value="Ready · choose Git SCR mode to analyze without Jira.")
        self.issues: list[dict[str, Any]] = []
        self.selected_issue: dict[str, Any] | None = None
        self.repo_session: dict[str, Any] | None = None
        self.report_paths: dict[str, Path] = {}
        self._busy = False
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._toggle_mode()

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"), foreground="#18304c")
        style.configure("Subhead.TLabel", font=("Segoe UI", 10), foreground="#5b6b7d")
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(12, 7))

        outer = ttk.Frame(self.root, padding=(18, 14))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="CC/DC Local Workbench", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Native desktop app · local Git analysis · Jira optional · CC = Control Coupling, DC = Data Coupling",
            style="Subhead.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        source = ttk.LabelFrame(outer, text="1 · Source and SCR", style="Section.TLabelframe", padding=12)
        source.pack(fill="x")
        ttk.Label(source, text="Git repository URL or local folder").grid(row=0, column=0, sticky="w")
        repo_entry = ttk.Entry(source, textvariable=self.repo_value)
        repo_entry.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 4))
        ttk.Button(source, text="Browse…", command=self._browse_repo).grid(row=1, column=3, padx=(8, 0), sticky="ew")
        ttk.Label(source, text="Private repositories use your configured Git credentials / SSH key.").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(0, 9)
        )
        source.columnconfigure(0, weight=1)

        mode_row = ttk.Frame(source)
        mode_row.grid(row=3, column=0, columnspan=4, sticky="w", pady=(0, 7))
        ttk.Radiobutton(mode_row, text="Use Git SCR (no Jira)", value="git", variable=self.mode, command=self._toggle_mode).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(mode_row, text="Browse Jira (optional)", value="jira", variable=self.mode, command=self._toggle_mode).pack(side="left")

        self.git_frame = ttk.Frame(source)
        self.git_frame.grid(row=4, column=0, columnspan=4, sticky="ew")
        ttk.Label(
            self.git_frame,
            text="Load the repository to see its recent commits and SCR keys found in branch names / commit messages. Select an SCR below; no key entry is needed.",
            wraplength=940,
        ).grid(row=0, column=0, sticky="w")

        self.jira_frame = ttk.LabelFrame(source, text="Optional Jira details", padding=8)
        self.jira_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        ttk.Label(self.jira_frame, text="Jira site / issue URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.jira_frame, textvariable=self.jira_url, width=38).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(3, 8))
        ttk.Label(self.jira_frame, text="Project").grid(row=0, column=1, sticky="w")
        ttk.Entry(self.jira_frame, textvariable=self.project, width=10).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(3, 8))
        ttk.Label(self.jira_frame, text="Updated within (days)").grid(row=0, column=2, sticky="w")
        ttk.Combobox(self.jira_frame, textvariable=self.days, values=("7", "30", "90", "180"), width=10, state="readonly").grid(row=1, column=2, sticky="ew", pady=(3, 8))
        ttk.Label(self.jira_frame, text="Authentication").grid(row=2, column=0, sticky="w")
        auth_box = ttk.Combobox(self.jira_frame, textvariable=self.auth_mode, values=("cloud", "datacenter"), state="readonly", width=18)
        auth_box.grid(row=3, column=0, sticky="ew", padx=(0, 8), pady=(3, 5))
        auth_box.bind("<<ComboboxSelected>>", lambda _event: self._toggle_auth())
        self.cloud_auth_frame = ttk.Frame(self.jira_frame)
        self.cloud_auth_frame.grid(row=2, column=1, columnspan=2, rowspan=2, sticky="ew", pady=(0, 5))
        ttk.Label(self.cloud_auth_frame, text="Account email").grid(row=0, column=0, sticky="w")
        ttk.Label(self.cloud_auth_frame, text="API token (held in memory only)").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Entry(self.cloud_auth_frame, textvariable=self.email, width=26).grid(row=1, column=0, sticky="ew", pady=(3, 0))
        ttk.Entry(self.cloud_auth_frame, textvariable=self.api_token, show="•", width=30).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(3, 0))
        self.cloud_auth_frame.columnconfigure(0, weight=1)
        self.cloud_auth_frame.columnconfigure(1, weight=1)
        self.dc_auth_frame = ttk.Frame(self.jira_frame)
        ttk.Label(self.dc_auth_frame, text="Data Center PAT (held in memory only)").pack(anchor="w")
        ttk.Entry(self.dc_auth_frame, textvariable=self.pat, show="•", width=45).pack(fill="x", pady=(3, 0))
        ttk.Label(self.jira_frame, text="Credentials are used only for Jira requests and are not saved to disk or reports.").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(3, 0)
        )
        self.jira_frame.columnconfigure(0, weight=2)
        self.jira_frame.columnconfigure(1, weight=1)
        self.jira_frame.columnconfigure(2, weight=1)

        actions = ttk.Frame(source)
        actions.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.load_button = ttk.Button(actions, text="Inspect SCR in Git", style="Primary.TButton", command=self._load_or_inspect)
        self.load_button.pack(side="left")
        self.status_label = ttk.Label(actions, textvariable=self.status)
        self.status_label.pack(side="left", padx=(12, 0))

        self.issue_frame = ttk.LabelFrame(outer, text="2 · SCRs and Git updates", style="Section.TLabelframe", padding=8)
        self.issue_frame.pack(fill="x", pady=(10, 0))
        self.repo_details = tk.StringVar(value="Load a repository to see its branches, SCR keys, and recent commits.")
        ttk.Label(self.issue_frame, textvariable=self.repo_details).pack(anchor="w", pady=(0, 6))
        split = ttk.Panedwindow(self.issue_frame, orient="horizontal")
        split.pack(fill="x", expand=True)
        scr_panel = ttk.LabelFrame(split, text="SCRs to select", padding=5)
        updates_panel = ttk.LabelFrame(split, text="Recent Git updates", padding=5)
        split.add(scr_panel, weight=1)
        split.add(updates_panel, weight=2)
        self.issue_list = tk.Listbox(scr_panel, height=5, activestyle="dotbox", exportselection=False)
        self.issue_list.pack(fill="both", expand=True)
        issue_scroll = ttk.Scrollbar(scr_panel, orient="vertical", command=self.issue_list.yview)
        issue_scroll.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        self.issue_list.configure(yscrollcommand=issue_scroll.set)
        self.inspect_button = ttk.Button(scr_panel, text="Inspect selected SCR", command=self._inspect_selected, state="disabled")
        self.inspect_button.pack(fill="x", pady=(5, 0))
        self.manual_frame = ttk.Frame(scr_panel)
        ttk.Label(self.manual_frame, text="No SCR key found in Git; optional manual fallback:").pack(anchor="w")
        manual_row = ttk.Frame(self.manual_frame)
        manual_row.pack(fill="x", pady=(3, 0))
        ttk.Entry(manual_row, textvariable=self.scr_key).pack(side="left", fill="x", expand=True)
        ttk.Button(manual_row, text="Use key", command=self._use_manual_key).pack(side="left", padx=(5, 0))
        self.update_list = ttk.Treeview(updates_panel, columns=("date", "ref", "subject"), show="headings", height=5)
        self.update_list.heading("date", text="DATE")
        self.update_list.heading("ref", text="COMMIT")
        self.update_list.heading("subject", text="UPDATE / SCR KEY")
        self.update_list.column("date", width=90, stretch=False)
        self.update_list.column("ref", width=100, stretch=False)
        self.update_list.column("subject", width=420, stretch=True)
        self.update_list.pack(fill="both", expand=True)
        update_scroll = ttk.Scrollbar(updates_panel, orient="vertical", command=self.update_list.yview)
        update_scroll.pack(side="right", fill="y")
        self.update_list.configure(yscrollcommand=update_scroll.set)

        refs = ttk.LabelFrame(outer, text="3 · Choose the Git change range", style="Section.TLabelframe", padding=10)
        refs.pack(fill="x", pady=(10, 0))
        self.refs_frame = refs
        ttk.Label(refs, text="Base (before change)").grid(row=0, column=0, sticky="w")
        ttk.Label(refs, text="Head (SCR change)").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.base_combo = ttk.Combobox(refs, textvariable=self.base_ref, width=46)
        self.base_combo.grid(row=1, column=0, sticky="ew", pady=(3, 7))
        self.head_combo = ttk.Combobox(refs, textvariable=self.head_ref, width=46)
        self.head_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(3, 7))
        ttk.Label(refs, text="Optional ccdc.json").grid(row=2, column=0, sticky="w")
        config_row = ttk.Frame(refs)
        config_row.grid(row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Entry(config_row, textvariable=self.config_path).pack(side="left", fill="x", expand=True)
        ttk.Button(config_row, text="Browse…", command=self._browse_config).pack(side="left", padx=(6, 0))
        self.analyze_button = ttk.Button(refs, text="Analyze selected range", style="Primary.TButton", command=self._analyze, state="disabled")
        self.analyze_button.grid(row=3, column=1, sticky="e", padx=(8, 0))
        refs.columnconfigure(0, weight=1)
        refs.columnconfigure(1, weight=1)

        results = ttk.LabelFrame(outer, text="4 · Impact report", style="Section.TLabelframe", padding=8)
        results.pack(fill="both", expand=True, pady=(10, 0))
        self.notebook = ttk.Notebook(results)
        self.notebook.pack(fill="both", expand=True)
        self.result_text: dict[str, tk.Text] = {}
        for title, key in (("Summary", "markdown"), ("Source diff", "diff"), ("Mermaid", "mermaid"), ("JSON", "json")):
            page = ttk.Frame(self.notebook)
            self.notebook.add(page, text=title)
            text = tk.Text(page, wrap="none", height=9, font=("Consolas", 9), undo=False)
            yscroll = ttk.Scrollbar(page, orient="vertical", command=text.yview)
            xscroll = ttk.Scrollbar(page, orient="horizontal", command=text.xview)
            text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set, state="disabled")
            text.grid(row=0, column=0, sticky="nsew")
            yscroll.grid(row=0, column=1, sticky="ns")
            xscroll.grid(row=1, column=0, sticky="ew")
            page.rowconfigure(0, weight=1)
            page.columnconfigure(0, weight=1)
            self.result_text[key] = text
        result_actions = ttk.Frame(results)
        result_actions.pack(fill="x", pady=(6, 0))
        self.open_report_button = ttk.Button(result_actions, text="Open report file", command=self._open_report, state="disabled")
        self.open_report_button.pack(side="left")
        ttk.Label(result_actions, text="Reports are saved under ccdc-reports/<SCR-KEY>/").pack(side="left", padx=(10, 0))

    def _toggle_mode(self) -> None:
        if self.mode.get() == "git":
            self.jira_frame.grid_remove()
            self.git_frame.grid()
            self.issue_frame.configure(text="2 · SCRs discovered from Git")
            self.load_button.configure(text="Load repository updates")
            self.status.set("Git-only mode · load the repository to discover SCR keys in its branches and commits.")
        else:
            self.git_frame.grid_remove()
            self.jira_frame.grid()
            self.issue_frame.configure(text="2 · Jira issues and Git updates")
            self.load_button.configure(text="Find recent SCRs in Jira")
            self.status.set("Jira mode · Jira supplies issue metadata; code analysis still runs locally.")
        self._toggle_auth()

    def _toggle_auth(self) -> None:
        if self.auth_mode.get() == "cloud":
            self.dc_auth_frame.grid_remove()
            self.cloud_auth_frame.grid()
        else:
            self.cloud_auth_frame.grid_remove()
            self.dc_auth_frame.grid(row=2, column=1, columnspan=2, rowspan=2, sticky="ew", padx=(8, 0))

    def _browse_repo(self) -> None:
        path = filedialog.askdirectory(title="Select a local Git repository")
        if path:
            self.repo_value.set(path)

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(title="Select ccdc.json", filetypes=(("CC/DC config", "ccdc.json"), ("JSON files", "*.json"), ("All files", "*.*")))
        if path:
            self.config_path.set(path)

    def _load_or_inspect(self) -> None:
        repo_value = self.repo_value.get().strip()
        if not repo_value:
            messagebox.showerror("Repository required", "Enter a Git URL or choose a local Git folder.", parent=self.root)
            return
        if self.mode.get() == "git":
            self._start_git_discovery(repo_value)
            return

        jira_url = self.jira_url.get().strip()
        if not jira_url:
            messagebox.showerror("Jira URL required", "Enter your Jira site URL or switch to Use Git SCR.", parent=self.root)
            return
        try:
            days = int(self.days.get())
        except ValueError:
            messagebox.showerror("Invalid update window", "Choose a number of days.", parent=self.root)
            return
        auth_mode = self.auth_mode.get()
        email = self.email.get().strip() or None
        api_token = self.api_token.get() or None
        pat = self.pat.get() or None
        project = self.project.get().strip() or "SCR"

        def fetch() -> list[dict[str, Any]]:
            client = JiraClient(jira_url, auth_mode=auth_mode, email=email, api_token=api_token, pat=pat)
            return client.recent_issues(project, days)

        self._run_async(fetch, self._show_issues, "Fetching recent SCRs from Jira…")

    def _fill_issue_list(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.issue_list.delete(0, tk.END)
        for issue in issues:
            summary = issue.get("summary") or "(No summary)"
            updated = issue.get("updated", "")[:10]
            self.issue_list.insert(tk.END, f"{issue.get('key', '')}  ·  {summary}  ·  {updated}")

    def _show_issues(self, issues: list[dict[str, Any]]) -> None:
        self._fill_issue_list(issues)
        self._show_updates([])
        self.manual_frame.pack_forget()
        self.repo_details.set(f"Jira returned {len(issues)} issue(s). Repository updates will load after you select an SCR.")
        self.inspect_button.configure(text="Inspect selected SCR", state="normal" if issues else "disabled")
        self.status.set(f"Loaded {len(issues)} Jira issue(s). Select one, then inspect its Git refs.")
        if not issues:
            messagebox.showinfo("No issues found", "No recent Jira issues matched the selected project and date range.", parent=self.root)

    def _inspect_selected(self) -> None:
        selection = self.issue_list.curselection()
        if not selection:
            messagebox.showinfo("Select an SCR", "Choose an SCR from the list first.", parent=self.root)
            return
        issue = self.issues[selection[0]]
        if self.mode.get() == "git":
            self.selected_issue = issue
            if self.repo_session:
                self._show_issue_refs(self.repo_session, issue)
            return
        self._start_repo_inspection(self.repo_value.get().strip(), issue)

    def _start_git_discovery(self, repo_value: str) -> None:
        self.selected_issue = None
        self.analyze_button.configure(state="disabled")
        self._run_async(
            lambda: self._inspect_repo(repo_value),
            self._show_git_repository,
            "Cloning/fetching repository and scanning branches + recent commits…",
        )

    def _start_repo_inspection(self, repo_value: str, issue: dict[str, Any]) -> None:
        if not repo_value:
            messagebox.showerror("Repository required", "Enter a Git URL or choose a local Git folder.", parent=self.root)
            return
        self.selected_issue = issue
        if self.repo_session and self.repo_session.get("repo_label") == repo_value:
            self._show_issue_refs(self.repo_session, issue)
            return
        self.analyze_button.configure(state="disabled")
        self._run_async(
            lambda: self._inspect_repo(repo_value),
            lambda session: self._show_jira_repository(session, issue),
            f"Loading repository details for {issue['key']}…",
        )

    @staticmethod
    def _inspect_repo(repo_value: str) -> dict[str, Any]:
        tempdir = tempfile.TemporaryDirectory(prefix="ccdc-desktop-")
        repo = Path(tempdir.name) / "repo"
        try:
            _clone_repo(repo_value, repo)
            refs = sorted({
                ref for ref in _git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", "refs/heads"], repo, check=False).splitlines()
                if ref and not ref.endswith("/HEAD")
            })
            raw_log = _git(
                ["log", "--all", "--max-count=5000", "--date=short", "--format=%H%x09%ad%x09%s"],
                repo,
                check=False,
            )
            commits = []
            for line in raw_log.splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    commits.append({"ref": parts[0], "short": parts[0][:10], "date": parts[1], "subject": parts[2]})
            issues, issue_matches = discover_git_issues(refs, commits)
            latest = commits[0] if commits else {"subject": "No commits found", "date": ""}
            return {
                "tempdir": tempdir,
                "path": repo,
                "repo_label": repo_value,
                "refs": refs,
                "commits": commits,
                "recent_commits": commits[:100],
                "discovered_issues": issues,
                "issue_matches": issue_matches,
                "default_base": _default_base(repo),
                "latest_commit": latest,
            }
        except Exception:
            tempdir.cleanup()
            raise

    def _replace_repository_session(self, session: dict[str, Any]) -> None:
        if self.repo_session and self.repo_session is not session:
            self.repo_session["tempdir"].cleanup()
        self.repo_session = session
        self._show_updates(session.get("recent_commits", []))
        latest = session.get("latest_commit", {})
        self.repo_details.set(
            f"{session['repo_label']}  ·  base {session['default_base']}  ·  {len(session['refs'])} refs  ·  "
            f"{len(session['commits'])} commits scanned  ·  latest: {latest.get('date', '')} {latest.get('subject', '')}"
        )

    def _show_updates(self, commits: list[dict[str, str]]) -> None:
        for item in self.update_list.get_children():
            self.update_list.delete(item)
        for commit in commits:
            subject = commit.get("subject", "")
            keys = ", ".join(dict.fromkeys(key.upper() for key in ISSUE_KEY_SCAN_RE.findall(subject)))
            display = f"{keys} · {subject}" if keys else subject
            self.update_list.insert("", "end", values=(commit.get("date", ""), commit.get("short", ""), display))

    def _show_git_repository(self, session: dict[str, Any]) -> None:
        self._replace_repository_session(session)
        issues = session["discovered_issues"]
        self._fill_issue_list(issues)
        self.inspect_button.configure(text="Inspect selected SCR", state="normal" if issues else "disabled")
        if issues:
            self.manual_frame.pack_forget()
            self.status.set(f"Found {len(issues)} SCR key(s) in repository history. Select one to inspect matching refs.")
        else:
            self.manual_frame.pack(fill="x", pady=(6, 0))
            self.status.set("Repository loaded, but no SCR-like key was found in its branches or recent commit messages.")

    def _show_jira_repository(self, session: dict[str, Any], issue: dict[str, Any]) -> None:
        self._replace_repository_session(session)
        self.selected_issue = issue
        self._show_issue_refs(session, issue)

    def _show_issue_refs(self, session: dict[str, Any], issue: dict[str, Any]) -> None:
        matches = session["issue_matches"].get(issue["key"].upper(), {"branches": [], "commits": []})
        branches = matches["branches"]
        commits = matches["commits"]
        commit_refs = [item["ref"] for item in commits]
        base_choices = list(dict.fromkeys([session["default_base"], *session["refs"]]))
        head_choices = list(dict.fromkeys([*branches, *commit_refs, "HEAD", *session["refs"]]))
        self.base_combo.configure(values=base_choices)
        self.head_combo.configure(values=head_choices)
        self.base_ref.set(session["default_base"])
        self.head_ref.set(branches[0] if branches else (commit_refs[0] if commit_refs else "HEAD"))
        if branches or commits:
            self.status.set(
                f"{issue['key']}: found {len(branches)} matching branch(es) and {len(commits)} commit(s). Verify refs before analysis."
            )
        else:
            self.status.set(f"No branch or commit mentions {issue['key']}; choose the SCR range from the Git updates list.")
        self.analyze_button.configure(state="normal")

    def _use_manual_key(self) -> None:
        if not self.repo_session:
            return
        key = self.scr_key.get().strip().upper()
        if not ISSUE_KEY_RE.fullmatch(key):
            messagebox.showerror("Invalid SCR key", "Enter a valid key such as SCR-123.", parent=self.root)
            return
        issue = {"key": key, "summary": "Manually identified SCR", "updated": "Git only", "status": "Jira not queried", "url": None}
        self.selected_issue = issue
        self._show_issue_refs(self.repo_session, issue)

    def _analyze(self) -> None:
        if not self.repo_session or not self.selected_issue:
            messagebox.showerror("Inspect a repository first", "Choose an SCR and inspect the Git repository before analysis.", parent=self.root)
            return
        base_ref = self.base_ref.get().strip()
        head_ref = self.head_ref.get().strip()
        if not base_ref or not head_ref:
            messagebox.showerror("Git refs required", "Choose both a base and head ref.", parent=self.root)
            return
        config_value = self.config_path.get().strip()
        config_path = Path(config_value).expanduser() if config_value else None
        session = self.repo_session
        issue = self.selected_issue
        output_dir = Path.cwd() / "ccdc-reports" / issue["key"]

        def run() -> dict[str, Any]:
            if not _git(["rev-parse", "--verify", base_ref], session["path"], check=False):
                raise RuntimeError(f"Git base ref was not found: {base_ref}")
            if not _git(["rev-parse", "--verify", head_ref], session["path"], check=False):
                raise RuntimeError(f"Git head ref was not found: {head_ref}")
            if not _git(["merge-base", base_ref, head_ref], session["path"], check=False):
                raise RuntimeError("Base and head refs have no common ancestor.")
            paths = analyze_change(session["path"], base_ref, head_ref, issue, output_dir, config_path, repo_label=session["repo_label"])
            contents = {key: path.read_text(encoding="utf-8") for key, path in paths.items()}
            return {"paths": paths, "contents": contents}

        self.analyze_button.configure(state="disabled")
        self._run_async(run, self._show_report, f"Analyzing {issue['key']} · {base_ref} → {head_ref}…")

    def _show_report(self, result: dict[str, Any]) -> None:
        self.report_paths = result["paths"]
        for key, text in result["contents"].items():
            if key not in self.result_text:
                continue
            widget = self.result_text[key]
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text)
            widget.configure(state="disabled")
        self.open_report_button.configure(state="normal")
        self.analyze_button.configure(state="normal")
        self.status.set(f"Analysis complete · report saved to {self.report_paths['markdown']}")
        self.notebook.select(0)
        if self.repo_session:
            self.repo_session["tempdir"].cleanup()
            self.repo_session = None

    def _open_report(self) -> None:
        path = self.report_paths.get("markdown")
        if not path or not path.exists():
            messagebox.showerror("Report missing", "The Markdown report file is not available.", parent=self.root)
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open report", str(exc), parent=self.root)

    def _run_async(self, work: Callable[[], Any], on_success: Callable[[Any], None], busy_text: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.status.set(busy_text)
        self.load_button.configure(state="disabled")
        self.inspect_button.configure(state="disabled")
        self.analyze_button.configure(state="disabled")
        self.root.configure(cursor="watch")

        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, self._finish_error, str(exc))
            else:
                self.root.after(0, self._finish_success, on_success, result)

        self.executor.submit(run)

    def _finish_success(self, callback: Callable[[Any], None], result: Any) -> None:
        self._busy = False
        self.root.configure(cursor="")
        self.load_button.configure(state="normal")
        if self.mode.get() == "jira" and self.issues:
            self.inspect_button.configure(state="normal")
        callback(result)

    def _finish_error(self, message: str) -> None:
        self._busy = False
        self.root.configure(cursor="")
        self.load_button.configure(state="normal")
        self.inspect_button.configure(state="normal" if self.mode.get() == "jira" and self.issues else "disabled")
        self.analyze_button.configure(state="normal" if self.repo_session else "disabled")
        self.status.set("Operation failed · see error details")
        messagebox.showerror("CC/DC operation failed", message, parent=self.root)

    def _close(self) -> None:
        if self.repo_session:
            self.repo_session["tempdir"].cleanup()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main() -> int:
    if tk is None:
        print("ccdc-gui requires Tkinter, which is missing from this Python installation. On Debian/Ubuntu, install python3-tk; on Windows, use the standard python.org installer.", file=sys.stderr)
        return 2
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
