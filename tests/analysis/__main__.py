"""Offline analysis of evaluation runs. No LLM calls.

Usage: python -m tests.analysis report RUN [RUN ...] [--out DIR]   write RUN/report/ (report.md, report.json, cases.jsonl, costs.json)
       python -m tests.analysis compare RUN_A RUN_B [--a NAME --b NAME] [--out FILE]
       python -m tests.analysis select RUN_OR_RESULTS --select FILTER ... [--kind support|replay] [--variant V]
       python -m tests.analysis sample [--candidates] [--per-class 3] [--write]
RUN is a run directory (eval-runs/<name>); fetch archived runs with python -m tests.eval_run fetch NAME. Several runs of one
milestone (e.g. one per repeat) are analysed as one: report RUN1 RUN2 RUN3 --out DIR, or RUN1,RUN2,RUN3 in compare and select.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tests.analysis import compare as compare_module
from tests.analysis.load import load_runs, parse_runs
from tests.analysis.report import write_report
from tests.analysis.select import draw_regression_sample, resolve_evaluation, select_cases, write_regression_sample


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.analysis", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="write RUN/report/")
    report.add_argument("runs", type=Path, nargs="+")
    report.add_argument("--out", type=Path, help="report directory (required for several runs)")
    compare = commands.add_parser("compare", help="compare two runs case by case")
    compare.add_argument("run_a", help="run directory, or RUN1,RUN2,... for one milestone")
    compare.add_argument("run_b")
    compare.add_argument("--a", help="results file in RUN_A (default: pair evaluations by kind and variant)")
    compare.add_argument("--b", help="results file in RUN_B")
    compare.add_argument("--out", type=Path)
    select = commands.add_parser("select", help="print the case ids a --select filter picks")
    select.add_argument("run", help="run directory, results file, or RUN1,RUN2,...")
    select.add_argument("--select", action="append", required=True)
    select.add_argument("--kind", default="support", choices=("support", "replay"))
    select.add_argument("--variant", default="full")
    sample = commands.add_parser("sample", help="draw the regression sample for a case set")
    sample.add_argument("--candidates", action="store_true", help="the Debezium candidates instead of tests/replay/cases")
    sample.add_argument("--per-class", type=int, default=3)
    sample.add_argument("--write", action="store_true", help="write <cases dir>/regression-sample.json")
    args = parser.parse_args()

    try:
        if args.command == "report":
            if len(args.runs) > 1 and not args.out:
                raise ValueError("several runs need --out DIR for their combined report")
            out = write_report(load_runs(args.runs), args.out)
            print((out / "report.md").read_text())
            print(f"=== report: {out}")
        elif args.command == "compare":
            run_a, run_b = load_runs(parse_runs(args.run_a)), load_runs(parse_runs(args.run_b))
            text = compare_module.render(run_a, run_b, compare_module.compare(run_a, run_b, args.a, args.b))
            print(text)
            if args.out:
                args.out.write_text(text)
        elif args.command == "select":
            _, evaluation = resolve_evaluation(args.run, args.kind, args.variant)
            print(" ".join(f"--case {case}" for case in select_cases(evaluation, args.select)))
        elif args.command == "sample":
            from tests.replay.harness import CANDIDATES_DIR, CASES_DIR, load_cases

            cases_dir = CANDIDATES_DIR if args.candidates else CASES_DIR
            chosen = draw_regression_sample(load_cases(cases_dir), args.per_class)
            print(" ".join(chosen))
            if args.write:
                print(f"=== written: {write_regression_sample(cases_dir, chosen, args.per_class)}")
    except ValueError as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
