"""`pacds eval`: the evaluation toolkit's subcommands."""

from __future__ import annotations

import importlib
import sys

from pacds.cli import _help

ANALYSIS = {
    "report": "write a report for one or more runs",
    "compare": "compare two runs case by case",
    "select": "print the case ids a --select filter picks",
    "sample": "print or write the regression sample",
    "context": "print the context of a case for review",
    "classify": "classify a run's misses",
}
MODULES = {
    "casebook": "pacds_eval.casebook",
    "catalog": "pacds_eval.catalog",
    "runs": "pacds_eval.runs",
    "replay": "pacds_eval.harness",
    "support": "pacds_eval.support_agent.run",
}
COMMANDS = {
    "run": "run an evaluation (Compose stack or --target)",
    "audit": "audit a deployment",
    "casebook": "import, review and label case sets",
    "catalog": "regenerate or check a case set's CATALOG.md",
    "runs": "list, fetch, archive and manage run directories",
    "replay": "replay cases against PACDS or the baseline",
    "support": "run the support-agent evaluation",
    **ANALYSIS,
}


def _call(name: str, module: str, args: list[str]) -> int:
    main = importlib.import_module(module).main
    saved = sys.argv
    sys.argv = [f"pacds eval {name}", *args]
    try:
        main()
    finally:
        sys.argv = saved
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        _help("pacds eval", COMMANDS)
        return 0
    command, rest = args[0], args[1:]
    if command == "run":
        from pacds_eval.run import main as run_main

        return run_main(rest)
    if command == "audit":
        from pacds_eval.audit import main as audit_main

        return audit_main(rest)
    if command in ANALYSIS:
        return _call(command, "pacds_eval.analysis.__main__", [command, *rest])
    if command in MODULES:
        return _call(command, MODULES[command], rest)
    print(f"pacds eval: unknown command '{command}'\n", file=sys.stderr)
    _help("pacds eval", COMMANDS, file=sys.stderr)
    return 2
