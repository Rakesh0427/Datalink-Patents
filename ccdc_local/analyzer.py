from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Language, Parser
import tree_sitter_c
import tree_sitter_cpp

EXTENSIONS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".C", ".H"}
SKIP_DIRS = {".git", ".svn", "build", "dist", "out", "target", "vendor", "third_party", "node_modules", ".venv", "venv"}


@dataclass
class Function:
    id: str
    name: str
    path: str
    line: int
    end_line: int
    params: list[str]
    control_params: list[str]
    data_params: list[str]
    component: str
    arity: int


@dataclass
class CallEdge:
    caller: str
    callee: str
    caller_component: str
    callee_component: str
    path: str
    line: int
    arguments: list[str]
    data_coupling: list[str]
    control_coupling: list[str]
    control_context: bool
    state: str = "current"

    @property
    def cross_component(self) -> bool:
        return self.caller_component != self.callee_component

    @property
    def kinds(self) -> list[str]:
        result = []
        if self.data_coupling:
            result.append("data")
        if self.control_coupling or self.control_context:
            result.append("control")
        return result


def _parser_for(path: Path) -> Parser:
    language = Language(tree_sitter_c.language() if path.suffix in {".c", ".h", ".H"} else tree_sitter_cpp.language())
    return Parser(language)


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _descendant_identifier_names(node, source: bytes) -> list[str]:
    names = []
    for child in _walk(node):
        if child.type in {"identifier", "field_identifier", "type_identifier"}:
            names.append(_text(child, source))
    return names


def _function_name(function_node, source: bytes) -> str | None:
    declarator = function_node.child_by_field_name("declarator")
    if declarator is None:
        return None
    fdecl = next((n for n in _walk(declarator) if n.type == "function_declarator"), None)
    if fdecl is None:
        return None
    name_node = fdecl.child_by_field_name("declarator")
    if name_node is None:
        return None
    raw = _text(name_node, source).strip()
    # Keep a qualified name for reports; use its final identifier for heuristic resolution.
    raw = re.sub(r"\s+", "", raw)
    return raw or None


def _simple_name(name: str) -> str:
    cleaned = name.split("::")[-1]
    cleaned = cleaned.split(".")[-1]
    cleaned = cleaned.split("->")[-1]
    return cleaned.lstrip("*&")


def _parameter_names(function_node, source: bytes) -> list[str]:
    declarator = function_node.child_by_field_name("declarator")
    if declarator is None:
        return []
    fdecl = next((n for n in _walk(declarator) if n.type == "function_declarator"), None)
    if fdecl is None:
        return []
    params = fdecl.child_by_field_name("parameters")
    if params is None:
        return []
    output = []
    for param in params.named_children:
        if param.type in {"comment", "variadic_parameter"}:
            continue
        decl = param.child_by_field_name("declarator")
        if decl is None:
            # Abstract/unnamed parameter: record a stable placeholder.
            output.append(f"<arg{len(output)}>")
            continue
        ids = [n for n in _walk(decl) if n.type in {"identifier", "field_identifier"}]
        output.append(_text(ids[-1], source) if ids else f"<arg{len(output)}>" )
    return output


def _condition_nodes(function_node):
    condition_types = {
        "if_statement", "else_clause", "while_statement", "do_statement",
        "for_statement", "switch_statement", "conditional_expression",
    }
    for node in _walk(function_node):
        if node.type not in condition_types:
            continue
        condition = node.child_by_field_name("condition")
        if condition is None and node.type in {"if_statement", "while_statement", "switch_statement", "conditional_expression"}:
            condition = node.named_children[0] if node.named_children else None
        if condition is not None:
            yield node, condition


def _params_usage(function_node, body, params: list[str], source: bytes) -> tuple[list[str], list[str]]:
    param_set = {p for p in params if not p.startswith("<arg")}
    control: set[str] = set()
    regions: list[tuple[int, int]] = []
    for _owner, condition in _condition_nodes(body):
        regions.append((condition.start_byte, condition.end_byte))
        control |= set(_descendant_identifier_names(condition, source)) & param_set
    data: set[str] = set()
    for node in _walk(body):
        if node.type not in {"identifier", "field_identifier"}:
            continue
        name = _text(node, source)
        if name not in param_set:
            continue
        if not any(start <= node.start_byte and node.end_byte <= end for start, end in regions):
            data.add(name)
    return sorted(control), sorted(data)


