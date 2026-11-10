from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FunctionInfo:
    id: str
    module: str
    name: str
    path: str
    line: int
    end_line: int
    params: list[str]
    control_params: list[str] = field(default_factory=list)
    data_params: list[str] = field(default_factory=list)
    component: str = "Unassigned"


@dataclass
class Edge:
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
    resolution: str = "resolved"
    change_state: str = "current"

    @property
    def coupling_types(self) -> list[str]:
        kinds = []
        if self.data_coupling:
            kinds.append("data")
        if self.control_coupling or self.control_context:
            kinds.append("control")
        return kinds


@dataclass
class Analysis:
    root: Path
    functions: dict[str, FunctionInfo]
    edges: list[Edge]
    changed_files: list[str]
    changed_nodes: list[str]
    impacted_nodes: list[str]
    requirements: dict[str, list[str]]
    tests: dict[str, list[str]]
    unresolved_changed_files: list[str]
    source_revision: str
    config_sha256: str
    base_ref: str | None
    head_ref: str | None


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class _FunctionUseVisitor(ast.NodeVisitor):
    """Find parameter reads and parameters that influence conditional decisions."""

    def __init__(self, params: set[str]):
        self.params = params
        self.control_names: set[str] = set()
        self.data_names: set[str] = set()

    def visit_If(self, node: ast.If) -> None:
        self.control_names |= _names_in(node.test) & self.params
        self.visit(node.test)
        for child in node.body + node.orelse:
            self.visit(child)

    def visit_While(self, node: ast.While) -> None:
        self.control_names |= _names_in(node.test) & self.params
        self.visit(node.test)
        for child in node.body + node.orelse:
            self.visit(child)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.params:
            self.data_names.add(node.id)


class _CallVisitor(ast.NodeVisitor):
    def __init__(self, module: str, imports: dict[str, str]):
        self.module = module
        self.imports = imports
        self.controlled = False
        self.calls: list[dict[str, Any]] = []

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        old = self.controlled
        self.controlled = True
        for child in node.body + node.orelse:
            self.visit(child)
        self.controlled = old

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        old = self.controlled
        self.controlled = True
        for child in node.body + node.orelse:
            self.visit(child)
        self.controlled = old

    def visit_Call(self, node: ast.Call) -> None:
        target = None
        if isinstance(node.func, ast.Name):
            target = self.imports.get(node.func.id, f"{self.module}.{node.func.id}")
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            alias = node.func.value.id
            prefix = self.imports.get(alias, alias)
            target = f"{prefix}.{node.func.attr}"
        args = [ast.unparse(arg) for arg in node.args]
        self.calls.append({
            "target": target,
            "line": node.lineno,
            "arguments": args + [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords],
            "arg_nodes": list(node.args),
            "keyword_nodes": list(node.keywords),
            "control_context": self.controlled,
        })
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Nested function bodies are not attributed to the enclosing function.
        return

    visit_AsyncFunctionDef = visit_FunctionDef


def _component_for(path: str, components: dict[str, list[str]]) -> str:
    matches = []
    for name, prefixes in components.items():
        for prefix in prefixes:
            normalized = prefix.replace("\\", "/").rstrip("/")
            if path == normalized or path.startswith(normalized + "/"):
                matches.append((len(normalized), name))
    return max(matches)[1] if matches else "Unassigned"


def _in_scope(path: Path, include: list[str] | None) -> bool:
    prefixes = [Path(item) for item in include or ["."]]
    return any(
        str(prefix) == "." or path == prefix or prefix in path.parents
        for prefix in prefixes
    )


def _load_python(root: Path, components: dict[str, list[str]], include: list[str] | None = None) -> tuple[dict[str, FunctionInfo], list[tuple[str, dict[str, str], ast.FunctionDef, str]]]:
    functions: dict[str, FunctionInfo] = {}
    pending: list[tuple[str, dict[str, str], ast.FunctionDef, str]] = []
    ignored = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist"}
    for path in sorted(root.rglob("*.py")):
        relative_path = path.relative_to(root)
        if any(part in ignored for part in relative_path.parts) or not _in_scope(relative_path, include):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        module = _module_name(root, path)
        relative = relative_path.as_posix()
        imports: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                if node.level:
                    parent = module.split(".")[:-node.level]
                    base = ".".join(parent + ([node.module] if node.module else []))
                base = base.strip(".")
                for alias in node.names:
                    imports[alias.asname or alias.name] = f"{base}.{alias.name}".strip(".")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            params = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
            if args.vararg:
                params.append(args.vararg.arg)
            if args.kwarg:
                params.append(args.kwarg.arg)
            visitor = _FunctionUseVisitor(set(params))
            for child in node.body:
                visitor.visit(child)
            function_id = f"{module}.{node.name}"
            functions[function_id] = FunctionInfo(
                id=function_id,
                module=module,
                name=node.name,
                path=relative,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                params=params,
                control_params=sorted(visitor.control_names),
                data_params=sorted(visitor.data_names),
                component=_component_for(relative, components),
            )
            pending.append((module, imports, node, relative))
    return functions, pending


