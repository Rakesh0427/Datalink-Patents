# Coupling Guard (CC/DC change-impact prototype)

Coupling Guard is a small, dependency-free Python prototype for demonstrating **Control Coupling (CC)**, **Data Coupling (DC)**, change-impact analysis, and GitHub pull-request checks. It analyzes the configured Python source, traces resolvable function calls and parameter uses, maps changed files to impacted functions, and recommends requirement-linked tests.

> **Scope and safety:** This repository originally had no application source or declared implementation language, so this first implementation is a runnable Python MVP with a deliberately small demo. It is not a C/C++/Ada analyzer, it does not collect runtime coupling coverage, it is not DO-178B/C qualified, and its results are not proof of certification compliance. DO-178 use requires project-specific verification, configuration management, tool assessment/qualification where applicable, and review by the responsible engineering/certification team.

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

Requirements: Python 3.10 or newer. No third-party Python package is required.

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

Open PowerShell in the repository root. The implementation uses Python's standard library, so no package installation is needed:

```powershell
py -3.11 -m coupling_guard analyze --root . --config coupling.json --changed demo_project/sensor.py --out reports
py -3.11 -m unittest discover -v
```

If the Python launcher is not installed, use `python` instead of `py -3.11`. View `reports\coupling-report.md` in an editor and open `reports\impact-flow.mmd` in a Mermaid-capable Markdown preview. The paths in `coupling.json` use forward slashes; Python handles them on Windows.

For Git diff analysis in PowerShell, use:

```powershell
py -3.11 -m coupling_guard analyze --root . --config coupling.json --base-ref origin/main --head-ref HEAD --out reports
```

Git must be installed and the base ref fetched. The analyzer calls Git with argument-safe subprocess APIs, so it does not depend on shell quoting for the diff itself. The GitHub Actions workflow template supports both Ubuntu and Windows runners; its command steps explicitly use Bash so environment-variable handling is consistent. Copy `examples/github-actions/coupling-guard.yml` to `.github/workflows/coupling-guard.yml` in your GitHub repository to activate it.

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