def _component_for(path: str, config: dict[str, Any]) -> str:
    components = config.get("components", {})
    matches = []
    normalized_path = path.replace("\\", "/")
    for name, prefixes in components.items():
        for prefix in prefixes:
            prefix = prefix.replace("\\", "/").rstrip("/")
            if normalized_path == prefix or normalized_path.startswith(prefix + "/"):
                matches.append((len(prefix), name))
    if matches:
        return max(matches)[1]
    parts = Path(path).parts
    return parts[0] if len(parts) > 1 else "root"


def _find_functions(root: Path, config: dict[str, Any]) -> tuple[dict[str, Function], dict[str, bytes], list[str]]:
    functions: dict[str, Function] = {}
    source_map: dict[str, bytes] = {}
    parse_warnings: list[str] = []
    files = []
    for directory, subdirs, filenames in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in SKIP_DIRS)
        base = Path(directory)
        files.extend(base / name for name in filenames if Path(name).suffix in EXTENSIONS)
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        try:
            source = path.read_bytes()
            tree = _parser_for(path).parse(source)
        except (OSError, ValueError) as exc:
            parse_warnings.append(f"{relative}: parse/read error: {exc}")
            continue
        source_map[relative] = source
        if tree.root_node.has_error:
            parse_warnings.append(f"{relative}: syntax errors or unsupported preprocessor-dependent syntax; results may be incomplete")
        for node in _walk(tree.root_node):
            if node.type != "function_definition":
                continue
            name = _function_name(node, source)
            if not name:
                continue
            params = _parameter_names(node, source)
            body = node.child_by_field_name("body")
            control, data = _params_usage(node, body or node, params, source)
            function_id = f"{relative}::{name}#{len(params)}"
            functions[function_id] = Function(
                id=function_id,
                name=name,
                path=relative,
                line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                params=params,
                control_params=control,
                data_params=data,
                component=_component_for(relative, config),
                arity=len(params),
            )
    return functions, source_map, parse_warnings


def _call_name(call_node, source: bytes) -> str:
    fn = call_node.child_by_field_name("function")
    raw = _text(fn, source) if fn is not None else ""
    raw = re.sub(r"\s*<.*>$", "", raw)
    return _simple_name(raw)


def _call_args(call_node, source: bytes) -> list[str]:
    arguments = call_node.child_by_field_name("arguments")
    if arguments is None:
        return []
    return [_text(node, source) for node in arguments.named_children if node.type != "comment"]


def _under_control_body(call_node) -> bool:
    node = call_node
    while node.parent is not None and node.parent.type != "function_definition":
        parent = node.parent
        if parent.type in {"if_statement", "while_statement", "for_statement", "switch_statement", "do_statement"}:
            condition = parent.child_by_field_name("condition")
            if condition is None or not (condition.start_byte <= call_node.start_byte and call_node.end_byte <= condition.end_byte):
                return True
        node = parent
    return False


def _build_edges(root: Path, functions: dict[str, Function], source_map: dict[str, bytes]) -> tuple[list[CallEdge], list[str]]:
    by_simple: dict[str, list[Function]] = defaultdict(list)
    for fn in functions.values():
        by_simple[_simple_name(fn.name)].append(fn)
    edges: list[CallEdge] = []
    unresolved: list[str] = []
    for path, source in source_map.items():
        try:
            tree = _parser_for(Path(path)).parse(source)
        except ValueError:
            continue
        function_nodes = {
            node.start_point.row + 1: node
            for node in _walk(tree.root_node)
            if node.type == "function_definition"
        }
        file_functions = [fn for fn in functions.values() if fn.path == path]
        for caller in file_functions:
            function_node = function_nodes.get(caller.line)
            if function_node is None:
                continue
            body = function_node.child_by_field_name("body")
            if body is None:
                continue
            for call in (n for n in _walk(body) if n.type == "call_expression"):
                name = _call_name(call, source)
                candidates = by_simple.get(name, [])
                args = _call_args(call, source)
                candidates = [candidate for candidate in candidates if candidate.arity == len(args)]
                if len(candidates) != 1:
                    if candidates:
                        unresolved.append(f"{path}:{call.start_point.row + 1}: ambiguous C/C++ call to {name} ({len(candidates)} candidates)")
                    continue
                callee = candidates[0]
                data = []
                control = []
                for idx, param in enumerate(callee.params):
                    if idx >= len(args) or param.startswith("<arg"):
                        continue
                    mapping = f"{param} <- {args[idx]}"
                    if param in callee.data_params or param in callee.control_params:
                        # A condition input is still data crossing the component boundary.
                        data.append(mapping)
                    if param in callee.control_params:
                        control.append(mapping)
                edges.append(CallEdge(
                    caller=caller.id,
                    callee=callee.id,
                    caller_component=caller.component,
                    callee_component=callee.component,
                    path=path,
                    line=call.start_point.row + 1,
                    arguments=args,
                    data_coupling=sorted(set(data)),
                    control_coupling=sorted(set(control)),
                    control_context=_under_control_body(call),
                ))
    return edges, unresolved


