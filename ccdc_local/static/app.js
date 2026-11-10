const $ = (selector) => document.querySelector(selector);
const state = { issues: [], selectedIssue: null, sessionId: null, repoData: null, jobId: null, report: null };
const api = async (url, body) => {
  const response = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
};
const setBusy = (button, busy, busyLabel = "Working…") => {
  button.disabled = busy;
  if (busy) {
    button.dataset.label = button.querySelector("span")?.textContent || button.textContent;
    const label = button.querySelector("span");
    if (label) label.textContent = busyLabel;
  } else {
    const label = button.querySelector("span");
    if (label && button.dataset.label) label.textContent = button.dataset.label;
  }
};
const showError = (message) => {
  $("#errorText").textContent = message;
  $("#errorBanner").classList.remove("hidden");
  $("#errorBanner").scrollIntoView({ behavior: "smooth", block: "center" });
};
const clearError = () => $("#errorBanner").classList.add("hidden");
const toast = (message, isError = false) => {
  const item = $("#toast");
  item.textContent = message;
  item.classList.toggle("error", isError);
  item.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => item.classList.add("hidden"), 3200);
};

const isLocalBrowser = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

async function checkHealth() {
  if (!isLocalBrowser) {
    const message = $("#authMessage");
    message.classList.add("warn");
    message.textContent = "Remote preview only. Run ccdc-gui on your computer before entering Jira credentials.";
    ["#jiraEmail", "#jiraToken", "#jiraPat"].forEach((selector) => { $(selector).disabled = true; });
    return;
  }
  try {
    const data = await api("/api/health");
    window.__jiraEnvAuthConfigured = data.jira_auth_configured;
    const message = $("#authMessage");
    if (data.jira_auth_configured) {
      message.classList.add("ready");
      message.innerHTML = '<span class="auth-dot"></span><span>Jira credentials detected in local environment</span>';
    } else {
      message.classList.add("warn");
      message.innerHTML = '<span class="auth-dot"></span><span>Enter Jira credentials below; they are not saved</span>';
    }
  } catch (error) {
    const message = $("#authMessage");
    message.classList.add("warn");
    message.textContent = "Local service is unavailable. Restart ccdc-gui.";
  }
}

function renderIssues(issues) {
  const container = $("#issueList");
  container.replaceChildren();
  container.classList.remove("empty-state");
  if (!issues.length) {
    container.classList.add("empty-state");
    container.innerHTML = '<div class="empty-illustration"><span class="empty-ring ring-1"></span><span class="empty-ring ring-2"></span><span class="empty-center">⌕</span></div><strong>No recent issues found</strong><p>Try a wider update window or verify the project key.</p>';
    return;
  }
  issues.forEach((issue) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "issue-card" + (state.selectedIssue?.key === issue.key ? " selected" : "");
    const key = document.createElement("span"); key.className = "issue-key"; key.textContent = issue.key;
    const summary = document.createElement("span"); summary.className = "issue-summary"; summary.textContent = issue.summary || "(No summary)";
    const status = document.createElement("span"); status.className = "issue-status"; status.textContent = issue.status || issue.issue_type || "Open";
    const date = document.createElement("span"); date.className = "issue-date"; date.textContent = (issue.updated || "").slice(0, 10);
    button.append(key, summary, status, date);
    button.addEventListener("click", () => selectIssue(issue));
    container.append(button);
  });
}

async function fetchIssues(event) {
  event.preventDefault();
  clearError();
  if (!isLocalBrowser) return showError("This is a remote UI preview. Start ccdc-gui on your own computer to use your Jira credentials and repository.");
  const button = $("#findIssuesButton");
  const repoUrl = $("#repoUrl").value.trim();
  const jiraUrl = $("#jiraUrl").value.trim();
  if (!repoUrl || !jiraUrl) return showError("Enter both the Git repository and Jira URL first.");
  setBusy(button, true, "Loading recent SCRs…");
  try {
    const authMode = $("#authMode").value;
    const email = $("#jiraEmail").value.trim();
    const apiToken = $("#jiraToken").value;
    const pat = $("#jiraPat").value;
    if (authMode === "cloud" && ((!email && !window.__jiraEnvAuthConfigured) || (!apiToken && !window.__jiraEnvAuthConfigured))) {
      return showError("For Jira Cloud, enter your account email and API token, or configure JIRA_EMAIL and JIRA_API_TOKEN before starting the app.");
    }
    if (authMode === "datacenter" && !pat && !window.__jiraEnvAuthConfigured) {
      return showError("For Jira Data Center, enter your personal access token or configure JIRA_PAT before starting the app.");
    }
    const result = await api("/api/issues", {
      jira_url: jiraUrl,
      project: $("#projectKey").value.trim() || "SCR",
      days: Number($("#days").value),
      auth_mode: authMode,
      email: email || null,
      api_token: apiToken || null,
      pat: pat || null,
    });
    state.issues = result.issues;
    state.selectedIssue = null;
    state.sessionId = null;
    $("#issueCount").textContent = `${result.count} ISSUE${result.count === 1 ? "" : "S"}`;
    $("#issueSubhead").textContent = `Updated within the last ${$("#days").value} days · select one to inspect its code impact.`;
    renderIssues(state.issues);
    $("#repoPanel").classList.add("hidden");
    $("#report").classList.add("hidden");
    $("#stepOne").classList.add("done");
    $("#stepTwo").classList.add("active");
    if (!result.count) toast("No issues matched that Jira project and update window.");
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(button, false);
  }
}

