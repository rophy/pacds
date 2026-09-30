# Packaging and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship PACDS as one published image (`ghcr.io/rophy/pacds`, CLI `pacds serve|check-llm|eval ...`), a deploy
bundle and a support-agent sample, built and released by GitHub Actions on a version bump, so a corporate deployment
needs no git checkout.

**Architecture:** The evaluation toolkit moves from `tests/` into a second package `src/pacds_eval/` in the same
distribution; a `pacds` dispatcher fronts the service and the toolkit; `eval.sh --target` is ported to
`pacds eval run`; the exfiltration audit becomes `pacds eval audit`; a standalone sample agent lives in
`samples/support-agent/`. CI tests on every push and, on `master`, publishes when `pyproject.toml`'s version is new.

**Tech Stack:** Python 3.12, uv / uv_build, argparse, FastAPI (unchanged), Docker buildx (amd64 + arm64), GitHub
Actions, GHCR.

**Spec:** `docs/superpowers/specs/2026-09-30-packaging-and-release-design.md` (committed on `master` as 5c4197b; copy
it into this branch in Task 1).

## Global Constraints

- One version for everything: `pyproject.toml` `version`; this plan ends at `2.1.0`.
- One image `ghcr.io/rophy/pacds`, `ENTRYPOINT ["pacds"]`, no default command; `pacds` with no arguments prints help
  and exits 0; every compose file sets `command: ["serve"]`.
- `src/pacds` never imports `pacds_eval` (the dispatcher imports it lazily inside the `eval` branch only).
- No behaviour, file-format (`case.json`, run directories, traces) or prompt-text changes from the move: request hashes
  must not change.
- The package has no default case set: every command takes `--cases-dir` / `PACDS_CASES_DIR`.
- No compatibility shims for the old command names or default command.
- Actions pinned to full commit SHA with the exact release tag in a comment; build always, push/release only when the
  version is new (skip, not fail).
- Historical records are not rewritten: `docs/evaluation/*`, `docs/superpowers/specs/*` and `docs/superpowers/plans/*`
  (other than this plan) keep their old command names. Live docs (`README.md`, `CLAUDE.md`, `docs/deployment.md`,
  `docs/evaluation-runbook.md`) are updated.
- Commit messages `<type>: <short description>`, types feat/fix/refactor/chore/docs/build/test; no attribution lines,
  no mention of any AI assistant. Never push, never merge, never publish: the user does that.
- `uv run pytest tests/unit tests/contract -q` green after every task; check exit codes directly, not through a pipe.
- Private domains or hostnames never appear in committed files; use `*.corp.example`.

## Branch and ruling