def _snapshot(root: Path, ref: str, config: dict[str, Any]) -> tuple[dict[str, Function], list[CallEdge], list[str], list[str]]:
    listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=root, text=True, capture_output=True)
    if listing.returncode:
        raise RuntimeError(f"cannot read Git ref {ref}: {listing.stderr.strip()}")
    with tempfile.TemporaryDirectory(prefix="ccdc-baseline-") as temp:
        temp_root = Path(temp)
        for item in listing.stdout.splitlines():
            rel = Path(item)
            if rel.suffix not in EXTENSIONS or any(part in SKIP_DIRS for part in rel.parts):
                continue
            blob = subprocess.run(["git", "show", f"{ref}:{item}"], cwd=root, capture_output=True)
            if blob.returncode:
                continue
            target = temp_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)
        functions, source_map, warnings = _find_functions(temp_root, config)
        edges, unresolved = _build_edges(temp_root, functions, source_map)
        return functions, edges, warnings, unresolved


def _edge_key(edge: CallEdge) -> tuple[Any, ...]:
    return (edge.caller, edge.callee, tuple(edge.arguments), tuple(edge.data_coupling), tuple(edge.control_coupling), edge.control_context)


def analyze_change(repo: Path, base_ref: str, head_ref: str, issue: dict[str, Any], out_dir: Path, config_path: Path | None = None, repo_label: str | None = None) -> dict[str, Path]:
    repo = repo.resolve()
    config = {}
    candidate_config = config_path or (repo / "ccdc.json")
    if candidate_config.exists():
        config = json.loads(candidate_config.read_text(encoding="utf-8"))
    changed = subprocess.run(["git", "diff", "--name-only", f"{base_ref}...{head_ref}"], cwd=repo, text=True, capture_output=True)
    if changed.returncode:
        raise RuntimeError(f"git diff failed: {changed.stderr.strip()}")
    changed_paths = [p for p in changed.stdout.splitlines() if Path(p).suffix in EXTENSIONS]

    # Analyze the selected head in the disposable clone, not the user's working tree.
    checkout = subprocess.run(["git", "checkout", "--detach", head_ref], cwd=repo, text=True, capture_output=True)
    if checkout.returncode:
        raise RuntimeError(f"cannot check out selected head {head_ref}: {checkout.stderr.strip()}")
    functions, source_map, warnings = _find_functions(repo, config)
    current_edges, unresolved = _build_edges(repo, functions, source_map)
    base_functions, base_edges, base_warnings, base_unresolved = _snapshot(repo, base_ref, config)

    changed_functions = sorted({
        fid for group in (functions, base_functions)
        for fid, fn in group.items() if fn.path in changed_paths
    })
    baseline_counts = Counter(_edge_key(e) for e in base_edges)
    edges: list[CallEdge] = []
    for edge in current_edges:
        key = _edge_key(edge)
        state = "unchanged" if baseline_counts[key] else "added"
        if baseline_counts[key]:
            baseline_counts[key] -= 1
        edges.append(CallEdge(**(asdict(edge) | {"state": state})))
    for edge in base_edges:
        key = _edge_key(edge)
        if baseline_counts[key]:
            baseline_counts[key] -= 1
            edges.append(CallEdge(**(asdict(edge) | {"state": "removed"})))

    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        forward[edge.caller].add(edge.callee)
        reverse[edge.callee].add(edge.caller)
    impacted = set(changed_functions)
    queue = list(changed_functions)
    while queue:
        node = queue.pop()
        for parent in reverse[node]:
            if parent not in impacted:
                impacted.add(parent)
                queue.append(parent)
    queue = list(impacted)
    while queue:
        node = queue.pop()
        for child in forward[node]:
            if child not in impacted:
                impacted.add(child)
                queue.append(child)

    reqs = config.get("requirements", {})
    tests = config.get("tests", {})
    affected_reqs = {key: value for key, value in reqs.items() if any(symbol in impacted for symbol in value)}
    affected_req_keys = set(affected_reqs)
    selected_tests = {key: req_ids for key, req_ids in tests.items() if affected_req_keys.intersection(req_ids)}
    relevant_edges = [e for e in edges if e.caller in impacted or e.callee in impacted]
    diff = subprocess.run(["git", "diff", "--no-ext-diff", "--unified=3", f"{base_ref}...{head_ref}", "--", *changed_paths], cwd=repo, text=True, capture_output=True)
    revision = subprocess.run(["git", "rev-parse", head_ref], cwd=repo, text=True, capture_output=True)
    report = {
        "tool": "ccdc-local",
        "version": "0.1.0",
        "language": "C/C++",
        "analysis_method": "Tree-sitter syntax parsing with heuristic direct-call resolution; static approximation",
        "issue": {k: issue.get(k) for k in ("key", "summary", "updated", "status", "url")},
        "repo": repo_label or str(repo),
        "base_ref": base_ref,
        "head_ref": head_ref,
        "head_commit": revision.stdout.strip() if revision.returncode == 0 else head_ref,
        "changed_files": changed_paths,
        "changed_functions": changed_functions,
        "impacted_functions": sorted(impacted),
        "affected_requirements": affected_reqs,
        "recommended_tests": selected_tests,
        "edges": [asdict(e) | {"cross_component": e.cross_component, "coupling_types": e.kinds} for e in relevant_edges],
        "unresolved_calls": sorted(set(unresolved + base_unresolved)),
        "parse_warnings": sorted(set(warnings + base_warnings)),
        "source_diff": diff.stdout,
        "limitations": [
            "This is heuristic source analysis, not compiler-semantic or object-code analysis.",
            "Indirect calls, templates, overload resolution, macros, aliasing, shared state, concurrency, and generated code may be incomplete.",
            "The tool does not execute tests or measure runtime coupling coverage; mapped tests are recommendations only.",
            "Review component boundaries and findings. This report is not DO-178B/C compliance evidence by itself.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": out_dir / "ccdc-report.md",
        "json": out_dir / "ccdc-report.json",
        "mermaid": out_dir / "impact-flow.mmd",
        "diff": out_dir / "source-diff.patch",
    }
    paths["json"].write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    paths["diff"].write_text(diff.stdout, encoding="utf-8")
    paths["mermaid"].write_text(_mermaid(report), encoding="utf-8")
    paths["markdown"].write_text(_markdown(report), encoding="utf-8")
    return paths


def _markdown(report: dict[str, Any]) -> str:
    edges = report.get("edges", [])
    control_edges = sum("control" in edge.get("coupling_types", []) for edge in edges)
    data_edges = sum("data" in edge.get("coupling_types", []) for edge in edges)
    cross_component_edges = sum(bool(edge.get("cross_component")) for edge in edges)
    lines = [
        "# C/C++ CC/DC impact report", "",
        f"- **Jira:** [{report['issue'].get('key')}]({report['issue'].get('url') or '#'}) — {report['issue'].get('summary')}",
        f"- **Issue updated:** {report['issue'].get('updated')} | **Status:** {report['issue'].get('status')}",
        f"- **Repository:** `{report['repo']}`",
        f"- **Compared:** `{report['base_ref']}` → `{report['head_ref']}` ({report['head_commit']})",
        f"- **Changed C/C++ files:** {len(report['changed_files'])}",
        "",
        "## At a glance",
        "| Measure | Count |",
        "|---|---:|",
        f"| Changed functions | {len(report['changed_functions'])} |",
        f"| Impacted functions | {len(report['impacted_functions'])} |",
        f"| CC edges — Control Coupling | {control_edges} |",
        f"| DC edges — Data Coupling | {data_edges} |",
        f"| Cross-component edges | {cross_component_edges} |",
        f"| Affected requirements | {len(report['affected_requirements'])} |",
        f"| Recommended tests | {len(report['recommended_tests'])} |",
        "",
        "**CC** indicates control coupling; **DC** indicates data coupling. Edge counts can overlap when one call has both types.",
        "> Static heuristic analysis. Review findings; this is not certification evidence by itself.",
        "",
        "## Changed files",
    ]
    lines += [f"- `{p}`" for p in report["changed_files"]] or ["- No C/C++ files in the selected Git diff."]
    lines += ["", "## Changed functions"]
    lines += [f"- `{f}`" for f in report["changed_functions"]] or ["- No function definitions mapped in changed files."]
    lines += ["", "## Impacted functions"]
    lines += [f"- `{f}`" for f in report["impacted_functions"]] or ["- None resolved."]
    lines += ["", "## Affected requirements"]
    if report["affected_requirements"]:
        lines += [f"- **{k}**: {', '.join('`'+x+'`' for x in v)}" for k, v in report["affected_requirements"].items()]
    else:
        lines.append("- No requirement map matched. Add mappings to `ccdc.json` for traceability.")
    lines += ["", "## Recommended tests"]
    if report["recommended_tests"]:
        lines += [f"- **{k}** (requirements: {', '.join(v)})" for k, v in report["recommended_tests"].items()]
    else:
        lines.append("- None mapped. Review the change and project test strategy.")
    lines += ["", "## Coupling edges"]
    for edge in report["edges"]:
        kind = ", ".join(edge["coupling_types"]) or "call only"
        scope = "inter-component" if edge["cross_component"] else "intra-component"
        lines.append(f"- `{edge['caller']}` → `{edge['callee']}` — **{kind}**, {scope}, **{edge['state']}** (`{edge['path']}:{edge['line']}`)")
        if edge["data_coupling"]:
            lines.append(f"  - Data: {', '.join(edge['data_coupling'])}")
        if edge["control_coupling"]:
            lines.append(f"  - Control inputs: {', '.join(edge['control_coupling'])}")
        if edge["control_context"]:
            lines.append("  - Call is under a conditional/loop path.")
    if not report["edges"]:
        lines.append("- No resolvable calls in the impact cone.")
    lines += ["", "## Parse and resolution notes"]
    notes = report["parse_warnings"] + report["unresolved_calls"]
    lines += [f"- {note}" for note in notes] or ["- No parser warnings or ambiguous calls reported."]
    lines += ["", "## Flow diagram", "The native desktop GUI renders the call/CC/DC graph from these findings. The accompanying `impact-flow.mmd` is its Mermaid source for external Mermaid-compatible viewers.", "", "## Source diff", "See the accompanying `source-diff.patch`.", "", "## Limitations"]
    lines += [f"- {x}" for x in report["limitations"]]
    return "\n".join(lines) + "\n"


def _mermaid(report: dict[str, Any]) -> str:
    lines = ["flowchart LR"]
    changed, impacted = set(report["changed_functions"]), set(report["impacted_functions"])
    names = set(changed) | set(impacted)
    for edge in report["edges"]:
        names.update((edge["caller"], edge["callee"]))
    ids = {name: f"N{index}" for index, name in enumerate(sorted(names))}
    for name, node_id in ids.items():
        label = name.replace('"', "'")
        lines.append(f'  {node_id}["{label}"]')
    for edge in report["edges"]:
        label = ", ".join(edge["coupling_types"]) or "call"
        label += f", {edge['state']}"
        lines.append(f'  {ids[edge["caller"]]} -->|"{label}"| {ids[edge["callee"]]}')
    for name, node_id in ids.items():
        if name in changed:
            lines.append(f"  style {node_id} fill:#ffd6d6,stroke:#c00,stroke-width:3px")
        elif name in impacted:
            lines.append(f"  style {node_id} fill:#fff0c2,stroke:#a66b00,stroke-width:2px")
    if len(lines) == 1:
        lines.append('  Empty["No resolved flows"]')
    return "\n".join(lines) + "\n"