def _resolve_target(target: str | None, functions: dict[str, FunctionInfo]) -> str | None:
    if not target:
        return None
    if target in functions:
        return target
    candidates = [fid for fid in functions if fid.endswith("." + target)]
    return candidates[0] if len(candidates) == 1 else None


def _build_edges(root: Path, functions: dict[str, FunctionInfo], pending: list[tuple[str, dict[str, str], ast.FunctionDef, str]]) -> list[Edge]:
    edges: list[Edge] = []
    for module, imports, node, relative in pending:
        caller_id = f"{module}.{node.name}"
        visitor = _CallVisitor(module, imports)
        for child in node.body:
            visitor.visit(child)
        for call in visitor.calls:
            callee_id = _resolve_target(call["target"], functions)
            if not callee_id or callee_id == caller_id:
                continue
            callee = functions[callee_id]
            param_to_expr: dict[str, str] = {}
            for idx, arg in enumerate(call["arg_nodes"]):
                if idx < len(callee.params):
                    param_to_expr[callee.params[idx]] = ast.unparse(arg)
            for kw in call["keyword_nodes"]:
                if kw.arg:
                    param_to_expr[kw.arg] = ast.unparse(kw.value)
            data = [f"{param} <- {param_to_expr[param]}" for param in callee.data_params if param in param_to_expr]
            control = [f"{param} <- {param_to_expr[param]}" for param in callee.control_params if param in param_to_expr]
            # Data dependence and control influence are independent labels: a
            # value used in a decision is still an externally supplied datum.
            for param in callee.control_params:
                if param in param_to_expr and not any(item.startswith(param + " <-") for item in data):
                    data.append(f"{param} <- {param_to_expr[param]}")
            edges.append(Edge(
                caller=caller_id,
                callee=callee_id,
                caller_component=functions[caller_id].component,
                callee_component=callee.component,
                path=relative,
                line=call["line"],
                arguments=call["arguments"],
                data_coupling=sorted(data),
                control_coupling=sorted(control),
                control_context=call["control_context"],
            ))
    return edges


def _baseline_snapshot(root: Path, ref: str, components: dict[str, list[str]], include: list[str] | None) -> tuple[dict[str, FunctionInfo], list[Edge]]:
    """Materialize configured Python files from a Git ref into a temporary snapshot."""
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if listing.returncode != 0:
        raise RuntimeError(f"cannot list baseline {ref}: {listing.stderr.strip()}")
    with tempfile.TemporaryDirectory(prefix="coupling-guard-baseline-") as temp_name:
        snapshot = Path(temp_name)
        for git_path in listing.stdout.splitlines():
            candidate = Path(git_path)
            if candidate.suffix != ".py" or not _in_scope(candidate, include):
                continue
            blob = subprocess.run(
                ["git", "show", f"{ref}:{git_path}"],
                cwd=root, capture_output=True, check=False,
            )
            if blob.returncode != 0:
                continue
            destination = snapshot / candidate
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(blob.stdout)
        baseline_functions, pending = _load_python(snapshot, components, include)
        baseline_edges = _build_edges(snapshot, baseline_functions, pending)
        return baseline_functions, baseline_edges


def _edge_key(edge: Edge) -> tuple[Any, ...]:
    return (
        edge.caller, edge.callee, tuple(edge.arguments), tuple(edge.data_coupling),
        tuple(edge.control_coupling), edge.control_context,
    )


