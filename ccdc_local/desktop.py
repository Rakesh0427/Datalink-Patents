from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import sys
from collections import defaultdict, deque
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
        issue_list_frame = ttk.Frame(scr_panel)
        issue_list_frame.pack(fill="both", expand=True)
        self.issue_list = tk.Listbox(issue_list_frame, height=5, activestyle="dotbox", exportselection=False)
        self.issue_list.pack(side="left", fill="both", expand=True)
        issue_scroll = ttk.Scrollbar(issue_list_frame, orient="vertical", command=self.issue_list.yview)
        issue_scroll.pack(side="right", fill="y")
        self.issue_list.configure(yscrollcommand=issue_scroll.set)
        self.issue_list.bind("<<ListboxSelect>>", self._on_issue_list_selection)
        self.issue_list.bind("<Double-Button-1>", lambda _event: self._inspect_selected())
        self.inspect_button = ttk.Button(scr_panel, text="Continue with selected SCR", command=self._inspect_selected, state="disabled")
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
        self.results_frame = results
        self.notebook = ttk.Notebook(results)
        self.notebook.pack(fill="both", expand=True)
        flow_page = ttk.Frame(self.notebook)
        self.notebook.add(flow_page, text="CC/DC flow diagram")
        flow_legend = ttk.Frame(flow_page, padding=(8, 5))
        flow_legend.pack(fill="x")
        ttk.Label(flow_legend, text="Changed", foreground="#b4232c").pack(side="left", padx=(0, 12))
        ttk.Label(flow_legend, text="Impacted", foreground="#1f5da8").pack(side="left", padx=(0, 12))
        ttk.Label(flow_legend, text="CC · Control Coupling", foreground="#7042a5").pack(side="left", padx=(0, 12))
        ttk.Label(flow_legend, text="DC · Data Coupling", foreground="#167a58").pack(side="left")
        self.flow_status = tk.StringVar(value="Run an analysis to draw the call and coupling flow.")
        ttk.Label(flow_page, textvariable=self.flow_status, padding=(8, 3)).pack(anchor="w")
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

        self.result_text: dict[str, tk.Text] = {}
        for title, key in (("Summary", "markdown"), ("Source diff", "diff"), ("Mermaid source", "mermaid"), ("JSON", "json")):
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
        if self.mode.get() != "git" or not self.repo_session:
            return
        selection = self.issue_list.curselection()
        if not selection:
            return
        issue = self.issues[selection[0]]
        self.selected_issue = issue
        self._show_issue_refs(self.repo_session, issue)

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
        self._scroll_to(self.issue_frame)
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
            self._render_repository_details(self.repo_session)
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
            if not refs and not commits:
                raise RuntimeError("Git clone completed, but the repository has no branches or commits to inspect. Verify that this is a non-empty Git repository.")
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
        self.inspect_button.configure(text="Inspect selected SCR", state="normal" if issues else "disabled")
        if issues:
            self.manual_frame.pack_forget()
            self.status.set(f"Found {len(issues)} SCR key(s) in repository history. Select one to inspect matching refs.")
        else:
            self.manual_frame.pack(fill="x", pady=(6, 0))
            self.status.set("Repository loaded, but no SCR-like key was found in its branches or recent commit messages.")
        self._scroll_to(self.issue_frame)

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
        recent_commit_refs = [item["ref"] for item in session.get("recent_commits", [])]
        head_choices = list(dict.fromkeys([*branches, *commit_refs, "HEAD", *session["refs"], *recent_commit_refs]))
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
            report = json.loads(contents["json"])
            return {"paths": paths, "contents": contents, "report": report}

        self.analyze_button.configure(state="disabled")
        self._run_async(run, self._show_report, f"Analyzing {issue['key']} · {base_ref} → {head_ref}…")

    def _draw_flow(self, report: dict[str, Any]) -> None:
        canvas = self.flow_canvas
        canvas.delete("all")
        edges = report.get("edges", [])
        changed = set(report.get("changed_functions", []))
        impacted = set(report.get("impacted_functions", []))
        nodes = set(changed) | set(impacted)
        components: dict[str, str] = {}
        for edge in edges:
            caller, callee = edge.get("caller"), edge.get("callee")
            if caller and callee:
                nodes.update((caller, callee))
                components[caller] = edge.get("caller_component", "")
                components[callee] = edge.get("callee_component", "")
        if not nodes:
            message = (
                "No C/C++ files changed in the selected Git range."
                if not report.get("changed_files")
                else "No functions or resolvable call flows were found. Review the parse and resolution notes in Summary."
            )
            canvas.create_text(450, 200, text=message, fill="#65748a", font=("Segoe UI", 12), width=760)
            canvas.configure(scrollregion=(0, 0, 900, 430))
            self.flow_status.set(message)
            return

        layers = flow_node_layers(report)
        columns: dict[int, list[str]] = defaultdict(list)
        for name in sorted(nodes):
            columns[layers.get(name, 0)].append(name)
        ordered_layers = sorted(columns)
        node_w, node_h = 278, 86
        x_gap, y_gap, margin = 92, 42, 28
        max_rows = max(len(columns[layer]) for layer in ordered_layers)
        width = max(900, margin * 2 + len(ordered_layers) * node_w + max(0, len(ordered_layers) - 1) * x_gap)
        height = max(430, margin * 2 + max_rows * node_h + max(0, max_rows - 1) * y_gap)
        canvas.configure(scrollregion=(0, 0, width, height))
        positions: dict[str, tuple[int, int]] = {}
        for column_index, layer in enumerate(ordered_layers):
            for row_index, name in enumerate(columns[layer]):
                positions[name] = (margin + column_index * (node_w + x_gap), margin + row_index * (node_h + y_gap))

        state_colors = {"added": "#16865f", "removed": "#c23d4b", "unchanged": "#8391a3", "current": "#8391a3"}
        for edge in edges:
            caller, callee = edge.get("caller"), edge.get("callee")
            if caller not in positions or callee not in positions:
                continue
            x1, y1 = positions[caller]
            x2, y2 = positions[callee]
            sy, ey = y1 + node_h // 2, y2 + node_h // 2
            if caller == callee:
                coords = (x1 + node_w - 10, y1 + 28, x1 + node_w + 44, y1 - 16, x1 + node_w + 44, y1 + 38, x1 + node_w - 8, y1 + 42)
            elif x2 == x1:
                side = x1 + node_w + 34
                coords = (x1 + node_w, sy, side, sy, side, ey, x1 + node_w, ey)
            elif x2 > x1:
                sx, ex = x1 + node_w, x2
                mid = (sx + ex) // 2
                coords = (sx, sy, mid, sy, mid, ey, ex, ey)
            else:
                sx, ex = x1, x2 + node_w
                mid = max(ex + 18, min(sx - 18, (sx + ex) // 2))
                coords = (sx, sy, mid, sy, mid, ey, ex, ey)
            coupling = set(edge.get("coupling_types", []))
            has_control = bool(edge.get("control_coupling")) or "control" in coupling or edge.get("control_context", False)
            has_data = bool(edge.get("data_coupling")) or "data" in coupling
            if has_control and has_data:
                color, kind = "#7050a0", "CC + DC"
            elif has_control:
                color, kind = "#7050a0", "CC"
            elif has_data:
                color, kind = "#16865f", "DC"
            else:
                color, kind = "#718096", "CALL"
            state = edge.get("state", "current")
            canvas.create_line(*coords, fill=state_colors.get(state, "#718096"), width=2, arrow=tk.LAST, arrowshape=(10, 12, 5), smooth=True)
            label = f"{kind} · {state}"
            mid_x = (coords[0] + coords[-2]) // 2
            mid_y = (coords[1] + coords[-1]) // 2 - 9
            canvas.create_rectangle(mid_x - 48, mid_y - 10, mid_x + 48, mid_y + 10, fill="white", outline="")
            canvas.create_text(mid_x, mid_y, text=label, fill=color, font=("Segoe UI", 8, "bold"))

        for name, (x, y) in positions.items():
            is_changed = name in changed
            is_impacted = name in impacted and not is_changed
            outline, fill, tag = (
                ("#c23d4b", "#fff0f0", "CHANGED") if is_changed
                else (("#2767ad", "#eef5ff", "IMPACTED") if is_impacted else ("#9aa7b7", "#ffffff", "FUNCTION"))
            )
            canvas.create_rectangle(x, y, x + node_w, y + node_h, fill=fill, outline=outline, width=2 if is_changed else 1)
            canvas.create_text(x + 12, y + 13, text=tag, anchor="w", fill=outline, font=("Segoe UI", 8, "bold"))
            path, _, symbol = name.partition("::")
            if len(symbol) > 36:
                symbol = symbol[:33] + "..."
            canvas.create_text(x + 12, y + 39, text=symbol or name, anchor="w", fill="#26364b", font=("Consolas", 9, "bold"))
            component = components.get(name) or (path if path else "")
            if len(component) > 42:
                component = component[:39] + "..."
            canvas.create_text(x + 12, y + 65, text=component, anchor="w", fill="#78879a", font=("Segoe UI", 8))

        cross_edges = sum(1 for edge in edges if edge.get("cross_component"))
        self.flow_status.set(
            f"{len(nodes)} functions · {len(edges)} call/coupling edges · {cross_edges} cross-component · "
            f"CC = Control Coupling, DC = Data Coupling. Edge color shows added/removed/current state."
        )

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
        self._draw_flow(result["report"])
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
