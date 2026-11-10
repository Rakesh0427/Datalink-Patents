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
        ttk.Label(self.git_frame, text="SCR key in branch / commit (for example SCR-123)").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.git_frame, textvariable=self.scr_key, width=32).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(self.git_frame, text="Git metadata is enough to locate matching branches and commits; Jira is not queried.").grid(
            row=1, column=1, sticky="w", padx=(12, 0)
        )

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

        self.issue_frame = ttk.LabelFrame(outer, text="2 · Jira issues (Jira mode only)", style="Section.TLabelframe", padding=8)
        self.issue_frame.pack(fill="x", pady=(10, 0))
        self.issue_list = tk.Listbox(self.issue_frame, height=4, activestyle="dotbox", exportselection=False)
        self.issue_list.pack(side="left", fill="x", expand=True)
        issue_scroll = ttk.Scrollbar(self.issue_frame, orient="vertical", command=self.issue_list.yview)
        issue_scroll.pack(side="right", fill="y")
        self.issue_list.configure(yscrollcommand=issue_scroll.set)
        self.inspect_button = ttk.Button(self.issue_frame, text="Inspect selected Jira SCR", command=self._inspect_selected, state="disabled")
        self.inspect_button.pack(side="right", padx=(8, 0), fill="y")

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
            self.issue_frame.pack_forget()
            self.git_frame.grid()
            self.load_button.configure(text="Inspect SCR in Git")
            self.inspect_button.configure(state="disabled")
            self.status.set("Git-only mode · no Jira request or Jira credentials needed.")
        else:
            self.git_frame.grid_remove()
            self.jira_frame.grid()
            self.issue_frame.pack(fill="x", pady=(10, 0), before=self.refs_frame)
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
            issue_key = self.scr_key.get().strip().upper()
            if not ISSUE_KEY_RE.fullmatch(issue_key):
                messagebox.showerror("SCR key required", "Enter a valid key such as SCR-123.", parent=self.root)
                return
            issue = {"key": issue_key, "summary": "SCR identified from Git", "updated": "Git only", "status": "Jira not queried", "url": None}
            self._start_repo_inspection(repo_value, issue)
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

    def _show_issues(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.issue_list.delete(0, tk.END)
        for issue in issues:
            summary = issue.get("summary") or "(No summary)"
            status = issue.get("status") or issue.get("issue_type") or ""
            self.issue_list.insert(tk.END, f"{issue.get('key', '')}  ·  {summary}  ·  {status}")
        self.inspect_button.configure(state="normal" if issues else "disabled")
        self.status.set(f"Loaded {len(issues)} Jira issue(s). Select one, then inspect its Git refs.")
        if not issues:
            messagebox.showinfo("No issues found", "No recent Jira issues matched the selected project and date range.", parent=self.root)

    def _inspect_selected(self) -> None:
        selection = self.issue_list.curselection()
        if not selection:
            messagebox.showinfo("Select an SCR", "Choose an issue from the list first.", parent=self.root)
            return
        self._start_repo_inspection(self.repo_value.get().strip(), self.issues[selection[0]])

    def _start_repo_inspection(self, repo_value: str, issue: dict[str, Any]) -> None:
        if not repo_value:
            messagebox.showerror("Repository required", "Enter a Git URL or choose a local Git folder.", parent=self.root)
            return
        self.selected_issue = issue
        self.analyze_button.configure(state="disabled")
        self._run_async(lambda: self._inspect_repo(repo_value, issue), self._show_repo, f"Cloning/fetching Git repository for {issue['key']}…")

    @staticmethod
    def _inspect_repo(repo_value: str, issue: dict[str, Any]) -> dict[str, Any]:
        tempdir = tempfile.TemporaryDirectory(prefix="ccdc-desktop-")
        repo = Path(tempdir.name) / "repo"
        try:
            _clone_repo(repo_value, repo)
            refs = sorted({ref for ref in _git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", "refs/heads"], repo, check=False).splitlines() if ref and not ref.endswith("/HEAD")})
            key = issue["key"].upper()
            branches = [ref for ref in refs if key.lower() in ref.lower()]
            log = _git(["log", "--all", "--regexp-ignore-case", f"--grep={key}", "--format=%H%x09%s"], repo, check=False)
            commits = []
            for line in log.splitlines()[:50]:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    commits.append({"ref": parts[0], "subject": parts[1]})
            return {"tempdir": tempdir, "path": repo, "repo_label": repo_value, "refs": refs, "branches": branches, "commits": commits, "default_base": _default_base(repo), "issue": issue}
        except Exception:
            tempdir.cleanup()
            raise

    def _show_repo(self, session: dict[str, Any]) -> None:
        if self.repo_session:
            self.repo_session["tempdir"].cleanup()
        self.repo_session = session
        base_choices = list(dict.fromkeys([session["default_base"], *session["refs"]]))
        head_choices = list(dict.fromkeys(["HEAD", *session["branches"], *(item["ref"] for item in session["commits"]), *session["refs"]]))
        self.base_combo.configure(values=base_choices)
        self.head_combo.configure(values=head_choices)
        self.base_ref.set(session["default_base"])
        self.head_ref.set(session["branches"][0] if session["branches"] else (session["commits"][0]["ref"] if session["commits"] else "HEAD"))
        match_count = len(session["branches"]) + len(session["commits"])
        if match_count:
            self.status.set(f"Found {len(session['branches'])} matching branch(es) and {len(session['commits'])} matching commit(s). Verify base/head before analysis.")
        else:
            self.status.set(f"No branch or commit mentions {session['issue']['key']}; select the SCR change range manually.")
        self.analyze_button.configure(state="normal")

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