Work on a new branch `feat/packaging` created from `feat/claude-code-backend` in the worktree
`/home/rophy/projects/pacds-claude-code` (`git checkout -b feat/packaging`). Spec §7 step 1 ("merge the Claude Code
backend branch first") is replaced by this: both branches reach `master` together when the user merges. The spec's
"skills single source in `samples/support-agent/skills/`" is kept as a single source, but its canonical location is
`src/pacds_eval/skills/` (package data ships only from `src/`), and `samples/support-agent/skills` is a symlink to it;
tarballs and the image dereference it.

## File map

| Now | After |
|---|---|
| `tests/replay/{harness,baseline,casebook,catalog}.py` | `src/pacds_eval/{harness,baseline,casebook,catalog}.py` |
| `tests/replay/prompts/` | `src/pacds_eval/prompts/` (review.md) |
| `tests/analysis/` (package incl. `prompts/classify.md`) | `src/pacds_eval/analysis/` (classify.md → `src/pacds_eval/prompts/`) |
| `tests/support_agent/{agent,run}.py` | `src/pacds_eval/support_agent/{agent,run}.py` |
| `tests/support_agent/skills/` | `src/pacds_eval/skills/` (+ symlink `samples/support-agent/skills`) |
| `tests/eval_run.py` | `src/pacds_eval/runs.py` |
| `tests/oidc.py`, `tests/s3.py` | `src/pacds_eval/oidc.py`, `src/pacds_eval/s3.py` |
| `tests/security/{test_exfiltration.py,attack-vectors.json}` | `src/pacds_eval/audit.py`, `src/pacds_eval/attack-vectors.json` |
| `tests/replay/cases/` | `cases/github/` |
| `tests/replay/candidates/` (incl. `regression-sample.json`, `rejected.json`) | `cases/debezium/` |
| `tests/replay/CATALOG.md` | `cases/github/CATALOG.md`, `cases/debezium/CATALOG.md` (per set) |
| — | `src/pacds/cli.py` (dispatcher), `src/pacds_eval/cli.py`, `src/pacds_eval/run.py` |
| — | `samples/support-agent/` |
| — | `.github/workflows/ci.yaml`, `scripts/build-bundles.sh`, `scripts/image-smoke.sh` |

Module renames for imports: `tests.replay.harness` → `pacds_eval.harness`, `tests.replay.baseline` →
`pacds_eval.baseline`, `tests.replay.casebook` → `pacds_eval.casebook`, `tests.replay.catalog` → `pacds_eval.catalog`,
`tests.analysis` → `pacds_eval.analysis`, `tests.support_agent` → `pacds_eval.support_agent`, `tests.eval_run` →
`pacds_eval.runs`, `tests.oidc` → `pacds_eval.oidc`, `tests.s3` → `pacds_eval.s3`.

---

### Task 1: Branch, spec, and a replay-parity baseline run

**Files:** branch only; `docs/superpowers/specs/2026-09-30-packaging-and-release-design.md` (copied); a recorded run
under `eval-runs/` (git-ignored).

**Produces:** branch `feat/packaging`; `eval-runs/<PARITY_RUN>` recorded with the *current* code, used in Task 12.

- [ ] **Step 1:** In `/home/rophy/projects/pacds-claude-code`: `git status` must be clean (only untracked `.env`,
  `compose.override.yaml`, `eval-runs/`, `.superpowers/`). `git checkout -b feat/packaging`.
- [ ] **Step 2:** `git show master:docs/superpowers/specs/2026-09-30-packaging-and-release-design.md > docs/superpowers/specs/2026-09-30-packaging-and-release-design.md`
  (the worktree shares the repository, so `master` is visible). Commit: `docs: bring the packaging design onto the branch`.
- [ ] **Step 3: record the parity baseline** (costs a little subscription usage on haiku). `.env` already sets
  `LLM_API=claude_code`; run with the model overridden:
  ```bash
  sed -i 's/^LLM_MODEL=.*/LLM_MODEL=haiku/' .env
  PACDS_OIDC_URL=http://localhost:3013 ./scripts/eval.sh --replay "--set clear" --replay "--baseline --set clear" > /tmp/parity-baseline.log 2>&1; echo exit=$?
  ```
  Expected `exit=0`. Note the run directory printed on the first line of the log as `PARITY_RUN` in the report. (The
  support agent with PACDS never replays, so it is not part of the parity run.) Restore `LLM_MODEL` to what it was
  (`claude-opus-5-5`) afterwards.

---

### Task 2: Fix the two bugs from the opus run

**Files:** `tests/analysis/report.py`, `src/pacds/engine/mcp_http.py`; tests in `tests/unit/test_analysis.py` (or the
file that tests `report.py`), `tests/unit/test_mcp_http.py`.

- [ ] **Step 1 (report crash):** `report.py` `input_attribution` does `tool_call["function"]["name"]`; Claude Code
  session traces (`Trace.add_session`, phase `claude_code`) store assistant `tool_calls` flat
  (`{"id", "name", "arguments"}`). Write a failing test that runs `input_attribution` (or `write_report`) on a trace
  containing an `add_session` session; then accept both shapes: `name = call.get("name") or call["function"]["name"]`
  (same for arguments if read). Also check the rest of `report.py` for the same assumption.
- [ ] **Step 2 (MCP reset noise):** `mcp_http._Server._handle` lets a `ConnectionResetError`/`BrokenPipeError` from
  `writer.drain()` escape (logged by asyncio as "Unhandled exception in client_connected_cb"). Catch
  `(ConnectionError, OSError)` around the write/drain/close and drop it silently (the client went away). Test: a client
  that connects, sends a request and closes before reading produces no exception in the handler (use
  `loop.set_exception_handler` to capture and assert none).
- [ ] **Step 3:** unit suite green; commit `fix: report Claude Code traces and ignore dropped MCP clients`.

---

### Task 3: Move the toolkit into `src/pacds_eval`

**Files:** everything in the file map except the new files; `pyproject.toml`; `scripts/eval.sh`, `scripts/e2e.sh`,
`scripts/seed-logs.sh`; all `tests/unit/*`, `tests/contract/*`, `tests/e2e/*`, `tests/live/*`; `CLAUDE.md`, `README.md`,
`docs/deployment.md`, `docs/evaluation-runbook.md`.

**Produces:** package `pacds_eval` with the module names above; `cases/github`, `cases/debezium`; no repository default
case set.

- [ ] **Step 1: move with history.**
  ```bash
  mkdir -p src/pacds_eval/support_agent cases
  git mv tests/replay/harness.py tests/replay/baseline.py tests/replay/casebook.py tests/replay/catalog.py src/pacds_eval/
  git mv tests/replay/prompts src/pacds_eval/prompts
  git mv tests/analysis src/pacds_eval/analysis
  git mv src/pacds_eval/analysis/prompts/classify.md src/pacds_eval/prompts/classify.md
  git mv tests/support_agent/agent.py tests/support_agent/run.py tests/support_agent/__init__.py src/pacds_eval/support_agent/
  git mv tests/support_agent/skills src/pacds_eval/skills
  git mv tests/eval_run.py src/pacds_eval/runs.py
  git mv tests/oidc.py src/pacds_eval/oidc.py
  git mv tests/s3.py src/pacds_eval/s3.py
  git mv tests/replay/cases cases/github
  git mv tests/replay/candidates cases/debezium
  git mv tests/replay/CATALOG.md cases/CATALOG.md
  ```
  Add `src/pacds_eval/__init__.py` (docstring only: "PACDS evaluation toolkit: harness, case authoring, analysis.").
  Remove now-empty `tests/replay/`, `tests/support_agent/` (`__init__.py` leftovers) and any `__pycache__`.
- [ ] **Step 2: rewrite imports and `python -m` paths** in `src/`, `tests/`, `scripts/` with the rename table
  (e.g. `grep -rl "tests\.\(replay\|analysis\|support_agent\|eval_run\|oidc\|s3\)" src tests scripts | xargs sed -i ...`),
  then read every hit of `grep -rn "tests\." src scripts` and fix the leftovers by hand. `tests.e2e.test_smoke`
  imports stay (tests importing tests).
- [ ] **Step 3: file paths inside the modules.**
  - `support_agent/agent.py`: `SKILLS_DIR = Path(__file__).parents[1] / "skills"`.
  - `casebook.py`: `PLAYBOOK = Path(__file__).parent / "skills" / "tech-support" / "SKILL.md"`;
    `REVIEW_PROMPT = Path(__file__).parent / "prompts" / "review.md"`.
  - `analysis/classify.py`: `PROMPT_FILE = Path(__file__).parents[1] / "prompts" / "classify.md"`.
  - `harness.py`: remove `CASES_DIR`/`CANDIDATES_DIR` repository defaults and the `--candidates` flag everywhere
    (`harness`, `support_agent/run.py`, `analysis`, `catalog`, `s3 seed`); `resolve_cases_dir(cases_dir)` returns
    `--cases-dir`, else `$PACDS_CASES_DIR`, else exits with "no case set: pass --cases-dir or set PACDS_CASES_DIR".
  - `catalog.py`: the catalog is always `<cases dir>/CATALOG.md`; drop the combined repository catalog. Regenerate
    `cases/github/CATALOG.md` and `cases/debezium/CATALOG.md` with the new command and delete `cases/CATALOG.md`.
  - `s3.py seed`: seeds the logs of the case set given by `PACDS_CASES_DIR` (no repository fallback).
- [ ] **Step 4: packaging.** `pyproject.toml`: move `boto3` from the `dev` group to `dependencies`; configure uv_build
  to build both modules and include package data (`src/pacds_eval/prompts/*`, `skills/**`, `attack-vectors.json` in
  Task 6): with uv_build this is `[tool.uv.build-backend] module-name = ["pacds", "pacds_eval"]` (check the uv_build
  docs for the installed version with `uv run python -c "import uv_build"` / `uv build --help`; non-Python files under
  a module directory are included by default — verify with `uv build` and `unzip -l dist/*.whl`). `uv lock`.
- [ ] **Step 5: scripts.** `scripts/eval.sh`, `scripts/e2e.sh`: `python -m tests.X` → `python -m pacds_eval.X`
  (Task 5 rewrites `eval.sh` further). `eval.sh` gains a dev default: `export PACDS_CASES_DIR="${PACDS_CASES_DIR:-$ROOT_DIR/cases/github}"`.
  `scripts/seed-logs.sh`: loop over `cases/*/*/`.
- [ ] **Step 6: tests.** Fix unit/contract/e2e tests for the new modules and paths (fixtures that pointed at
  `tests/replay/cases` now point at `cases/github` or build a tmp case set). Add
  `tests/unit/test_packaging.py::test_service_never_imports_the_toolkit`: `grep`-style scan of `src/pacds/**/*.py`
  (excluding `cli.py`, added in Task 4) for `pacds_eval`, and a subprocess `python -c "import pacds.main, pacds.app, sys; assert not [m for m in sys.modules if m.startswith('pacds_eval')]"`.
- [ ] **Step 7: live docs.** `CLAUDE.md`, `README.md`, `docs/deployment.md`, `docs/evaluation-runbook.md`: replace
  `python -m tests.…` with `python -m pacds_eval.…` for now (Task 11 rewrites them for the image) and `--candidates`
  with `--cases-dir cases/debezium`.
- [ ] **Step 8: verify.** `uv run pytest tests/unit tests/contract -q; echo exit=$?` → 0; `uv build` → a wheel
  containing `pacds_eval/prompts/review.md`, `pacds_eval/prompts/classify.md`, `pacds_eval/skills/tech-support/SKILL.md`;
  `grep -rn "tests\.\(replay\|analysis\|support_agent\|eval_run\|oidc\|s3\)" src scripts tests CLAUDE.md README.md docs/deployment.md docs/evaluation-runbook.md` → nothing.
  Commit `refactor: move the evaluation toolkit into the pacds_eval package`.

---

### Task 4: The `pacds` dispatcher and `pacds eval` subcommands

**Files:** create `src/pacds/cli.py`, `src/pacds_eval/cli.py`; modify `pyproject.toml` `[project.scripts]`,
`src/pacds/main.py`, `src/pacds/devtools/check_llm.py`; tests `tests/unit/test_cli.py`.

**Produces:**
- Console script `pacds = "pacds.cli:main"` (replaces `pacds = "pacds.main:main"`); `pacds-fake-llm` unchanged.
- `pacds.cli.main(argv: list[str] | None = None) -> int`: no arguments or `-h/--help` → prints help listing `serve`,
  `check-llm`, `eval` with one line each, returns 0; `serve [args]` → `pacds.main.main()` (argv passed through);
  `check-llm [args]` → `pacds.devtools.check_llm.main()`; `eval ...` → imports `pacds_eval.cli` lazily and calls
  `pacds_eval.cli.main(rest)`; unknown command → help to stderr, exit 2.
- `pacds_eval.cli.main(argv) -> int` with subcommands dispatching to the existing module `main()` functions, argv
  passed through unchanged: `run` (Task 5), `audit` (Task 6), `casebook` → `pacds_eval.casebook`, `report|compare|select|sample|context|classify`
  → `pacds_eval.analysis.__main__` (prepend the subcommand), `catalog` → `pacds_eval.catalog`, `runs` →
  `pacds_eval.runs` (subcommands `list|fetch|archive|collect-traces|record|manifest|errors|finish|sync`), `replay` →
  `pacds_eval.harness`, `support` → `pacds_eval.support_agent.run`. `pacds eval` alone or `--help` → help with one line
  per subcommand. The existing module mains read `sys.argv`: call them with a patched `sys.argv` (`[f"pacds eval {cmd}", *args]`)
  so their `argparse` usage lines read correctly.

- [ ] **Step 1: failing tests** (`tests/unit/test_cli.py`):
  - `pacds.cli.main([])` returns 0 and prints `serve`, `check-llm`, `eval` (capsys).
  - `pacds.cli.main(["bogus"])` returns 2.
  - `pacds.cli.main(["eval"])` returns 0 and lists `run`, `audit`, `casebook`, `report`, `catalog`, `runs`.
  - `pacds.cli.main(["serve", "--help"])`... (if `pacds.main.main` has no argparse, test instead that `serve` calls
    `pacds.main.main` via monkeypatch).
  - `pacds.cli.main(["eval", "report", "--help"])` exits 0 with usage starting `usage: pacds eval report`.
  - subprocess: `uv run pacds` exit 0; `python -c "import pacds.cli, sys; assert 'pacds_eval' not in sys.modules"`.
- [ ] **Step 2:** implement with `argparse` (subparsers with `add_help=False` and `argparse.REMAINDER` for pass-through,
  or a small manual dispatch table — choose the simpler that keeps each subcommand's own `--help`).
- [ ] **Step 3:** update `test_service_never_imports_the_toolkit` to allow only `src/pacds/cli.py`, and only inside a
  function body.
- [ ] **Step 4:** unit suite green; `uv run pacds; echo exit=$?` → help, 0. Commit `feat: one pacds command for the service, preflight and evaluation toolkit`.

---

### Task 5: `pacds eval run` (port of `eval.sh --target`)

**Files:** create `src/pacds_eval/run.py`; modify `src/pacds_eval/cli.py`, `scripts/eval.sh`; tests
`tests/unit/test_eval_run_command.py`, an e2e test `tests/e2e/test_eval_run.py`.

**Produces:** `pacds eval run --target URL [--cases-dir DIR] [--pacds-config FILE] [--replay-from RUN,...] [--no-seed]
[--replay "ARGS"]... [--support "ARGS"]... [--run-dir DIR]` with exactly `eval.sh --target`'s behaviour
(`scripts/eval.sh` lines for `--target`, `with_out`, `stack_on_exit`, the sync loop):

1. Run dir: `--run-dir` else `$EVAL_RUN_DIR` else `./eval-runs/<UTC %Y%m%dT%H%M%SZ>`; create `traces/`.
   Print `=== run directory: DIR`.
2. `runs.record(run_dir, argv)` (the original argv).
3. `PACDS_URL = --target`; print `=== target: URL (<GET /healthz body or 'health check failed'>)`.
4. Unless `--no-seed`: `s3.seed` for the case set. Without `--pacds-config`: print the warning. `runs.manifest(run_dir, config)`.
5. With `PACDS_EVAL_ARCHIVE_S3_URI`: a background thread calling `runs.sync(run_dir)` every `EVAL_SYNC_SECONDS`
   (default 300), warnings on failure, stopped at the end.
6. For each `--replay` args string n=1..: `shlex.split`, append `--out RUN/replay-n.json` and
   `--trace-dir RUN/traces/replay-n` unless present, `--replay-from RUNS` when given; print `=== replay: ARGS`; run
   `pacds_eval.harness.main` in-process with that argv (or `subprocess` of `sys.executable -m pacds_eval.harness` —
   choose subprocess: it isolates `sys.argv`, `asyncio` loops and module state exactly as today). Same for each
   `--support` → `support-n`, `pacds_eval.support_agent.run`. A step with a non-zero exit fails the run (like
   `set -e`), after the finally-steps below.
7. Finally, always, pass or fail: stop sync; with `PACDS_TRACE_SOURCE_DIR`: `runs.collect_traces` (warning on failure);
   `runs.errors`; `runs.finish`; `analysis report` (print `=== report: RUN/report/report.md`, or the warning); with
   `PACDS_EVAL_ARCHIVE_S3_URI`: `runs.archive` (warning on failure).
8. `--replay-from` for a target: print the NOTE that PACDS replays only if the target was started with those
   recordings (as `eval.sh` does).
9. `--cases-dir` sets `PACDS_CASES_DIR` for the steps.
10. Exit status: the first failing step's, else 0.

`scripts/eval.sh` keeps its option parsing, `.env` loading, LLM checks, the Claude Code guard, `--reuse/--keep`,
the stack (`stack_start`, show_config into `pacds-config.json`, the replay-source copy into `PACDS_REPLAY_HOST_DIR`,
compose logs on exit) and `--audit` (Task 6), and delegates everything else to
`uv run pacds eval run --target http://localhost:3002 --run-dir "$RUN_DIR" --pacds-config "$RUN_DIR/pacds-config.json" --no-seed ...`
(the stack is seeded by `seed-logs.sh`), passing `--replay`/`--support`/`--replay-from`. `--target` on `eval.sh` is
removed (operators use `pacds eval run` directly).

- [ ] **Step 1: unit tests** with monkeypatched step runner and `runs`/`s3` functions: argument composition
  (`--out`/`--trace-dir` appended only when absent; `--replay-from` appended), step order, finally-steps run after a
  failing step, exit status propagation, run-dir default, sync thread started only with the archive variable.
- [ ] **Step 2:** implement `run.py`; wire `pacds eval run`.
- [ ] **Step 3:** rewrite `scripts/eval.sh` as above; `bash -n` passes.
- [ ] **Step 4: e2e parity test** (`tests/e2e/test_eval_run.py`, marker `e2e`, runs under `scripts/e2e.sh` with the
  fake LLM): `pacds eval run --target http://localhost:3002 --cases-dir cases/github --no-seed --replay "--set clear --case <one clear case id>"`
  into a tmp run dir; assert `run.json`, `replay-1.json`, `errors.json` exist, `report/report.md` exists, exit 0.
- [ ] **Step 5:** `./scripts/e2e.sh; echo exit=$?` → 0 (includes the new e2e test). Unit suite green. Commit
  `feat: pacds eval run replaces eval.sh --target`.

---

### Task 6: `pacds eval audit`

**Files:** `git mv tests/security/test_exfiltration.py src/pacds_eval/audit.py`,
`git mv tests/security/attack-vectors.json src/pacds_eval/attack-vectors.json`; remove `tests/security/`; modify
`src/pacds_eval/cli.py`, `scripts/eval.sh`; tests `tests/unit/test_audit.py`.

**Produces:** `pacds eval audit --target URL [--repo URL] [--ref REF] [--vectors FILE] [--run-dir DIR]`:
- Default repo `https://github.com/rophy/tostada.git`, ref `main` (today's `GIT` in `tests/e2e/test_smoke.py`); the
  `CAUSES` criteria move into `audit.py`.
- For each vector (package `attack-vectors.json` plus `--vectors FILE` entries, same schema) send today's request body
  (token from `oidc.TokenSource`), apply today's checks exactly (status 200; keys; answer fields; strings outside the
  allowed set; leak patterns on produced text). Collect per-vector `{id, passed, reason}`.
- Print one line per vector (`ok`/`LEAK`/`FAIL` + reason) and a summary; write `<run dir>/audit.json`; exit 1 when any
  vector fails. Run dir default as `eval run`.
- `scripts/eval.sh --audit` → `uv run pacds eval audit --target http://localhost:3002 --run-dir "$RUN_DIR"`.

- [ ] **Step 1: failing tests** with a local stub HTTP server (FastAPI `TestClient` is not enough: the audit calls a
  URL — use `httpx.MockTransport` injected into the audit's client, or a threaded `uvicorn`/`http.server`): a stub
  answering typed values passes all vectors; a stub adding `"note": "func GetUser ..."` fails with a leak; a stub
  returning 500 fails; `--vectors` extra entries are run; `audit.json` written.
- [ ] **Step 2:** implement; wire the subcommand; update `eval.sh`.
- [ ] **Step 3:** unit suite green; commit `feat: pacds eval audit runs the exfiltration audit against any PACDS`.

---

### Task 7: Versions in `/healthz` and `run.json`

**Files:** `src/pacds/app.py`, `src/pacds_eval/runs.py`, `src/pacds_eval/run.py`, `src/pacds_eval/analysis/report.py`;
tests in `tests/unit/test_app.py`, `tests/unit/test_eval_run.py`, report tests.

**Produces:** `/healthz` → `{"status": "ok", "version": importlib.metadata.version("pacds")}`; `run.json` gains
`"toolkit_version"` (same source) and `"target_version"` (from `/healthz`, `null` when unreachable) — `runs.record`
keeps the git commit when run from a checkout (`git rev-parse` succeeding), else omits it; `report.md` shows both
versions; `pacds eval run` prints `WARNING: toolkit X.y.z and target A.b.c differ in major version` when they do.

- [ ] Steps: failing tests (healthz body; record with/without git; manifest target version from a mocked `/healthz`;
  report header lines; warning), implement, suite green, commit `feat: report the PACDS and toolkit versions of every run`.

---

### Task 8: The standalone support-agent sample

**Files:** create `samples/support-agent/{README.md,agent.py,requirements.txt,example-ticket.json}`, symlink
`samples/support-agent/skills -> ../../src/pacds_eval/skills`; tests `tests/unit/test_sample_agent.py`.

**Produces:** `python agent.py ticket.json` — no import of `pacds` or `pacds_eval`; dependencies `openai`, `httpx` only.
- Ticket: `{"report": str, "repo": str (https git URL), "ref": str (tag or full commit), "logs": [{"name": str, "url": str}]}`
  (`logs` optional; URLs the team's log store already serves, e.g. presigned).
- Environment: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`; `PACDS_URL`; `PACDS_TOKEN`, or `PACDS_OIDC_TOKEN_URL`,
  `PACDS_OIDC_CLIENT_ID`, `PACDS_OIDC_CLIENT_SECRET` (client-credentials grant).
- System prompt: "You handle support tickets for {application}. Use your skills below. Submit a decision for every
  ticket." + both `SKILL.md` files (resolved relative to `agent.py`), as the evaluation agent builds it.
- Tools (chat-completions function definitions): `call_pacds(document: object, questions: object, logs: [str])` →
  POST `{PACDS_URL}/v1/systemone` with `{"model": "pacds", "state": {**document, "pacds": {"git": {"url", "ref"}, "logs": [selected ticket logs]}}, "questions": questions}`
  and `Authorization: Bearer <token>`; returns the JSON body (answers or error) to the model. `submit_decision(class: A|B|C|D, escalate: bool, confidence: number, reply: string)`
  ends the loop.
- Loop: at most 10 turns, at most 3 PACDS calls; prints the decision as JSON on stdout; exit 1 when no decision.
- README: what it is (a starting point, not a supported component), setup (`pip install -r requirements.txt`),
  environment, running on `example-ticket.json`, how to adapt the skills, the Jev request shape with a sample body
  (from the real request shape in `pacds_eval/support_agent/agent.py`), and that the class definitions live in
  `skills/tech-support/SKILL.md`.

- [ ] **Step 1: failing test:** run `agent.main([...])` (import the sample module by path with `importlib.util`)
  against a fake OpenAI server (a stub `openai` client injected, or `httpx.MockTransport` behind `openai.OpenAI(http_client=...)`)
  that first calls `call_pacds`, then `submit_decision`; and a mocked PACDS endpoint; assert the PACDS request body
  (`state.pacds.git` from the ticket, bearer header) and the printed decision. A second test: the sample imports
  nothing from `pacds`/`pacds_eval` (scan the source).
- [ ] **Step 2:** implement (about 150 lines, plain and readable: it is meant to be read and copied).
- [ ] **Step 3:** suite green; commit `feat: a standalone support-agent sample`.

---

### Task 9: One image

**Files:** `Dockerfile`, `compose.yaml`, `deploy/compose.yaml`, `deploy/compose.eval.yaml`,
`deploy/compose.eval-replay.yaml` (if it defines a command), `deploy/.env.example`; create `scripts/image-smoke.sh`.

**Produces:**
- `Dockerfile`: `COPY samples ./samples` is replaced by copying the dereferenced sample into
  `/opt/pacds/samples/support-agent` (`COPY` does not follow a symlinked directory: copy `src/pacds_eval/skills` into
  `/opt/pacds/samples/support-agent/skills` in a separate `COPY` after copying the rest of `samples/support-agent`
  without its `skills` link); `ARG PACDS_VERSION` → `ENV PACDS_VERSION`; `ENTRYPOINT ["pacds"]`, no `CMD`; the
  `CLAUDE_CODE_VERSION` block unchanged.
- Every compose service running PACDS sets `command: ["serve"]` (the dev `fake-llm` service keeps
  `["pacds-fake-llm"]` — with the new `ENTRYPOINT` it must set `entrypoint: ["pacds-fake-llm"]` instead of `command`).
- `deploy/.env.example`: `PACDS_IMAGE=ghcr.io/rophy/pacds:2.1.0` (the bundle script in Task 10 stamps the version).
- `scripts/image-smoke.sh IMAGE`: `docker run --rm IMAGE` exits 0 and prints `serve`; `docker run --rm IMAGE eval --help`
  exits 0; `docker run --rm IMAGE check-llm --help` exits 0; `docker run --rm --entrypoint ls IMAGE /opt/pacds/samples/support-agent/skills/tech-support/SKILL.md`;
  `serve` in a container with a minimal config (reuse `dev/pacds.yaml` and the fake-LLM env) answers `/healthz` with
  `"version"`. Exit non-zero on any failure.

- [ ] Steps: implement; `docker build -t pacds-smoke .`; `scripts/image-smoke.sh pacds-smoke; echo exit=$?` → 0;
  `./scripts/e2e.sh` → 0 (the dev stack now uses `command: ["serve"]`); remove `pacds-smoke`; commit
  `build: one image with the pacds command as entrypoint`.

---

### Task 10: CI, bundles and version 2.1.0

**Files:** create `.github/workflows/ci.yaml`, `scripts/build-bundles.sh`; modify `pyproject.toml` (`version = "2.1.0"`),
`uv.lock`.

**Produces:**
- `scripts/build-bundles.sh VERSION OUT_DIR`: writes `pacds-deploy-VERSION.tar.gz` (top dir `pacds-deploy-VERSION/`:
  `compose.yaml`, `compose.eval.yaml`, `compose.eval-replay.yaml`, `pacds.example.yaml`, `.env.example` with
  `PACDS_IMAGE=ghcr.io/rophy/pacds:VERSION`, `certs/README.md`, `docs/deployment.md`, `docs/evaluation-runbook.md`)
  and `pacds-samples-VERSION.tar.gz` (top dir `pacds-samples-VERSION/support-agent/` with skills dereferenced, `tar -h`).
- `.github/workflows/ci.yaml` (use the github-actions-cicd skill's rules; look up each action's latest release and
  its SHA with `gh api`; read-only `gh` calls are allowed):
  - `on: push` (all branches), `pull_request`.
  - job `test`: checkout; setup uv (astral-sh/setup-uv); `uv sync`; `uv run pytest tests/unit tests/contract -q`;
    docker buildx build (amd64, `push: false`, `load: true`) tagged `pacds:ci`; `scripts/image-smoke.sh pacds:ci`;
    `scripts/build-bundles.sh 0.0.0-ci dist` (validates the bundles).
  - job `e2e` (needs test, `if: github.ref == 'refs/heads/master'`): `./scripts/e2e.sh`.
  - job `release` (needs e2e, master only; `permissions: contents: write, packages: write`): read the version from
    `pyproject.toml`; step `check` sets `exists=true` when tag `v<ver>` exists (`git ls-remote --tags origin v<ver>`),
    printing "v<ver> already released — skipping"; when not: setup-qemu, setup-buildx, login to ghcr.io with
    `GITHUB_TOKEN`, build and push `linux/amd64,linux/arm64` with tags `<ver>`, `<major>.<minor>`, `latest` and build
    arg `PACDS_VERSION=<ver>`; `scripts/build-bundles.sh <ver> dist`; create tag `v<ver>` and push it; notes from
    `gh api repos/{repo}/releases/generate-notes` plus a section with `docker pull ghcr.io/rophy/pacds:<ver>`;
    `gh release create v<ver> -F notes.md dist/*.tar.gz`.
  - A `workflow_dispatch` input `dry_run` (default true when dispatched) that runs the release job's build (both
    architectures) and bundles without login, push, tag or release.
- `version = "2.1.0"`; `uv lock`.

- [ ] Steps: write the script and test it locally (`scripts/build-bundles.sh 2.1.0 /tmp/bundles && tar -tzf ...`:
  check the listed files, the stamped image, the dereferenced skills); write the workflow; validate syntax with
  `actionlint` if available (`docker run --rm -v "$PWD":/repo -w /repo rhysd/actionlint:latest`), else a YAML parse;
  suite green; commit `build: CI, release bundles and version 2.1.0`. Do not push.

---

### Task 11: Docs for "pull the image, unpack the bundle"

**Files:** `docs/deployment.md`, `docs/evaluation-runbook.md`, `README.md`, `CLAUDE.md`, `samples/support-agent/README.md`
(consistency only).

- `deployment.md`: get the release (`docker pull` / mirror into the internal registry with `docker tag` + `push`,
  download and unpack `pacds-deploy-<ver>.tar.gz`); building the image becomes an appendix for sites that build
  themselves; every command uses the image (`docker compose ... run --rm pacds check-llm`, `serve`); no
  `python -m`, no checkout.
- `evaluation-runbook.md`: prerequisites without Python/uv/checkout; the harness is `docker run --rm --env-file eval.env
  -v <cases>:/cases -v <runs>:/runs [-v <traces>:/traces:ro] ghcr.io/rophy/pacds:<ver> eval ...` (define a shell alias
  `pacds-eval` for brevity in the doc); every section's commands converted (`casebook`, `run`, `audit` — new section
  "Exfiltration audit" with `--repo` for the site's own repository — `report`, `compare`, `classify`, `runs`); the
  Claude Code section stays but notes it is for development machines with a checkout.
- `README.md`: a "What is published" section (image, bundle, samples; how a release is made: bump version, merge to
  master); development commands use `uv run pacds ...`.
- `CLAUDE.md`: command names (`pacds eval report|compare|select|sample|context`, `pacds eval runs list|fetch`,
  `pacds eval casebook`), case sets under `cases/`.
- Check: `grep -rn "python -m tests\.\|tests/replay\|tests/analysis\|--candidates" README.md CLAUDE.md docs/deployment.md docs/evaluation-runbook.md samples`
  → nothing. Commit `docs: deploy and evaluate from the published image`.

---

### Task 12: Verification

No new code unless something fails.

- [ ] `uv run pytest tests/unit tests/contract -q` → green; `./scripts/e2e.sh` → 0.
- [ ] Replay parity: `PACDS_OIDC_URL=http://localhost:3013 ./scripts/eval.sh --replay "--set clear" --replay "--baseline --set clear" --replay-from eval-runs/<PARITY_RUN>`
  with `LLM_MODEL=haiku` in `.env` → every PACDS session and baseline answer is replayed (all `sessions[].replayed`
  true in `traces/pacds/*.json` and the client traces), identical results to `PARITY_RUN`. Restore `LLM_MODEL`.
- [ ] `docker build -t pacds-verify . && scripts/image-smoke.sh pacds-verify` → 0.
- [ ] `pacds eval audit` against the dev stack with a real model is optional (costs usage): run
  `./scripts/eval.sh --audit` with haiku once and record the result in the report.
- [ ] Report: what ran, results, anything skipped.