def _changed_files(root: Path, changed: list[str] | None, base_ref: str | None, head_ref: str | None) -> list[str]:
    if changed:
        return sorted({Path(p).as_posix() for p in changed})
    if not base_ref:
        return []
    head = head_ref or "HEAD"
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...{head}"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def analyze(root: Path, config_path: Path, changed: list[str] | None = None, base_ref: str | None = None, head_ref: str | None = None) -> Analysis:
    root = root.resolve()
    config_path = config_path if config_path.is_absolute() else root / config_path
    config = json.loads(config_path.read_text(encoding="utf-8"))
    components = config.get("components", {})
    include = config.get("include")
    functions, pending = _load_python(root, components, include)
    current_edges = _build_edges(root, functions, pending)
    changed_paths = _changed_files(root, changed, base_ref, head_ref)

    baseline_functions: dict[str, FunctionInfo] = {}
    baseline_edges: list[Edge] = []
    if base_ref:
        baseline_functions, baseline_edges = _baseline_snapshot(root, base_ref, components, include)

    # Tag graph edges as unchanged, added, or removed across the two snapshots.
    baseline_counts = Counter(_edge_key(edge) for edge in baseline_edges)
    report_edges: list[Edge] = []
    for edge in current_edges:
        key = _edge_key(edge)
        state = "unchanged" if baseline_counts[key] else ("added" if base_ref else "current")
        if baseline_counts[key]:
            baseline_counts[key] -= 1
        report_edges.append(Edge(**(asdict(edge) | {"change_state": state})))
    if base_ref:
        for edge in baseline_edges:
            key = _edge_key(edge)
            if baseline_counts[key]:
                baseline_counts[key] -= 1
                report_edges.append(Edge(**(asdict(edge) | {"change_state": "removed"})))

    changed_nodes = sorted({
        fid for snapshot_functions in (baseline_functions, functions)
        for fid, fn in snapshot_functions.items() if fn.path in changed_paths
    })
    parsed_paths = {fn.path for fn in functions.values()} | {fn.path for fn in baseline_functions.values()}
    unresolved = [
        p for p in changed_paths
        if p.endswith(".py") and _in_scope(Path(p), include) and p not in parsed_paths
    ]

    # Conservative impact cone across both snapshots: changed functions, their
    # callers, then reachable downstream interactions. File-level changes are
    # intentionally conservative because this prototype does not diff AST nodes.
    reverse: dict[str, set[str]] = {}
    forward: dict[str, set[str]] = {}
    all_nodes = set(functions) | set(baseline_functions)
    for edge in report_edges:
        forward.setdefault(edge.caller, set()).add(edge.callee)
        reverse.setdefault(edge.callee, set()).add(edge.caller)
        all_nodes.update((edge.caller, edge.callee))
    impacted = set(changed_nodes)
    work = list(changed_nodes)
    while work:
        current = work.pop()
        for parent in reverse.get(current, set()):
            if parent not in impacted:
                impacted.add(parent)
                work.append(parent)
    work = list(impacted)
    while work:
        current = work.pop()
        for child in forward.get(current, set()):
            if child not in impacted:
                impacted.add(child)
                work.append(child)

    requirements = {str(k): list(v) for k, v in config.get("requirements", {}).items()}
    tests = {str(k): list(v) for k, v in config.get("tests", {}).items()}
    revision_result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    )
    revision = revision_result.stdout.strip() if revision_result.returncode == 0 else "unknown"
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return Analysis(
        root, functions, report_edges, changed_paths, changed_nodes,
        sorted(impacted), requirements, tests, sorted(unresolved), revision,
        config_hash, base_ref, head_ref,
    )


def build_report(analysis: Analysis) -> dict[str, Any]:
    impacted = set(analysis.impacted_nodes)
    affected_requirements = {
        req: symbols for req, symbols in analysis.requirements.items()
        if any(symbol in impacted for symbol in symbols)
    }
    affected_req_ids = set(affected_requirements)
    affected_tests = {
        test: reqs for test, reqs in analysis.tests.items()
        if affected_req_ids.intersection(reqs)
    }
    relevant_edges = [e for e in analysis.edges if e.caller in impacted or e.callee in impacted]
    return {
        "tool": "coupling-guard",
        "version": "0.1.0",
        "analysis_scope": "Python source; static approximation",
        "source_revision": analysis.source_revision,
        "base_ref": analysis.base_ref,
        "head_ref": analysis.head_ref,
        "config_sha256": analysis.config_sha256,
        "changed_files": analysis.changed_files,
        "changed_functions": analysis.changed_nodes,
        "impacted_functions": analysis.impacted_nodes,
        "unresolved_changed_files": analysis.unresolved_changed_files,
        "affected_requirements": affected_requirements,
        "recommended_tests": affected_tests,
        "edges": [asdict(e) | {"coupling_types": e.coupling_types} for e in relevant_edges],
        "limitations": [
            "Static results are approximate and do not prove runtime execution or certification compliance.",
            "Only top-level Python functions and resolvable direct calls are modeled.",
            "Test links are recommendations from configuration; runtime test coverage is not collected by this prototype.",
            "The impact cone is file-granular and may over-report; review indirect calls, shared state, generated code, and unsupported constructs.",
        ],
    }


