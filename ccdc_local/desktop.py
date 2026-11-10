from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import sys
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from math import sqrt
from pathlib import Path
from typing import Any, Callable

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:  # Tkinter ships with python.org Python on Windows/macOS; Linux may package it separately.
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = ttk = None  # type: ignore[assignment]

from .analyzer import ProgressCallback, analyze_change, render_mermaid_png
from .cli import _clone_repo, _default_base, _git
from .jira import JiraClient

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
ISSUE_KEY_SCAN_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b", re.IGNORECASE)
MAX_FLOW_RENDER_PIXELS = 24_000_000
MAX_FLOW_RENDER_WIDTH = 8_000
MAX_FLOW_RENDER_HEIGHT = 6_000


def discover_git_issues(
    refs: list[str],
    commits: list[dict[str, str]],
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, list[Any]]]]:
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
    total_commits = len(commits)
    update_every = max(1, total_commits // 40)
    for index, commit in enumerate(commits, start=1):
        for key in ISSUE_KEY_SCAN_RE.findall(commit.get("subject", "")):
            bucket = ensure(key)["commits"]
            if commit not in bucket:
                bucket.append(commit)
        if progress and (index == total_commits or index % update_every == 0):
            progress(index, total_commits)

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


def resolve_scr_range(
    session: dict[str, Any],
    issue: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> tuple[dict[str, str] | None, str]:
    """Infer a safe base/head pair from SCR-named refs or matching commit history."""
    key = issue.get("key", "").upper()
    repo = session["path"]
    matches = session.get("issue_matches", {}).get(key, {"branches": [], "commits": []})
    branches = list(dict.fromkeys(matches.get("branches", [])))
    commits = matches.get("commits", [])
    default_base = session.get("default_base", "HEAD")
    if progress:
        progress(None, f"Matching {key} to a Git branch or commit range…")

    base_sha = _git(["rev-parse", "--verify", f"{default_base}^{{commit}}"], repo, check=False)
    branch_candidates = []
    for branch in branches:
        tip_sha = _git(["rev-parse", "--verify", f"{branch}^{{commit}}"], repo, check=False)
        if not tip_sha or tip_sha == base_sha:
            continue
        common = _git(["merge-base", default_base, branch], repo, check=False)
        if not common or common == tip_sha:
            continue  # Already contained in the default branch; use the commit range below.
        timestamp = _git(["log", "-1", "--format=%ct", branch], repo, check=False)
        try:
            commit_time = int(timestamp)
        except ValueError:
            commit_time = 0
        branch_candidates.append((commit_time, branch))
    if branch_candidates:
        _, head_ref = max(branch_candidates)
        resolved = {
            "base_ref": default_base,
            "head_ref": head_ref,
            "strategy": "SCR-matching branch against the repository default branch",
            "evidence": f"Branch `{head_ref}` contains {key}.",
        }
        if progress:
            progress(None, f"Using {head_ref} against {default_base}; checking out the SCR-matching branch…")
        return resolved, ""

    # Commit-subject matching is safe only when SCR-tagged commits form one
    # uninterrupted first-parent sequence. Otherwise a range would silently
    # include unrelated commits between matching messages.
    if commits:
        head_ref = commits[0]["ref"]
        head_sha = _git(["rev-parse", "--verify", f"{head_ref}^{{commit}}"], repo, check=False)
        matching_shas = {item["ref"] for item in commits}
        history = _git(
            ["log", "--first-parent", "--format=%H%x09%P%x09%s", head_sha],
            repo,
            check=False,
        ).splitlines() if head_sha else []
        history_parts = [line.split("\t", 2) for line in history]
        matching_positions = [
            index for index, parts in enumerate(history_parts)
            if parts[0] in matching_shas
        ]
        if matching_positions:
            oldest_index = max(matching_positions)
            segment = history_parts[:oldest_index + 1]
            segment_has_only_scr_commits = all(
                len(parts) == 3 and key in {match.upper() for match in ISSUE_KEY_SCAN_RE.findall(parts[2])}
                for parts in segment
            )
            # A merge introduces side-branch changes not represented by its
            # first-parent subject; leave those ranges for explicit review.
            contains_merge = any(len(parts[1].split()) > 1 for parts in segment if len(parts) == 3)
            oldest = segment[-1][0]
            parent_refs = segment[-1][1].split() if len(segment[-1]) == 3 else []
            if segment_has_only_scr_commits and not contains_merge and len(parent_refs) == 1:
                base_ref = parent_refs[0]
                if _git(["merge-base", base_ref, head_ref], repo, check=False):
                    commit_count = len(segment)
                    resolved = {
                        "base_ref": base_ref,
                        "head_ref": head_ref,
                        "strategy": "matching SCR commit range",
                        "evidence": f"{commit_count} contiguous {key} commit(s), from {oldest[:10]} through {head_ref[:10]}.",
                    }
                    if progress:
                        progress(None, f"Using the matching SCR commit range ({commit_count} contiguous commit(s))…")
                    return resolved, ""

    reason = (
        f"No safe Git range could be inferred for {key}. Matching commits are absent, non-contiguous, "
        "or involve a merge/root commit. Include the SCR key on every commit in the change or use the optional manual range."
    )
    return None, reason


def flow_node_layers(report: dict[str, Any]) -> dict[str, int]:

    """Place changed functions at the center and connected call-flow nodes around them."""
    edges = report.get("edges", [])
    nodes = set(report.get("changed_functions", [])) | set(report.get("impacted_functions", []))
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        caller, callee = edge.get("caller"), edge.get("callee")
        if not caller or not callee:
            continue
        nodes.update((caller, callee))
        forward[caller].add(callee)
        reverse[callee].add(caller)
    changed = set(report.get("changed_functions", []))
    if not nodes:
        return {}
    if not changed:
        return {node: index for index, node in enumerate(sorted(nodes))}

    layers = {node: 0 for node in changed}
    queue = deque((node, 0) for node in changed)
    while queue:
        node, distance = queue.popleft()
        for child in forward[node]:
            if child not in layers:
                layers[child] = distance + 1
                queue.append((child, distance + 1))
    queue = deque((node, 0) for node in changed)
    while queue:
        node, distance = queue.popleft()
        for parent in reverse[node]:
            if parent not in layers:
                layers[parent] = -(distance + 1)
                queue.append((parent, distance + 1))
    max_layer = max(layers.values(), default=0)
    for node in sorted(nodes - set(layers)):
        max_layer += 1
        layers[node] = max_layer
    shift = min(layers.values(), default=0)
    return {node: layer - shift for node, layer in layers.items()}


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
        self._progress_animation_running = False
        self._progress_reset_id: str | None = None
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

        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0)
        page_scroll = ttk.Scrollbar(shell, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=page_scroll.set)
        page_scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(self.canvas, padding=(18, 14))
        self.outer = outer
        self.canvas_window = self.canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_scrollable_page)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)
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
        self.progress_frame = ttk.Frame(source)
        self.progress_frame.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.progress_detail = tk.StringVar(value="")
        self.progress_percent = tk.StringVar(value="")
        ttk.Label(self.progress_frame, textvariable=self.progress_detail, anchor="w").grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.progress_bar = ttk.Progressbar(self.progress_frame, mode="indeterminate", maximum=100, length=280)
        self.progress_bar.grid(row=0, column=1, sticky="ew")
        ttk.Label(self.progress_frame, textvariable=self.progress_percent, width=8, anchor="e").grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.progress_frame.columnconfigure(0, weight=3)
        self.progress_frame.columnconfigure(1, weight=2)
        self.progress_frame.grid_remove()

        self.issue_frame = ttk.LabelFrame(outer, text="2 · SCRs and Git updates", style="Section.TLabelframe", padding=8)
        self.issue_frame.pack(fill="x", pady=(10, 0))
        self.repo_details = tk.StringVar(value="Load a repository to discover SCRs. Selecting Analyze will match the SCR to Git changes automatically.")
        ttk.Label(self.issue_frame, textvariable=self.repo_details).pack(anchor="w", pady=(0, 6))
        split = ttk.Panedwindow(self.issue_frame, orient="horizontal")
        split.pack(fill="x", expand=True)
        scr_panel = ttk.LabelFrame(split, text="SCRs to select", padding=5)
        updates_panel = ttk.LabelFrame(split, text="Recent Git updates", padding=5)
        split.add(scr_panel, weight=1)
        split.add(updates_panel, weight=2)
        issue_list_frame = ttk.Frame(scr_panel)
        issue_list_frame.pack(fill="both", expand=True)
        self.issue_list = tk.Listbox(issue_list_frame, height=5, activestyle="dotbox", exportselection=False)
        self.issue_list.pack(side="left", fill="both", expand=True)
        issue_scroll = ttk.Scrollbar(issue_list_frame, orient="vertical", command=self.issue_list.yview)
        issue_scroll.pack(side="right", fill="y")
        self.issue_list.configure(yscrollcommand=issue_scroll.set)
        self.issue_list.bind("<<ListboxSelect>>", self._on_issue_list_selection)
        self.issue_list.bind("<Double-Button-1>", lambda _event: self._inspect_selected())
        self.inspect_button = ttk.Button(scr_panel, text="Analyze selected SCR", command=self._inspect_selected, state="disabled")
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

        refs = ttk.LabelFrame(outer, text="Optional · Advanced manual range fallback", style="Section.TLabelframe", padding=10)
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
        self.range_notice = tk.StringVar(value="")
        ttk.Label(refs, textvariable=self.range_notice, wraplength=980, foreground="#8a5410").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        refs.columnconfigure(0, weight=1)
        refs.columnconfigure(1, weight=1)
        refs.pack_forget()

        results = ttk.LabelFrame(outer, text="3 · Impact report", style="Section.TLabelframe", padding=8)
        results.pack(fill="both", expand=True, pady=(10, 0))
        self.results_frame = results
        self.notebook = ttk.Notebook(results)
        self.notebook.pack(fill="both", expand=True)
        overview_page = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(overview_page, text="Overview")
        self.overview_summary = tk.StringVar(value="Run an analysis to see a concise impact summary.")
        ttk.Label(overview_page, textvariable=self.overview_summary, wraplength=1000, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        metric_frame = ttk.Frame(overview_page)
        metric_frame.pack(fill="x", pady=(0, 8))
        self.overview_metrics: dict[str, tk.StringVar] = {}
        for index, (key, label) in enumerate((
            ("changed_files", "Changed files"), ("changed_functions", "Changed functions"),
            ("impacted_functions", "Impacted functions"), ("control_edges", "CC edges"), ("data_edges", "DC edges"),
        )):
            card = ttk.LabelFrame(metric_frame, text=label, padding=(8, 5))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 5, 0))
            value = tk.StringVar(value="—")
            self.overview_metrics[key] = value
            ttk.Label(card, textvariable=value, font=("Segoe UI", 16, "bold")).pack(anchor="w")
            metric_frame.columnconfigure(index, weight=1)
        overview_columns = ttk.Frame(overview_page)
        overview_columns.pack(fill="both", expand=True)
        trace_frame = ttk.LabelFrame(overview_columns, text="Requirements and recommended tests", padding=6)
        trace_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        review_frame = ttk.LabelFrame(overview_columns, text="Review notes and assurance limits", padding=6)
        review_frame.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.overview_trace_text = tk.Text(trace_frame, wrap="word", height=12, font=("Segoe UI", 9), state="disabled")
        trace_scroll = ttk.Scrollbar(trace_frame, orient="vertical", command=self.overview_trace_text.yview)
        self.overview_trace_text.configure(yscrollcommand=trace_scroll.set)
        self.overview_trace_text.grid(row=0, column=0, sticky="nsew")
        trace_scroll.grid(row=0, column=1, sticky="ns")
        trace_frame.rowconfigure(0, weight=1)
        trace_frame.columnconfigure(0, weight=1)
        self.overview_review_text = tk.Text(review_frame, wrap="word", height=12, font=("Segoe UI", 9), state="disabled")
        review_scroll = ttk.Scrollbar(review_frame, orient="vertical", command=self.overview_review_text.yview)
        self.overview_review_text.configure(yscrollcommand=review_scroll.set)
        self.overview_review_text.grid(row=0, column=0, sticky="nsew")
        review_scroll.grid(row=0, column=1, sticky="ns")
        review_frame.rowconfigure(0, weight=1)
        review_frame.columnconfigure(0, weight=1)

        flow_page = ttk.Frame(self.notebook)
        self.notebook.add(flow_page, text="Rendered Mermaid flow")
        self.flow_report: dict[str, Any] | None = None
        self.flow_png_path: Path | None = None
        self.flow_mermaid_source: str | None = None
        self.flow_zoom = 1.0
        self._flow_render_request = 0
        self.flow_photo: Any = None
        self.flow_image_original: Any = None
        flow_legend = ttk.Frame(flow_page, padding=(8, 5))
        flow_legend.pack(fill="x")
        ttk.Label(flow_legend, text="Changed", foreground="#b4232c").pack(side="left", padx=(0, 10))
        ttk.Label(flow_legend, text="Impacted", foreground="#1f5da8").pack(side="left", padx=(0, 10))
        ttk.Label(flow_legend, text="Edge labels: CC = Control Coupling · DC = Data Coupling").pack(side="left")
        flow_zoom_controls = ttk.Frame(flow_legend)
        flow_zoom_controls.pack(side="right")
        ttk.Button(flow_zoom_controls, text="Fit", command=self._fit_flow).pack(side="left", padx=2)
        ttk.Button(flow_zoom_controls, text="100%", command=self._actual_flow_size).pack(side="left", padx=2)
        ttk.Button(flow_zoom_controls, text="−", width=3, command=lambda: self._zoom_flow(0.8)).pack(side="left", padx=2)
        ttk.Button(flow_zoom_controls, text="+", width=3, command=lambda: self._zoom_flow(1.25)).pack(side="left", padx=2)
        self.flow_zoom_label = tk.StringVar(value="Fit")
        ttk.Label(flow_zoom_controls, textvariable=self.flow_zoom_label, width=5, anchor="center").pack(side="left", padx=(5, 0))
        self.flow_status = tk.StringVar(value="Run an analysis to render the Mermaid call and coupling flow.")
        ttk.Label(flow_page, textvariable=self.flow_status, padding=(8, 3), wraplength=1000).pack(anchor="w")
        flow_body = ttk.Frame(flow_page)
        flow_body.pack(fill="both", expand=True)
        self.flow_canvas = tk.Canvas(flow_body, background="white", highlightthickness=0, width=900, height=430)
        flow_xscroll = ttk.Scrollbar(flow_body, orient="horizontal", command=self.flow_canvas.xview)
        flow_yscroll = ttk.Scrollbar(flow_body, orient="vertical", command=self.flow_canvas.yview)
        self.flow_canvas.configure(xscrollcommand=flow_xscroll.set, yscrollcommand=flow_yscroll.set)
        self.flow_canvas.grid(row=0, column=0, sticky="nsew")
        flow_yscroll.grid(row=0, column=1, sticky="ns")
        flow_xscroll.grid(row=1, column=0, sticky="ew")
        flow_body.rowconfigure(0, weight=1)
        flow_body.columnconfigure(0, weight=1)
        self.flow_canvas.bind("<MouseWheel>", self._on_flow_mousewheel)
        self.flow_canvas.bind("<Button-4>", self._on_flow_mousewheel)
        self.flow_canvas.bind("<Button-5>", self._on_flow_mousewheel)

        edges_page = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(edges_page, text="CC/DC edge details")
        self.edge_table = ttk.Treeview(edges_page, columns=("caller", "callee", "types", "details", "state", "scope", "location"), show="headings", height=12)
        for column, heading in (("caller", "CALLER"), ("callee", "CALLEE"), ("types", "CC/DC"), ("details", "EFFECT / INPUTS"), ("state", "CHANGE"), ("scope", "SCOPE"), ("location", "SOURCE")):
            self.edge_table.heading(column, text=heading)
        self.edge_table.column("caller", width=230, minwidth=140)
        self.edge_table.column("callee", width=230, minwidth=140)
        self.edge_table.column("types", width=100, minwidth=75, stretch=False)
        self.edge_table.column("details", width=300, minwidth=180)
        self.edge_table.column("state", width=90, minwidth=70, stretch=False)
        self.edge_table.column("scope", width=130, minwidth=100, stretch=False)
        self.edge_table.column("location", width=180, minwidth=120)
        edge_y = ttk.Scrollbar(edges_page, orient="vertical", command=self.edge_table.yview)
        edge_x = ttk.Scrollbar(edges_page, orient="horizontal", command=self.edge_table.xview)
        self.edge_table.configure(yscrollcommand=edge_y.set, xscrollcommand=edge_x.set)
        self.edge_table.grid(row=0, column=0, sticky="nsew")
        edge_y.grid(row=0, column=1, sticky="ns")
        edge_x.grid(row=1, column=0, sticky="ew")
        edges_page.rowconfigure(0, weight=1)
        edges_page.columnconfigure(0, weight=1)

        self.result_text: dict[str, tk.Text] = {}
        for title, key in (("Markdown report", "markdown"), ("Source diff", "diff"), ("Mermaid source", "mermaid"), ("JSON", "json")):
            page = ttk.Frame(self.notebook)
            self.notebook.add(page, text=title)
            text = tk.Text(page, wrap="word" if key == "markdown" else "none", height=9, font=("Consolas", 9), undo=False)
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

    def _update_scrollregion(self, _event: Any = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_scrollable_page(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> str | None:
        widget = event.widget
        if isinstance(widget, (tk.Listbox, tk.Text, tk.Canvas, ttk.Treeview, ttk.Combobox)):
            return None
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(1, "units")
        elif getattr(event, "delta", 0):
            self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def _on_flow_mousewheel(self, event: tk.Event) -> str:
        if getattr(event, "num", None) == 4:
            self.flow_canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.flow_canvas.yview_scroll(1, "units")
        elif getattr(event, "delta", 0):
            self.flow_canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def _scroll_to(self, widget: tk.Widget) -> None:
        self.root.update_idletasks()
        page_height = max(1, self.outer.winfo_height())
        target_y = max(0, widget.winfo_rooty() - self.outer.winfo_rooty())
        self.canvas.yview_moveto(min(1.0, target_y / page_height))

    def _on_issue_list_selection(self, _event: Any = None) -> None:
        if self._busy:
            return
        selection = self.issue_list.curselection()
        if not selection:
            return
        issue = self.issues[selection[0]]
        self.selected_issue = issue
        self.refs_frame.pack_forget()
        self.inspect_button.configure(text="Analyze selected SCR", state="normal")
        if self.mode.get() == "git":
            if not self.repo_session:
                return
            matches = self.repo_session.get("issue_matches", {}).get(issue["key"].upper(), {"branches": [], "commits": []})
            if matches["branches"] or matches["commits"]:
                self.status.set(f"{issue['key']} matches Git history. Automatically selecting its change range and starting CC/DC analysis…")
            else:
                self.status.set(f"No exact Git reference for {issue['key']}; attempting safe automatic matching before offering a manual fallback…")
            self._start_selected_analysis(issue)
        else:
            self.status.set(f"Loading repository and automatically analyzing selected Jira SCR {issue['key']}…")
            self._start_repo_inspection(self.repo_value.get().strip(), issue)

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
        self.selected_issue = None
        self._fill_issue_list(issues)
        self._show_updates([])
        self.manual_frame.pack_forget()
        self.repo_details.set(f"Jira returned {len(issues)} issue(s). Selecting an SCR loads its repository and starts analysis automatically.")
        self.inspect_button.configure(text="Analyze selected SCR", state="normal" if issues else "disabled")
        self.refs_frame.pack_forget()
        self.status.set(f"Loaded {len(issues)} Jira issue(s). Select an SCR to automatically load its repository, infer the range, and run CC/DC analysis.")
        self._scroll_to(self.issue_frame)
        if not issues:
            messagebox.showinfo("No issues found", "No recent Jira issues matched the selected project and date range.", parent=self.root)

    def _inspect_selected(self) -> None:
        selection = self.issue_list.curselection()
        if selection:
            issue = self.issues[selection[0]]
        else:
            issue = self.selected_issue
        if not issue:
            messagebox.showinfo("Select an SCR", "Choose an SCR from the list or enter a manual SCR key first.", parent=self.root)
            return
        self.selected_issue = issue
        self.refs_frame.pack_forget()
        if self.mode.get() == "git":
            if self.repo_session:
                self._start_selected_analysis(issue)
            return
        self._start_repo_inspection(self.repo_value.get().strip(), issue)

    def _start_git_discovery(self, repo_value: str) -> None:
        self.selected_issue = None
        self.analyze_button.configure(state="disabled")
        self._run_async(
            lambda: self._inspect_repo(repo_value, self._report_progress),
            self._show_git_repository,
            "Cloning/fetching repository and scanning branches + recent commits…",
            completion_text="Repository scan complete · refs, commits, and SCR keys are loaded.",
        )

    def _start_repo_inspection(self, repo_value: str, issue: dict[str, Any]) -> None:
        if not repo_value:
            messagebox.showerror("Repository required", "Enter a Git URL or choose a local Git folder.", parent=self.root)
            return
        self.selected_issue = issue
        if self.repo_session and self.repo_session.get("repo_label") == repo_value:
            self._render_repository_details(self.repo_session)
            self._start_selected_analysis(issue)
            return
        self.analyze_button.configure(state="disabled")
        self._run_async(
            lambda: self._inspect_repo(repo_value, self._report_progress),
            lambda session: self._show_jira_repository(session, issue),
            f"Loading repository details for {issue['key']}…",
            completion_text=f"Repository details ready for {issue['key']}.",
        )

    @staticmethod
    def _inspect_repo(repo_value: str, progress: ProgressCallback | None = None) -> dict[str, Any]:
        def report(value: float | None, message: str) -> None:
            if progress:
                progress(value, message)

        tempdir = tempfile.TemporaryDirectory(prefix="ccdc-desktop-")
        repo = Path(tempdir.name) / "repo"
        try:
            report(None, "Cloning/fetching the repository and receiving Git objects…")
            _clone_repo(repo_value, repo)
            report(52, "Repository data received. Enumerating local and remote refs…")
            refs = sorted({
                ref for ref in _git(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", "refs/heads"], repo, check=False).splitlines()
                if ref and not ref.endswith("/HEAD")
            })
            report(57, f"Found {len(refs)} refs. Reading up to 5,000 recent commit subjects…")
            report(None, "Git is collecting recent commit metadata…")
            raw_log = _git(
                ["log", "--all", "--max-count=5000", "--date=short", "--format=%H%x09%ad%x09%s"],
                repo,
                check=False,
            )
            log_lines = raw_log.splitlines()
            total_lines = len(log_lines)
            commits = []
            update_every = max(1, total_lines // 35)
            for index, line in enumerate(log_lines, start=1):
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    commits.append({"ref": parts[0], "short": parts[0][:10], "date": parts[1], "subject": parts[2]})
                if total_lines and (index == total_lines or index % update_every == 0):
                    percent = 59 + 15 * index / total_lines
                    report(percent, f"Reading commit list: {index}/{total_lines} entries")

            if total_lines:
                report(74, f"Checking {len(commits)} commit subjects and {len(refs)} refs for SCR keys…")
                issues, issue_matches = discover_git_issues(
                    refs,
                    commits,
                    progress=lambda done, total: report(
                        74 + 17 * done / max(1, total),
                        f"Scanning commit messages for SCR keys: {done}/{total}",
                    ),
                )
            else:
                report(91, "No commits to scan; checking branch names for SCR keys…")
                issues, issue_matches = discover_git_issues(refs, commits)
            if not refs and not commits:
                raise RuntimeError("Git clone completed, but the repository has no branches or commits to inspect. Verify that this is a non-empty Git repository.")
            latest = commits[0] if commits else {"subject": "No commits found", "date": ""}
            report(96, "Selecting the default base ref and preparing repository details…")
            default_base = _default_base(repo)
            report(99, f"Preparing the recent-update list ({min(100, len(commits))} displayed commits)…")
            return {
                "tempdir": tempdir,
                "path": repo,
                "repo_label": repo_value,
                "refs": refs,
                "commits": commits,
                "recent_commits": commits[:100],
                "discovered_issues": issues,
                "issue_matches": issue_matches,
                "default_base": default_base,
                "latest_commit": latest,
            }
        except Exception:
            tempdir.cleanup()
            raise

    def _replace_repository_session(self, session: dict[str, Any]) -> None:
        if self.repo_session and self.repo_session is not session:
            self.repo_session["tempdir"].cleanup()
        self.repo_session = session
        self._render_repository_details(session)

    def _render_repository_details(self, session: dict[str, Any]) -> None:
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
        self.inspect_button.configure(text="Analyze selected SCR", state="normal" if issues else "disabled")
        self.refs_frame.pack_forget()
        if issues:
            self.manual_frame.pack_forget()
            self.status.set(f"Found {len(issues)} SCR key(s). Select an SCR to infer its Git range and start CC/DC analysis automatically; the button can re-run it.")
        else:
            self.manual_frame.pack(fill="x", pady=(6, 0))
            self.status.set("Repository loaded, but no SCR-like key was found in its branches or recent commit messages.")
        self._scroll_to(self.issue_frame)

    def _show_jira_repository(self, session: dict[str, Any], issue: dict[str, Any]) -> None:
        self._replace_repository_session(session)
        self.selected_issue = issue
        self.inspect_button.configure(state="disabled")
        self.root.after(80, self._start_selected_analysis, issue)

    def _show_issue_refs(self, session: dict[str, Any], issue: dict[str, Any], reason: str) -> None:
        matches = session["issue_matches"].get(issue["key"].upper(), {"branches": [], "commits": []})
        branches = matches["branches"]
        commits = matches["commits"]
        commit_refs = [item["ref"] for item in commits]
        base_choices = list(dict.fromkeys([session["default_base"], *session["refs"]]))
        recent_commit_refs = [item["ref"] for item in session.get("recent_commits", [])]
        head_choices = list(dict.fromkeys([*branches, *commit_refs, "HEAD", *session["refs"], *recent_commit_refs]))
        self.base_combo.configure(values=base_choices)
        self.head_combo.configure(values=head_choices)
        self.base_ref.set(session["default_base"])
        self.head_ref.set(branches[0] if branches else (commit_refs[0] if commit_refs else "HEAD"))
        self.range_notice.set(f"{reason} Set a base and head only if you know which Git change belongs to this SCR.")
        self.refs_frame.pack(fill="x", pady=(10, 0), before=self.results_frame)
        self.analyze_button.configure(text="Analyze this manual range", state="normal")
        self.inspect_button.configure(state="disabled")
        self.status.set(f"Automatic range matching could not safely identify {issue['key']}; the optional manual fallback is shown below.")
        self._scroll_to(self.refs_frame)

    def _use_manual_key(self) -> None:
        if not self.repo_session:
            return
        key = self.scr_key.get().strip().upper()
        if not ISSUE_KEY_RE.fullmatch(key):
            messagebox.showerror("Invalid SCR key", "Enter a valid key such as SCR-123.", parent=self.root)
            return
        issue = {"key": key, "summary": "Manually identified SCR", "updated": "Git only", "status": "Jira not queried", "url": None}
        self.selected_issue = issue
        self.refs_frame.pack_forget()
        self.inspect_button.configure(text="Analyze selected SCR", state="normal")
        self.status.set(f"{key} selected. Automatically matching its Git changes and starting CC/DC analysis…")
        self._start_selected_analysis(issue)

    def _analyze(self) -> None:
        """Run the optional manual range override."""
        if not self.repo_session or not self.selected_issue:
            messagebox.showerror("SCR required", "Select an SCR before analyzing.", parent=self.root)
            return
        base_ref = self.base_ref.get().strip()
        head_ref = self.head_ref.get().strip()
        if not base_ref or not head_ref:
            messagebox.showerror("Git refs required", "Choose both a base and head ref.", parent=self.root)
            return
        config_value = self.config_path.get().strip()
        config_path = Path(config_value).expanduser() if config_value else None
        session, issue = self.repo_session, self.selected_issue
        self.analyze_button.configure(state="disabled")
        self._run_async(
            lambda: self._analysis_work(session, issue, base_ref, head_ref, config_path),
            self._show_report,
            f"Analyzing {issue['key']} · {base_ref} → {head_ref}…",
            completion_text="Manual-range analysis complete · report and rendered Mermaid diagram are ready.",
        )

    def _start_selected_analysis(self, issue: dict[str, Any]) -> None:
        """Resolve the selected SCR to Git refs, then run analysis without asking for a range."""
        if self._busy:
            return
        if not self.repo_session:
            self._start_repo_inspection(self.repo_value.get().strip(), issue)
            return
        session = self.repo_session
        self.selected_issue = issue
        self.refs_frame.pack_forget()
        config_value = self.config_path.get().strip()
        config_path = Path(config_value).expanduser() if config_value else None

        def run() -> dict[str, Any]:
            mapping, reason = resolve_scr_range(session, issue, self._report_progress)
            if mapping is None:
                return {"manual_range_required": True, "reason": reason}
            self._report_progress(None, f"Matched {issue['key']} to {mapping['strategy']}; starting CC/DC analysis…")
            analysis = self._analysis_work(
                session, issue, mapping["base_ref"], mapping["head_ref"], config_path
            )
            return {"manual_range_required": False, "mapping": mapping, "analysis": analysis}

        def finish(payload: dict[str, Any]) -> None:
            if payload["manual_range_required"]:
                self._show_issue_refs(session, issue, payload["reason"])
                return
            mapping = payload["mapping"]
            self.base_ref.set(mapping["base_ref"])
            self.head_ref.set(mapping["head_ref"])
            self._show_report(payload["analysis"])
            self.status.set(
                f"Analysis complete · auto-matched {issue['key']} via {mapping['strategy']}: "
                f"{mapping['base_ref']} → {mapping['head_ref']}"
            )

        self._run_async(
            run,
            finish,
            f"Matching {issue['key']} to Git changes and running CC/DC analysis…",
            completion_text=lambda payload: (
                f"No safe automatic range for {issue['key']} · choose the optional manual range below."
                if payload["manual_range_required"]
                else f"Analysis complete · {issue['key']} report and Mermaid diagram are ready."
            ),
        )

    def _analysis_work(
        self,
        session: dict[str, Any],
        issue: dict[str, Any],
        base_ref: str,
        head_ref: str,
        config_path: Path | None,
    ) -> dict[str, Any]:
        self._report_progress(None, f"Verifying base ref {base_ref}…")
        if not _git(["rev-parse", "--verify", base_ref], session["path"], check=False):
            raise RuntimeError(f"Git base ref was not found: {base_ref}")
        self._report_progress(None, f"Base ref verified. Checking head ref {head_ref}…")
        if not _git(["rev-parse", "--verify", head_ref], session["path"], check=False):
            raise RuntimeError(f"Git head ref was not found: {head_ref}")
        self._report_progress(None, "Both refs exist. Checking their common ancestor…")
        if not _git(["merge-base", base_ref, head_ref], session["path"], check=False):
            raise RuntimeError("Base and head refs have no common ancestor.")
        self._report_progress(None, "Refs are ready. Beginning C/C++ impact analysis…")
        output_dir = Path.cwd() / "ccdc-reports" / issue["key"]
        paths = analyze_change(
            session["path"], base_ref, head_ref, issue, output_dir, config_path,
            repo_label=session["repo_label"], progress=self._report_progress,
        )
        self._report_progress(99, "Loading Markdown, JSON, source diff, and Mermaid source into the report tabs…")
        contents = {key: paths[key].read_text(encoding="utf-8") for key in ("markdown", "diff", "mermaid", "json")}
        report = json.loads(contents["json"])
        self._report_progress(None, "Decoding the generated Mermaid PNG for the diagram tab…")
        from PIL import Image

        with Image.open(paths["mermaid_png"]) as source_image:
            flow_image = source_image.convert("RGB")
        return {"paths": paths, "contents": contents, "report": report, "flow_image": flow_image}

    @staticmethod
    def _replace_text(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _populate_report_overview(self, report: dict[str, Any]) -> None:
        issue = report.get("issue", {})
        self.overview_summary.set(
            f"{issue.get('key', 'SCR')} · {issue.get('summary') or 'Git change impact'}  |  "
            f"{report.get('base_ref', '?')} → {report.get('head_ref', '?')}"
        )
        edges = report.get("edges", [])
        control_count = sum(bool(edge.get("control_coupling") or edge.get("control_context") or "control" in edge.get("coupling_types", [])) for edge in edges)
        data_count = sum(bool(edge.get("data_coupling") or "data" in edge.get("coupling_types", [])) for edge in edges)
        metrics = {
            "changed_files": len(report.get("changed_files", [])),
            "changed_functions": len(report.get("changed_functions", [])),
            "impacted_functions": len(report.get("impacted_functions", [])),
            "control_edges": control_count,
            "data_edges": data_count,
        }
        for key, value in metrics.items():
            self.overview_metrics[key].set(str(value))

        trace_lines = ["AFFECTED REQUIREMENTS"]
        reqs = report.get("affected_requirements", {})
        if reqs:
            trace_lines.extend(f"• {key}: {', '.join(symbols)}" for key, symbols in reqs.items())
        else:
            trace_lines.append("• No configured requirement mapping matched the impact cone.")
        trace_lines.extend(("", "RECOMMENDED TESTS"))
        tests = report.get("recommended_tests", {})
        if tests:
            trace_lines.extend(f"• {name} (requirements: {', '.join(requirements)})" for name, requirements in tests.items())
        else:
            trace_lines.append("• No tests are mapped. Select regression tests using the change and impact list.")
        self._replace_text(self.overview_trace_text, "\n".join(trace_lines))

        notes = []
        if not report.get("changed_files"):
            notes.append("No C/C++ files were changed in this Git range.")
        notes.extend(f"Parse: {item}" for item in report.get("parse_warnings", []))
        notes.extend(f"Unresolved: {item}" for item in report.get("unresolved_calls", []))
        notes.extend(report.get("limitations", []))
        if not notes:
            notes.append("No parser or resolution warnings were reported. Review the detected flows against the design and test evidence.")
        notes.append("Static heuristic results are not DO-178B/C certification evidence by themselves.")
        self._replace_text(self.overview_review_text, "\n".join(f"• {item}" for item in notes))

    def _populate_edge_table(self, report: dict[str, Any]) -> None:
        for item in self.edge_table.get_children():
            self.edge_table.delete(item)
        for edge in report.get("edges", []):
            kinds = edge.get("coupling_types", [])
            labels = []
            if "control" in kinds or edge.get("control_coupling") or edge.get("control_context"):
                labels.append("CC · Control")
            if "data" in kinds or edge.get("data_coupling"):
                labels.append("DC · Data")
            effects = []
            if edge.get("control_coupling"):
                effects.append("CC: " + ", ".join(edge["control_coupling"]))
            if edge.get("data_coupling"):
                effects.append("DC: " + ", ".join(edge["data_coupling"]))
            if edge.get("control_context"):
                effects.append("Call under conditional/loop")
            self.edge_table.insert("", "end", values=(
                edge.get("caller", ""), edge.get("callee", ""), ", ".join(labels) or "Call only",
                " | ".join(effects) or "No mapped parameter coupling",
                edge.get("state", ""), "Inter-component" if edge.get("cross_component") else "Intra-component",
                f"{edge.get('path', '')}:{edge.get('line', '')}",
            ))

    def _fit_flow(self) -> None:
        if self.flow_report is not None and self.flow_png_path is not None:
            self._draw_flow(self.flow_report, fit=True)

    def _actual_flow_size(self) -> None:
        if self.flow_report is not None and self.flow_png_path is not None:
            self._draw_flow(self.flow_report, zoom=1.0)

    def _zoom_flow(self, factor: float) -> None:
        if self.flow_report is not None and self.flow_png_path is not None:
            self._draw_flow(self.flow_report, zoom=self.flow_zoom * factor)

    def _draw_flow(
        self,
        report: dict[str, Any],
        image_path: Path | None = None,
        image: Any = None,
        fit: bool = False,
        zoom: float | None = None,
    ) -> None:
        from PIL import Image, ImageTk

        self.flow_report = report
        if image_path is not None:
            self.flow_png_path = image_path
        canvas = self.flow_canvas
        canvas.delete("all")
        if self.flow_png_path is None or not self.flow_png_path.exists():
            message = "Rendered Mermaid PNG is missing. Re-run the analysis to generate the diagram."
            canvas.create_text(450, 200, text=message, fill="#9b2633", font=("Segoe UI", 12), width=760)
            canvas.configure(scrollregion=(0, 0, 900, 430))
            self.flow_status.set(message)
            return

        if image is not None:
            self.flow_image_original = image
        elif self.flow_image_original is None or image_path is not None:
            with Image.open(self.flow_png_path) as source:
                self.flow_image_original = source.convert("RGB")
        original = self.flow_image_original
        self.root.update_idletasks()
        viewport_w = self.flow_canvas.winfo_width()
        viewport_h = self.flow_canvas.winfo_height()
        if viewport_w <= 2:
            viewport_w = max(700, self.root.winfo_width() - 100)
        if viewport_h <= 2:
            viewport_h = max(280, self.root.winfo_height() - 450)

        max_scale = min(
            4.0,
            MAX_FLOW_RENDER_WIDTH / original.width,
            MAX_FLOW_RENDER_HEIGHT / original.height,
            sqrt(MAX_FLOW_RENDER_PIXELS / (original.width * original.height)),
        )
        tall_flow = original.height / original.width >= 3.0
        if zoom is not None:
            scale = max(0.15, min(max_scale, zoom))
        elif fit and tall_flow:
            # For a tall graph, fit the width and allow vertical scrolling rather
            # than shrinking labels to unreadable text just to fit its full height.
            scale = min((viewport_w - 30) / original.width, max_scale)
        elif fit:
            scale = max(0.15, min((viewport_w - 30) / original.width, (viewport_h - 30) / original.height, 1.0))
        else:
            scale = self.flow_zoom
        self.flow_zoom = scale
        display_w = max(1, round(original.width * scale))
        display_h = max(1, round(original.height * scale))
        self._flow_render_request += 1
        request_id = self._flow_render_request
        resized = original.resize((display_w, display_h), Image.Resampling.LANCZOS)
        self.flow_photo = ImageTk.PhotoImage(resized, master=self.root)
        canvas.configure(scrollregion=(0, 0, display_w, display_h))
        canvas.create_image(0, 0, image=self.flow_photo, anchor="nw")
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)
        self.flow_zoom_label.set(f"{scale:.0%}")
        report_edges = report.get("edges", [])
        self.flow_status.set(
            f"Mermaid.js · {original.width} × {original.height}px source · viewing {display_w} × {display_h}px · "
            f"{len(report_edges)} analyzed edges. Diagram is summarized; Edge Details / JSON retain findings."
        )
        if self.flow_mermaid_source and (zoom is not None or (fit and tall_flow)):
            self.flow_status.set(f"Rendering a sharp Mermaid.js view at {display_w} × {display_h}px…")
            self.executor.submit(
                self._render_flow_zoom,
                request_id,
                self.flow_mermaid_source,
                display_w,
                display_h,
            )

    def _render_flow_zoom(self, request_id: int, source: str, width: int, height: int) -> None:
        try:
            png_bytes = render_mermaid_png(source, width, height)
            error = None
        except Exception as exc:
            png_bytes = None
            error = str(exc)
        try:
            self.root.after(0, self._finish_flow_zoom, request_id, source, width, height, png_bytes, error)
        except tk.TclError:
            pass

    def _finish_flow_zoom(
        self,
        request_id: int,
        source: str,
        width: int,
        height: int,
        png_bytes: bytes | None,
        error: str | None,
    ) -> None:
        if request_id != self._flow_render_request or source != self.flow_mermaid_source:
            return
        if error or png_bytes is None:
            detail = (error or "unknown rendering error").replace("\\n", " ")[:180]
            self.flow_status.set(f"High-resolution Mermaid render failed; showing the preview. {detail}")
            return
        from PIL import Image, ImageTk

        with Image.open(BytesIO(png_bytes)) as rendered:
            image = rendered.convert("RGB")
        if image.size != (width, height):
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        canvas = self.flow_canvas
        xview = canvas.xview()
        yview = canvas.yview()
        self.flow_photo = ImageTk.PhotoImage(image, master=self.root)
        canvas.delete("all")
        canvas.configure(scrollregion=(0, 0, width, height))
        canvas.create_image(0, 0, image=self.flow_photo, anchor="nw")
        if xview:
            canvas.xview_moveto(xview[0])
        if yview:
            canvas.yview_moveto(yview[0])
        edge_count = len((self.flow_report or {}).get("edges", []))
        self.flow_status.set(
            f"Sharp Mermaid.js render · {width} × {height}px · {edge_count} analyzed edges. "
            "Diagram is summarized; Edge Details / JSON retain findings."
        )

    def _show_report(self, result: dict[str, Any]) -> None:
        self.report_paths = result["paths"]
        self.flow_mermaid_source = result["contents"].get("mermaid")
        for key, text in result["contents"].items():
            if key not in self.result_text:
                continue
            widget = self.result_text[key]
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text)
            widget.configure(state="disabled")
        report = result["report"]
        self._populate_report_overview(report)
        self._populate_edge_table(report)
        self.notebook.select(1)
        self.root.update_idletasks()
        self._draw_flow(report, image_path=self.report_paths.get("mermaid_png"), image=result.get("flow_image"), fit=True)
        self.open_report_button.configure(state="normal")
        self.analyze_button.configure(state="normal")
        self.status.set(f"Analysis complete · report saved to {self.report_paths['markdown']}")
        self.notebook.select(0)
        if self.repo_session:
            self.repo_session["tempdir"].cleanup()
            self.repo_session = None
        self._scroll_to(self.results_frame)

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

    def _report_progress(self, percent: float | None, message: str) -> None:
        """Queue worker progress on Tk's main thread; None means work is not countable yet."""
        try:
            self.root.after(0, self._set_progress, percent, message)
        except tk.TclError:
            pass

    def _set_progress(self, percent: float | None, message: str) -> None:
        if not self.progress_frame.winfo_manager():
            self.progress_frame.grid()
        self.progress_detail.set(message)
        if percent is None:
            if self.progress_bar.cget("mode") != "indeterminate":
                self.progress_bar.stop()
                self._progress_animation_running = False
                self.progress_bar.configure(mode="indeterminate")
            if not self._progress_animation_running:
                self.progress_bar.start(12)
                self._progress_animation_running = True
            self.progress_percent.set("Working")
            return

        if self._progress_animation_running:
            self.progress_bar.stop()
            self._progress_animation_running = False
        if self.progress_bar.cget("mode") != "determinate":
            self.progress_bar.configure(mode="determinate")
        bounded = max(0.0, min(100.0, percent))
        self.progress_bar.configure(value=bounded)
        self.progress_percent.set(f"{bounded:.0f}%")

    def _begin_progress(self, message: str) -> None:
        if self._progress_reset_id is not None:
            try:
                self.root.after_cancel(self._progress_reset_id)
            except tk.TclError:
                pass
            self._progress_reset_id = None
        self.progress_frame.grid()
        self.progress_bar.stop()
        self._progress_animation_running = False
        self.progress_bar.configure(mode="indeterminate", value=0)
        self.progress_detail.set(message)
        self.progress_percent.set("Working")
        self.progress_bar.start(12)
        self._progress_animation_running = True

    def _complete_progress(self, message: str) -> None:
        if self._progress_animation_running:
            self.progress_bar.stop()
            self._progress_animation_running = False
        self.progress_bar.configure(mode="determinate", value=100)
        self.progress_percent.set("100%")
        self.progress_detail.set(message)
        self._progress_reset_id = self.root.after(4000, self._hide_progress)

    def _hide_progress(self) -> None:
        self._progress_reset_id = None
        if not self._busy:
            self.progress_frame.grid_remove()
            self.progress_bar.configure(value=0)
            self.progress_detail.set("")
            self.progress_percent.set("")

    def _run_async(
        self,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
        busy_text: str,
        completion_text: str | Callable[[Any], str] = "Operation complete.",
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self.status.set(busy_text)
        self._begin_progress(busy_text)
        self.load_button.configure(state="disabled")
        self.inspect_button.configure(state="disabled")
        self.analyze_button.configure(state="disabled")
        self.issue_list.configure(state="disabled")
        self.root.configure(cursor="watch")

        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, self._finish_error, str(exc))
            else:
                self.root.after(0, self._finish_success, on_success, result, completion_text)

        self.executor.submit(run)

    def _finish_success(
        self,
        callback: Callable[[Any], None],
        result: Any,
        completion_text: str | Callable[[Any], str],
    ) -> None:
        self._busy = False
        self.root.configure(cursor="")
        self.load_button.configure(state="normal")
        self.issue_list.configure(state="normal")
        self.inspect_button.configure(state="normal" if self.selected_issue else "disabled")
        callback(result)
        message = completion_text(result) if callable(completion_text) else completion_text
        self._complete_progress(message)

    def _finish_error(self, message: str) -> None:
        self._busy = False
        self.root.configure(cursor="")
        self.load_button.configure(state="normal")
        self.issue_list.configure(state="normal")
        self.inspect_button.configure(state="normal" if self.selected_issue or (self.mode.get() == "jira" and self.issues) else "disabled")
        self.analyze_button.configure(state="normal" if self.repo_session else "disabled")
        if self._progress_animation_running:
            self.progress_bar.stop()
            self._progress_animation_running = False
        self.progress_bar.configure(mode="determinate")
        self.progress_detail.set("Operation failed · see error details")
        self.progress_percent.set("Error")
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
