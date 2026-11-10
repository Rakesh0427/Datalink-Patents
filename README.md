# Coupling Guard (CC/DC change-impact prototype)

This repository contains local prototypes for **Control Coupling (CC)**, **Data Coupling (DC)**, and change impact: `coupling-guard` analyzes configured Python source, while `ccdc-local` analyzes C/C++ changes for a selected SCR using Git. **Jira is optional**: use the SCR key in a branch/commit for Git-only analysis, or connect Jira to browse recent issues and add summary/status context. The C/C++ `ccdc-local` workflow writes source diffs, JSON findings, Mermaid flow source, and Mermaid.js-rendered SVG/PNG diagrams; its native desktop GUI displays the rendered diagram. Analysis runs locally; the tool fetches Jira metadata only when Jira mode is selected and clones source using the user's normal Git credentials.

> **Scope and safety:** The C/C++ prototype uses Tree-sitter syntax parsing and heuristic call resolution. It is not a compiler-semantic analyzer, does not execute target tests or collect runtime coupling coverage, does not support Ada, and is not DO-178B/C qualified. Its results are not proof of certification compliance. Review all findings and unresolved calls; DO-178 use requires project-specific verification, configuration management, tool assessment/qualification where applicable, and approval by the responsible engineering/certification team.

## End-to-end example

The demo flow is:

```text
Sensor.convert_temperature(raw)
           │ data value: temperature
           ▼
Controller.update_valve(temperature, limit)
           │ decision: temperature >= limit
           ├── true  ──control influence──> Actuator.actuator_open()
           └── false ──control influence──> Actuator.actuator_close()
```

The Python source is under `demo_project/`. `coupling.json` declares component boundaries, requirement-to-function links, and requirement-to-test links. The test cases exercise below-limit, at-limit, and above-limit behavior.

## Run locally

Requirements: Python 3.10 or newer. The Python analyzer uses only the standard library. Install the C/C++ parser dependencies for `ccdc-local` with `python -m pip install -e .` (or `py -3.11 -m pip install -e .` on Windows).

From the repository root:

```bash
python -m coupling_guard analyze \
  --root . \
  --config coupling.json \
  --changed demo_project/sensor.py \
  --out reports
```

This models a change to `sensor.py` (the file does not need to be modified for this dry run). The command prints summary counts and writes:

- `reports/coupling-report.md` — review-friendly impact report.
- `reports/coupling-report.json` — machine-readable findings.
- `reports/impact-flow.mmd` — Mermaid flow diagram source.

To render the Mermaid graph, open `reports/impact-flow.mmd` in a Mermaid-compatible Markdown preview/editor. Changed functions are styled red and other impacted functions amber.

Expected summary (exact call/impact counts may change as the demo evolves):

```text
Analyzed 6 functions and 4 resolved calls.
Changed files: 1; changed functions: 1; impacted functions: 5
Recommended tests: TC-TEMP-LOW, TC-TEMP-LIMIT, TC-TEMP-HIGH
```

The report should identify the sensor conversion function as changed; the application, controller, and actuator paths as impacted; `REQ-TEMP-014` and `REQ-VALVE-022` as affected; and all three example test IDs as recommended. With `--base-ref`, it also analyzes the Python files in that Git revision and labels observed call edges `added`, `removed`, or `unchanged`.

### Run the tests

```bash
python -m unittest discover -v
```

The suite includes tests for the example application and analyzer. A successful run ends with `OK`.

### Windows (PowerShell)

Open PowerShell in the repository root. Install the package once (this also installs the C/C++ parsers), then run the Python demo and tests:

```powershell
py -3.11 -m pip install -e .
py -3.11 -m coupling_guard analyze --root . --config coupling.json --changed demo_project/sensor.py --out reports
py -3.11 -m unittest discover -v
```

If the Python launcher is not installed, use `python` instead of `py -3.11`. View `reports\coupling-report.md` in an editor and open `reports\impact-flow.mmd` in a Mermaid-capable Markdown preview. The paths in `coupling.json` use forward slashes; Python handles them on Windows.

For Git diff analysis in PowerShell, use:

```powershell
py -3.11 -m coupling_guard analyze --root . --config coupling.json --base-ref origin/main --head-ref HEAD --out reports
```

Git must be installed and the base ref fetched. The analyzer calls Git with argument-safe subprocess APIs, so it does not depend on shell quoting for the diff itself. The GitHub Actions workflow template supports both Ubuntu and Windows runners; its command steps explicitly use Bash so environment-variable handling is consistent. Copy `examples/github-actions/coupling-guard.yml` to `.github/workflows/coupling-guard.yml` in your GitHub repository to activate it.

## Local C/C++ desktop GUI and optional Jira (`ccdc-gui`)

The native desktop tool analyzes Git changes for a selected SCR. Jira is optional: Git-only mode uses the SCR key from the branch/commit; Jira mode browses recent issues and adds summary/status context. Analysis and report generation run locally.

### Install