def report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Coupling Guard change-impact report",
        "",
        f"**Scope:** {report['analysis_scope']}",
        f"**Source revision:** `{report['source_revision']}`",
        f"**Compared refs:** `{report['base_ref'] or 'not supplied'}` → `{report['head_ref'] or 'not supplied'}`",
        f"**Configuration SHA-256:** `{report['config_sha256']}`",
        "",
        "> Prototype static analysis only. This report is not certification evidence by itself.",
        "",
        "## Changed files",
    ]
    lines += [f"- `{p}`" for p in report["changed_files"]] or ["- No changed files supplied."]
    lines += ["", "## Changed functions"]
    lines += [f"- `{n}`" for n in report["changed_functions"]] or ["- No changed functions mapped."]
    lines += ["", "## Impacted functions"]
    lines += [f"- `{n}`" for n in report["impacted_functions"]] or ["- None identified."]
    lines += ["", "## Affected requirements"]
    if report["affected_requirements"]:
        for req, symbols in report["affected_requirements"].items():
            lines.append(f"- **{req}**: " + ", ".join(f"`{s}`" for s in symbols))
    else:
        lines.append("- No requirement mappings matched the impacted functions.")
    lines += ["", "## Recommended tests"]
    if report["recommended_tests"]:
        for test, reqs in report["recommended_tests"].items():
            lines.append(f"- **{test}** (requirements: {', '.join(reqs)})")
    else:
        lines.append("- No test mappings matched. Review requirement/test configuration.")
    lines += ["", "## Coupling edges"]
    if report["edges"]:
        for edge in report["edges"]:
            kinds = ", ".join(edge["coupling_types"]) or "call only"
            detail = []
            if edge["data_coupling"]:
                detail.append("data=" + "; ".join(edge["data_coupling"]))
            if edge["control_coupling"]:
                detail.append("control=" + "; ".join(edge["control_coupling"]))
            if edge["control_context"]:
                detail.append("call is inside a conditional path")
            lines.append(f"- `{edge['caller']}` → `{edge['callee']}` ({kinds}, {edge['change_state']}), `{edge['path']}:{edge['line']}`" + (" — " + "; ".join(detail) if detail else ""))
    else:
        lines.append("- No resolvable edges in the impact cone.")
    lines += ["", "## Unresolved changed files"]
    lines += [f"- `{p}`" for p in report["unresolved_changed_files"]] or ["- None."]
    lines += ["", "## Limitations"] + [f"- {x}" for x in report["limitations"]]
    return "\n".join(lines) + "\n"


def report_mermaid(report: dict[str, Any]) -> str:
    edges = report["edges"]
    changed = set(report["changed_functions"])
    impacted = set(report["impacted_functions"])
    nodes = set(changed) | set(impacted)
    for edge in edges:
        nodes.add(edge["caller"])
        nodes.add(edge["callee"])
    ids = {node: f"N{index}" for index, node in enumerate(sorted(nodes))}
    lines = ["flowchart LR"]
    for node, node_id in ids.items():
        label = node.replace('"', "'")
        lines.append(f'  {node_id}["{label}"]')
    for edge in edges:
        label = ", ".join(edge["coupling_types"]) or "call"
        if edge["change_state"] in {"added", "removed"}:
            label += f", {edge['change_state']}"
        if edge["control_context"] and "control" not in edge["coupling_types"]:
            label += ", control context"
        lines.append(f'  {ids[edge["caller"]]} -->|"{label}"| {ids[edge["callee"]]}')
    for node, node_id in ids.items():
        if node in changed:
            lines.append(f"  style {node_id} fill:#ffd6d6,stroke:#c00,stroke-width:3px")
        elif node in impacted:
            lines.append(f"  style {node_id} fill:#fff0c2,stroke:#a66b00,stroke-width:2px")
    if not ids:
        lines.append('  Empty["No resolvable flows"]')
    return "\n".join(lines) + "\n"


def write_reports(analysis: Analysis, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(analysis)
    outputs = {
        "json": out_dir / "coupling-report.json",
        "markdown": out_dir / "coupling-report.md",
        "mermaid": out_dir / "impact-flow.mmd",
    }
    outputs["json"].write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    outputs["markdown"].write_text(report_markdown(report), encoding="utf-8")
    outputs["mermaid"].write_text(report_mermaid(report), encoding="utf-8")
    return outputs