async function selectIssue(issue) {
  clearError();
  state.selectedIssue = issue;
  renderIssues(state.issues);
  $("#stepTwo").classList.add("done");
  $("#stepThree").classList.add("active");
  $("#repoPanel").classList.remove("hidden");
  $("#report").classList.add("hidden");
  $("#repoControls").classList.add("hidden");
  $("#repoLoading").classList.remove("hidden");
  $("#repoState").textContent = "INSPECTING";
  $("#repoPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const result = await api("/api/repo/open", { repo_url: $("#repoUrl").value.trim(), issue_key: issue.key });
    state.sessionId = result.session_id;
    state.repoData = result;
    fillRefSelect($("#baseRef"), [...new Set([result.default_base, ...result.refs])], result.default_base, $("#baseRefOptions"));
    const heads = [...new Set(["HEAD", ...result.matching_branches, ...result.matching_commits.map((c) => c.ref), ...result.refs])];
    const preferredHead = result.matching_branches[0] || result.matching_commits[0]?.ref || "HEAD";
    fillRefSelect($("#headRef"), heads, preferredHead, $("#headRefOptions"));
    if (result.matched_issue_key) {
      $("#matchCallout").classList.add("matched");
      $("#matchText").textContent = `Found ${result.matching_branches.length} branch(es) and ${result.matching_commits.length} commit(s) containing ${issue.key}. Verify the selected range before analysis.`;
    } else {
      $("#matchCallout").classList.remove("matched");
      $("#matchText").textContent = `No Git branch or commit mentions ${issue.key}. Choose the SCR pull request's base and head refs below.`;
    }
    $("#repoHint").textContent = `${result.repo_label} · select the refs that contain this SCR's change.`;
    $("#repoState").textContent = "READY";
    $("#repoControls").classList.remove("hidden");
    $("#analyzeButton").disabled = false;
  } catch (error) {
    showError(error.message);
    $("#repoState").textContent = "ERROR";
  } finally {
    $("#repoLoading").classList.add("hidden");
  }
}

function fillRefSelect(input, refs, selected, list) {
  list.replaceChildren();
  refs.filter(Boolean).forEach((ref) => {
    const option = document.createElement("option");
    option.value = ref;
    list.append(option);
  });
  input.value = selected || "";
}

