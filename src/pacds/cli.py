"""The `pacds` command: serve the service, run the preflight, or use the evaluation toolkit."""

from __future__ import annotations

import sys

COMMANDS = {
    "serve": "run the service (configured by PACDS_* environment variables and PACDS_CONFIG)",
    "check-llm": "check that the configured LLM can serve PACDS",
    "eval": "evaluation toolkit: run, audit, casebook, analysis, runs (pacds eval --help)",
}


def _help(prog: str = "pacds", commands: dict[str, str] = COMMANDS, file=None) -> None:
    lines = [f"usage: {prog} COMMAND [ARGS...]", "", "commands:"]
    lines += [f"  {name:<12}{text}" for name, text in commands.items()]
    lines += ["", f"Run '{prog} COMMAND --help' for a command's options."]
    print("\n".join(lines), file=file or sys.stdout)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        _help()
        return 0
    command, rest = args[0], args[1:]
    if command == "serve":
        if rest:
            print("usage: pacds serve\n\nRuns the service; it takes no arguments (see PACDS_CONFIG, PACDS_LISTEN_HOST, PACDS_LISTEN_PORT).",
                  file=sys.stdout if rest[0] in ("-h", "--help") else sys.stderr)
            return 0 if rest[0] in ("-h", "--help") else 2
        from pacds.main import main as serve

        serve()
        return 0
    if command == "check-llm":
        from pacds.devtools.check_llm import main as check_llm

        sys.argv = ["pacds check-llm", *rest]
        check_llm()
        return 0
    if command == "eval":
        from pacds_eval.cli import main as eval_main

        return eval_main(rest)
    print(f"pacds: unknown command '{command}'\n", file=sys.stderr)
    _help(file=sys.stderr)
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
