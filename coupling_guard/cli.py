from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyzer import analyze, build_report, write_reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coupling-guard",
        description="Static Python CC/DC flow and change-impact prototype.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("analyze", help="analyze source and write reports")
    command.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    command.add_argument("--config", type=Path, default=Path("coupling.json"), help="JSON component/requirements/test map")
    command.add_argument("--changed", action="append", default=[], help="changed path relative to root; repeatable")
    command.add_argument("--base-ref", help="git base ref; changed paths come from git diff BASE...HEAD")
    command.add_argument("--head-ref", help="git head ref (default: HEAD)")
    command.add_argument("--out", type=Path, default=Path("coupling-report"), help="output directory")
    command.add_argument("--fail-on-unresolved", action="store_true", help="exit nonzero if changed Python files cannot be mapped")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    config_path = args.config if args.config.is_absolute() else root / args.config
    out_dir = args.out if args.out.is_absolute() else root / args.out
    try:
        analysis = analyze(root, config_path, args.changed, args.base_ref, args.head_ref)
        outputs = write_reports(analysis, out_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"coupling-guard: error: {exc}", file=sys.stderr)
        return 2

    report = build_report(analysis)
    print(f"Analyzed {len(analysis.functions)} functions and {len(analysis.edges)} resolved calls.")
    print(f"Changed files: {len(analysis.changed_files)}; changed functions: {len(analysis.changed_nodes)}; impacted functions: {len(analysis.impacted_nodes)}")
    print("Reports:")
    for kind, path in outputs.items():
        print(f"  {kind}: {path.relative_to(root) if path.is_relative_to(root) else path}")
    if report["recommended_tests"]:
        print("Recommended tests: " + ", ".join(report["recommended_tests"]))
    else:
        print("Recommended tests: none mapped (review the requirements/tests configuration).")
    if args.fail_on_unresolved and analysis.unresolved_changed_files:
        print("Unresolved changed Python files: " + ", ".join(analysis.unresolved_changed_files), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