async function analyzeSelected() {
  if (!state.selectedIssue || !state.sessionId) return showError("Select an SCR and inspect its repository before analyzing.");
  clearError();
  const button = $("#analyzeButton");
  button.disabled = true;
  $("#progressPanel").classList.remove("hidden");
  $("#progressTitle").textContent = `Analyzing ${state.selectedIssue.key}`;
  $("#progressText").textContent = "Comparing the selected Git range and tracing C/C++ calls…";
  $("#progressPanel").scrollIntoView({ behavior: "smooth", block: "center" });
  try {
    const result = await api("/api/analyze", {
      session_id: state.sessionId,
      issue: state.selectedIssue,
      base_ref: $("#baseRef").value,
      head_ref: $("#headRef").value,
      config_path: $("#configPath").value.trim() || null,
    });
    state.jobId = result.job_id;
    let job;
    do {
      await new Promise((resolve) => setTimeout(resolve, 1300));
      job = await api(`/api/jobs/${state.jobId}`);
      $("#progressText").textContent = job.phase || "Analyzing source…";
      if (job.state === "failed") throw new Error(job.error || "Analysis failed.");
    } while (job.state !== "complete");
    state.report = await api(`/api/reports/${state.jobId}`);
    renderReport(state.report.json, state.report);
    $("#progressPanel").classList.add("hidden");
    $("#report").classList.remove("hidden");
    $("#report").scrollIntoView({ behavior: "smooth", block: "start" });
    toast("Impact report is ready.");
  } catch (error) {
    $("#progressPanel").classList.add("hidden");
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

function renderReport(report, assets) {
  const edges = report.edges || [];
  const control = edges.filter((edge) => edge.coupling_types.includes("control")).length;
  const data = edges.filter((edge) => edge.coupling_types.includes("data")).length;
  $("#metricChanged").textContent = report.changed_functions.length;
  $("#metricImpacted").textContent = report.impacted_functions.length;
  $("#metricControl").textContent = control;
  $("#metricData").textContent = data;
  $("#flowTabCount").textContent = edges.length;
  $("#reportMeta").textContent = `${report.issue.key} · ${report.base_ref} → ${report.head_ref} · ${report.head_commit.slice(0, 10)}`;
  $("#summaryIssue").textContent = report.issue.key;
  $("#summaryRange").textContent = `${report.base_ref} → ${report.head_ref}`;
  $("#summaryFiles").textContent = report.changed_files.length;
  renderChecklist(report);
  renderGraph(report);
  renderEdges(edges);
  $("#diffView").textContent = assets.diff || "No source diff available.";
  $("#mermaidView").textContent = assets.mermaid || "";
  $("#jsonView").textContent = JSON.stringify(report, null, 2);
  const notes = [...(report.parse_warnings || []), ...(report.unresolved_calls || []), ...(report.limitations || [])];
  $("#limitations").textContent = notes.length ? `Review notes: ${notes.join(" · ")}` : "No parser warnings reported. Static analysis remains approximate; review the findings against the project architecture and tests.";
  drawSvg(report);
}

function renderChecklist(report) {
  const container = $("#reviewChecklist");
  container.replaceChildren();
  const checks = [];
  if (report.changed_files.length) checks.push({ text: `${report.changed_files.length} changed C/C++ source file(s) in selected range`, warn: false });
  if (report.unresolved_calls?.length) checks.push({ text: `${report.unresolved_calls.length} unresolved or ambiguous call(s) need review`, warn: true });
  if (!Object.keys(report.affected_requirements || {}).length) checks.push({ text: "No configured requirement mapping matched the impact cone", warn: true });
  if (!Object.keys(report.recommended_tests || {}).length) checks.push({ text: "No configured tests mapped; choose verification tests manually", warn: true });
  if (!checks.length) checks.push({ text: "Review changed edges, affected requirements and verification evidence", warn: false });
  checks.slice(0, 5).forEach((item) => {
    const row = document.createElement("div"); row.className = "check-item";
    const bullet = document.createElement("span"); bullet.className = `check-bullet${item.warn ? " warn" : ""}`; bullet.textContent = item.warn ? "!" : "✓";
    const text = document.createElement("span"); text.textContent = item.text;
    row.append(bullet, text); container.append(row);
  });
}

function renderEdges(edges) {
  const tbody = $("#flowRows");
  tbody.replaceChildren();
  if (!edges.length) {
    const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 5; cell.textContent = "No resolvable calls in the impact cone."; row.append(cell); tbody.append(row); return;
  }
  edges.forEach((edge) => {
    const row = document.createElement("tr");
    const caller = document.createElement("td"); caller.textContent = edge.caller;
    const callee = document.createElement("td"); callee.textContent = edge.callee;
    const kinds = document.createElement("td");
    (edge.coupling_types.length ? edge.coupling_types : ["call"]).forEach((kind) => {
      const chip = document.createElement("span"); chip.className = `kind-chip ${kind}`; chip.textContent = kind.toUpperCase(); kinds.append(chip);
    });
    const state = document.createElement("td"); const stateChip = document.createElement("span"); stateChip.className = `state-chip ${edge.state}`; stateChip.textContent = edge.state.toUpperCase(); state.append(stateChip);
    const location = document.createElement("td"); location.textContent = `${edge.path}:${edge.line}`;
    row.append(caller, callee, kinds, state, location); tbody.append(row);
  });
}

function svgNode(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}
function shorten(id) {
  const after = id.split("::").pop() || id;
  return after.length > 42 ? `${after.slice(0, 39)}…` : after;
}
function drawSvg(report) {
  const container = $("#graphCanvas");
  container.replaceChildren();
  const edges = report.edges || [];
  if (!edges.length) { const empty = document.createElement("div"); empty.className = "graph-empty"; empty.textContent = "No resolved calls in the selected impact cone."; container.append(empty); return; }
  const width = 1000, rowH = 76, height = Math.max(190, edges.length * rowH + 24);
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, class: "graph-svg", role: "img", "aria-label": "CC/DC impact flow diagram" });
  const defs = svgNode("defs");
  const marker = svgNode("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "6", markerHeight: "6", orient: "auto-start-reverse" });
  marker.append(svgNode("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#98a8bf" })); defs.append(marker); svg.append(defs);
  const changed = new Set(report.changed_functions), impacted = new Set(report.impacted_functions);
  edges.forEach((edge, index) => {
    const y = 15 + index * rowH;
    const color = edge.state === "added" ? "#33aa8e" : edge.state === "removed" ? "#d87578" : "#9baac0";
    const callerFill = changed.has(edge.caller) ? "#fff0f0" : impacted.has(edge.caller) ? "#eff5ff" : "#ffffff";
    const calleeFill = changed.has(edge.callee) ? "#fff0f0" : impacted.has(edge.callee) ? "#eff5ff" : "#ffffff";
    svg.append(svgNode("rect", { x: 8, y: y + 4, width: 360, height: 50, rx: 8, fill: callerFill, stroke: changed.has(edge.caller) ? "#e38a8d" : "#e3eaf3" }));
    svg.append(svgNode("rect", { x: 632, y: y + 4, width: 360, height: 50, rx: 8, fill: calleeFill, stroke: changed.has(edge.callee) ? "#e38a8d" : "#e3eaf3" }));
    const callerText = svgNode("text", { x: 21, y: y + 24, fill: "#40516a", "font-size": "12", "font-family": "Consolas, monospace" }); callerText.textContent = shorten(edge.caller); svg.append(callerText);
    const calleeText = svgNode("text", { x: 645, y: y + 24, fill: "#40516a", "font-size": "12", "font-family": "Consolas, monospace" }); calleeText.textContent = shorten(edge.callee); svg.append(calleeText);
    const callerSub = svgNode("text", { x: 21, y: y + 42, fill: "#96a2b3", "font-size": "9", "font-family": "Segoe UI, sans-serif" }); callerSub.textContent = edge.caller_component; svg.append(callerSub);
    const calleeSub = svgNode("text", { x: 645, y: y + 42, fill: "#96a2b3", "font-size": "9", "font-family": "Segoe UI, sans-serif" }); calleeSub.textContent = edge.callee_component; svg.append(calleeSub);
    svg.append(svgNode("line", { x1: 380, y1: y + 29, x2: 620, y2: y + 29, stroke: color, "stroke-width": "1.6", "marker-end": "url(#arrow)" }));
    const label = [...(edge.coupling_types || []), edge.state].join(" · ").toUpperCase();
    const labelBg = svgNode("rect", { x: 446, y: y + 11, width: 112, height: 17, rx: 8, fill: "#f5f7fb", stroke: "#e7ecf3" }); svg.append(labelBg);
    const tag = svgNode("text", { x: 502, y: y + 23, fill: color === "#9baac0" ? "#74849a" : color, "font-size": "8", "font-family": "Consolas, monospace", "text-anchor": "middle" }); tag.textContent = label; svg.append(tag);
  });
  container.append(svg);
}

function download(filename, contents, type) {
  const blob = new Blob([contents], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
  URL.revokeObjectURL(url);
}

function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    document.querySelectorAll(".tab-content").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tab.dataset.tab}`));
  }));
}

$("#connectForm").addEventListener("submit", fetchIssues);
$("#analyzeButton").addEventListener("click", analyzeSelected);
$("#dismissError").addEventListener("click", clearError);
$("#downloadMarkdown").addEventListener("click", () => state.report && download("ccdc-report.md", state.report.markdown, "text/markdown"));
$("#downloadJson").addEventListener("click", () => state.report && download("ccdc-report.json", JSON.stringify(state.report.json, null, 2), "application/json"));
setupTabs();
$("#authMode").addEventListener("change", () => {
  const cloud = $("#authMode").value === "cloud";
  $("#cloudLogin").classList.toggle("hidden", !cloud);
  $("#dcLogin").classList.toggle("hidden", cloud);
});
checkHealth();