Create and activate a virtual environment, then install this tool and its Tree-sitter C/C++ grammars:

```bash
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e .
```

Git must be installed. For private repositories, configure Git Credential Manager or SSH access normally before launching the tool. Do not put Git/Jira credentials in the repository or pass them in command-line arguments.

### Configure Jira credentials locally

In the native desktop GUI, choose Jira Cloud or Data Center and enter the corresponding email/token or PAT. Credential fields are masked and used only for Jira requests; they are not saved to disk or included in reports. The terminal CLI can instead use `JIRA_EMAIL` + `JIRA_API_TOKEN` (Cloud) or `JIRA_PAT` (Data Center) from the local environment. Never commit credentials or a `.env` file.

### Start the native desktop GUI or terminal workflow

Run this from a terminal after activating the virtual environment:

```bash
ccdc-gui
```

This opens a native Tk desktop window on your computer. It does **not** start a web server or open a browser. In Git-only mode, enter the repository and click **Load repository updates**; the tool scans recent commits and branch names, shows detected SCR keys and recent updates, then lets you select an SCR without typing its key. Selecting an SCR takes you to the base/head refs and analysis action. Results open on a concise Overview with change/impact/CC/DC counts, affected requirements and tests, and review notes. Separate tabs provide the Mermaid.js-rendered CC/DC diagram (Fit and zoom controls), a tabular CC/DC edge-details view, Markdown, source diff, Mermaid source, and JSON. Analysis generates `impact-flow.mmd`, then the local Mermaid.js renderer writes `impact-flow.svg` and `impact-flow.png`; the app displays that rendered PNG. The renderer is installed as a Python dependency, so Node.js, Chromium, and a network connection are not needed at runtime. Repository loading and analysis show a live progress bar: it animates during Git operations whose totals are unknown, then reports actual refs, commit-message, and source-file counts as those contents are scanned, followed by explicit report/diagram generation stages. Common GitHub/GitLab/Bitbucket browser URLs are normalized to repository clone URLs. Jira browsing remains optional. On Windows, Tkinter is included with the standard python.org Python installer. On Debian/Ubuntu Linux, install `python3-tk` if Tkinter is missing.

For a terminal-only workflow, run:

```bash
ccdc-local
```

The CLI prompts for a Git repository URL or local path and an optional Jira URL. Leave Jira blank to enter an SCR key for Git-only analysis. If you provide Jira, it lists recent issues (project `SCR`, updated within the last 30 days by default).

You can set the project and update window on the command line, for example:

```bash
ccdc-local --repo "https://github.com/example/flight-software.git" --jira-url "https://example.atlassian.net" --project SCR --days 60
```

The command still displays the recent issue list and asks you to select one. To open one known issue directly:

```bash
ccdc-local --repo "https://github.com/example/flight-software.git" --jira-url "https://example.atlassian.net" --issue SCR-123
```

For Jira-free analysis, provide the Git repository and SCR key only:

```bash
ccdc-local --repo "https://github.com/example/flight-software.git" --issue SCR-123
```

After selection, it looks for branches or commit messages containing the SCR key. If found, select the candidate branch/commit and the base ref. If there is no issue-key match, the tool prompts for base and head refs. **For reliable automatic mapping, use the SCR key in branch names or commit messages**, for example `feature/SCR-123-calibration` and `Fix sensor scale SCR-123`. A Jira issue list alone cannot establish which code change belongs to that issue.

The tool clones the source into a temporary local directory, checks out the selected head there, compares the selected base/head, and deletes the temporary clone when analysis completes. Reports remain locally under `ccdc-reports/<SCR-KEY>/`:

- `ccdc-report.md` — issue, changed files/functions, impact, CC/DC edges, requirements/tests, parse warnings, and limitations.
- `ccdc-report.json` — structured findings, edge states, and selected refs.
- `impact-flow.mmd` — Mermaid diagram source.
- `impact-flow.svg` / `impact-flow.png` — Mermaid.js-rendered diagram (vector and raster).
- `source-diff.patch` — source diff for the selected Git range.

The desktop GUI displays the rendered diagram; the `.mmd` source can also be opened in any Mermaid-compatible viewer. No source is uploaded to Jira by this tool; Jira is queried for issue metadata. Git itself contacts the supplied repository host to clone/fetch as expected.

### Windows example

In PowerShell, after installing the package (Jira credentials are needed only if using Jira mode):

```powershell
ccdc-local --repo "https://github.com/example/flight-software.git" --jira-url "https://example.atlassian.net" --project SCR --days 30 --out "C:\work\ccdc-reports"
```

The same command works with a local repository path, such as `--repo "C:\work\flight-software"`. Reports are written to the `--out` directory. The CLI itself prompts for the Jira issue and refs when necessary.

### Configure component and traceability mappings

Optionally put `ccdc.json` at the target repository root, or pass a config path using `--config`. Component path prefixes define the software boundaries used in inter-component reporting. Requirements map to function IDs, and tests map to requirement IDs:

```json
{
  "components": {
    "Sensor": ["src/sensor"],
    "Control": ["src/control"],
    "Actuator": ["src/actuator"]
  },
  "requirements": {
    "REQ-TEMP-014": ["src/sensor/sensor.c::convert_temperature#1"],
    "REQ-VALVE-022": ["src/control/controller.cpp::update_valve#2"]
  },
  "tests": {
    "TC-TEMP-LIMIT": ["REQ-TEMP-014", "REQ-VALVE-022"]
  }
}
```

Without `ccdc.json`, the analyzer uses the first directory in each source path as a provisional component and reports no requirements/tests unless mapped. Function IDs are emitted in the report; use those exact IDs in mappings.

### Test the C/C++ analyzer without Jira

Install the project and run the automated suite:

```bash
python -m unittest discover -v
```

The C/C++ test creates a temporary Git repository, changes a C function, runs analysis against a C++ caller/controller/actuator flow, and checks that data/control edges and reports are produced. It does not require a Jira server or a real source repository.

### Demonstrate a regression being caught

1. In `demo_project/sensor.py`, change `return raw * 0.1` to `return raw * 0.01`.
2. Run `python -m unittest discover -v`.
3. The at-limit and above-limit tests should fail because the altered conversion changes the controller’s input.
4. Restore `0.1`, rerun the tests, and verify that they pass.
5. Run the analyzer command above to see the cross-component impact and recommended tests.

The mutation is an illustration only; tests should be run against an identified build/revision in real projects.

## Analyze an actual Git diff

When both refs are available locally, the analyzer can get changed file names from Git:

```bash
python -m coupling_guard analyze \
  --root . \
  --config coupling.json \
  --base-ref origin/main \
  --head-ref HEAD \
  --out reports
```

It uses `git diff --name-only BASE...HEAD`. In CI, fetch the base commit/history before running this command. For a local dry run, use repeatable `--changed path/to/file.py` options instead.

## Configure components, requirements, and tests

Edit `coupling.json`:

- `include`: Python source paths to analyze, relative to repository root.
- `components`: component names and path prefixes.
- `requirements`: requirement IDs mapped to fully qualified function IDs.
- `tests`: test IDs mapped to requirement IDs.

Example:

```json
{
  "include": ["src"],
  "components": {
    "Input": ["src/input"],
    "Control": ["src/control"],
    "Output": ["src/output"]
  },
  "requirements": {
    "REQ-101": ["src.control.evaluate"]
  },
  "tests": {
    "TEST-101-A": ["REQ-101"]
  }
}
```

Function IDs are the module path (relative to repository root, with `.py` removed) plus the function name, for example `demo_project.controller.update_valve`.

## GitHub pull-request integration

`examples/github-actions/coupling-guard.yml` is a GitHub Actions template for pull requests on both `ubuntu-latest` and `windows-latest`. Copy it to `.github/workflows/coupling-guard.yml` in the target repository to activate it. It:

1. Checks out the pull request and base history.
2. Analyzes the changed files against the configured source and mappings.
3. Runs the example regression tests.
4. Adds the Markdown report to the GitHub Actions job summary.
5. Uploads JSON, Markdown, and Mermaid outputs as a workflow artifact.

The workflow job is a GitHub check. To make it block merges, configure a repository ruleset/branch protection rule that requires **CC/DC impact analysis and regression tests** to pass. This repository workflow is an example; check your organization’s Actions policy and pin/approve actions according to your configuration-management policy.

For hardware-dependent tests, change `runs-on` to an approved self-hosted runner and ensure the runner/build environment is controlled. Keep certification records in the project’s approved evidence repository; the example workflow artifact retention is only 30 days.

## What this prototype detects

- Top-level Python function definitions and direct calls it can resolve.
- Call-site arguments mapped to callee parameters for straightforward calls/imports.
- Parameter use in `if`/`while` conditions as a control influence.
- Parameter use in other expressions as a data dependency.
- Calls located under conditional paths as control influence on the callee.
- A conservative impact cone: changed functions, their callers, and reachable downstream functions.
- Requirement/test recommendations configured in `coupling.json`.

## Current limitations and next engineering steps

- This is **Python-only**; it does not parse C, C++, Ada, generated code, or object code.
- The graph is a static approximation; dynamic dispatch, reflection, aliasing, shared state, concurrency/interrupts, and some import patterns are not fully resolved.
- The analyzer does not execute tests, record runtime data/control-coupling coverage, or determine whether a test genuinely exercises a dependency. The workflow runs the sample suite; test links in the report are configuration-based recommendations.
- Comments and documentation are not treated as proof of executable behavior.
- It does not create a DO-178B/C compliance claim or replace the project's verification process.

A production path should begin by selecting the real project language and compiler, then adding language/compiler-aware analysis, instrumentation or trace collection for requirements-based tests, component/interface baselines, exact test-result linkage, reviewed findings, controlled evidence export, and a project-specific DO-330/tool assessment where applicable.
