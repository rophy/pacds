# Packaging and release — Design

**Date:** 2026-09-30
**Status:** Approved design, not implemented
**Context:** a corporate deployment should need no git checkout of this repository: the service, the evaluation
toolkit and the support-agent sample must arrive as published artifacts. Today only the service has a Dockerfile, the
toolkit lives under `tests/` and runs through `uv run` and `scripts/eval.sh` from a checkout, and nothing is published
(no CI).

## 1. Deliverables

| Deliverable | Form | Where |
|---|---|---|
| PACDS service, preflight and evaluation toolkit | one container image | `ghcr.io/rophy/pacds:<ver>` (also `<major>.<minor>`, `latest`), linux/amd64 + linux/arm64 |
| Deploy bundle | tarball | GitHub Release asset `pacds-deploy-<ver>.tar.gz` |
| Support-agent sample | source tarball (also inside the image) | GitHub Release asset `pacds-samples-<ver>.tar.gz`, `/opt/pacds/samples/` in the image |

Decisions:

- **One image.** With one version for everything (section 5), a second image buys only a smaller production image.
  The toolkit adds about 32 MB (botocore 30 MB) and no network listener. If a corporate security review demands a
  minimal production image, a second target is a later, contained change.
- **No PyPI.** PyPI has no scoped names and GitHub Packages has no Python registry; a container image is the org-scoped,
  mirrorable form, and needs no Python or uv on site.
- **The support agent is sample code**, adopted and adapted by the support team, not a deployed component.

## 2. Repository layout

```
src/pacds/                  the service; never imports pacds_eval (enforced by a unit test)
src/pacds_eval/             the evaluation toolkit (moved from tests/)
  cli.py                    `pacds eval ...` subcommands
  run.py                    one run (ported from eval.sh --target)
  audit.py                  the exfiltration audit (from tests/security) + attack-vectors.json
  harness.py baseline.py    ← tests/replay/
  casebook.py catalog.py    ← tests/replay/
  support_agent/            ← tests/support_agent/ (the instrumented agent the harness drives)
  analysis/                 ← tests/analysis/ (report, compare, select, sample, classify, context)
  runs.py                   ← tests/eval_run.py
  oidc.py s3.py             ← tests/
  prompts/                  review and classify prompts (package data)
samples/support-agent/      standalone sample (section 6); its skills/ is the single source of the skills
cases/                      the public development case sets (← tests/replay/{cases,candidates}); in no image
tests/                      tests only: unit/, contract/, e2e/, live/ (the audit moves to pacds_eval)
scripts/eval.sh             dev wrapper: start the Compose stack, `pacds eval run --target http://localhost:3002 ...`, tear down
deploy/                     packed into the deploy bundle
```

- One `pyproject.toml`, one distribution containing both packages; boto3 moves to the main dependencies.
- The package has no default case set: every command takes `--cases-dir` / `PACDS_CASES_DIR`; `eval.sh` sets it to
  `cases/...` in development.
- The skills are copied into `pacds_eval` as package data at build time from `samples/support-agent/skills/`.
- Behaviour, file formats (`case.json`, run directories, traces) and prompt texts do not change, so request hashes do
  not change and recorded runs replay unchanged.

## 3. Command line

The image's `ENTRYPOINT` is `pacds`, with no default command. `pacds` with no arguments prints help and exits 0.

```
pacds serve                          the service (every compose file sets command: ["serve"])
pacds check-llm [CONFIG] [...]       today's python -m pacds.devtools.check_llm
pacds eval run --target URL [--cases-dir DIR] [--pacds-config FILE] [--replay-from RUN,...] [--no-seed]
               [--replay "ARGS"]... [--support "ARGS"]... [--run-dir DIR]
pacds eval audit --target URL [--repo URL --ref REF] [--vectors FILE] [--run-dir DIR]
pacds eval casebook import|review-llm|packet|review|label|adjudicate|screen|status ...
pacds eval report|compare|select|sample|context|classify RUN ...
pacds eval catalog --cases-dir DIR [--cutoff model=DATE]
pacds eval runs list|fetch NAME|archive RUN|collect-traces RUN DIR
pacds eval replay|support ARGS       one step without the run wrapper
```

- **`eval run`** is `eval.sh --target` in Python, same steps in the same order: record `run.json`; seed logs (unless
  `--no-seed`); each `--replay` / `--support` step with its `--out` and `--trace-dir`; archive sync every
  `EVAL_SYNC_SECONDS`; collect PACDS traces from `PACDS_TRACE_SOURCE_DIR`; `errors.json`; report; archive. Non-zero
  exit when a step fails.
- **`eval audit`** sends the attack vectors (8 today) to the target and passes only when every answer is typed values
  and nothing else: HTTP 200, only the expected keys and fields, no string outside the allowed names and the caller's
  labels, no leak pattern. The repository is configurable (default the public `github.com/rophy/tostada` at `main`)
  so a corporate audit runs against its own code; `--vectors FILE` adds its own attacks. Writes a pass/fail report
  to the run directory; non-zero exit on any leak. The dev `eval.sh --audit` calls it.
- Subcommands keep today's arguments (`python -m tests.analysis report X` → `pacds eval report X`); configuration stays
  in the environment (`LLM_*`, `PACDS_URL`, `PACDS_OIDC_*` / `PACDS_TOKEN`, `PACDS_LOGS_S3_*`, `PACDS_EVAL_ARCHIVE_*`,
  `PACDS_CASES_DIR`).
- Image defaults: `PACDS_CASES_DIR=/cases`, runs under `/runs`, `PACDS_TRACE_SOURCE_DIR=/traces` when mounted; runs as a
  non-root uid.

```
docker run --rm --env-file eval.env -v /data/cases/crm:/cases -v /data/eval-runs:/runs \
  [-v /srv/pacds-eval/traces:/traces:ro] ghcr.io/rophy/pacds:2.1.0 eval run --target http://pacds-vm:8081
```

- Stay in `scripts/eval.sh` (need Docker on the host): the stack lifecycle (`--reuse`, `--keep`, build, teardown).

## 4. Image

- One Dockerfile target (`runtime`), `ENTRYPOINT ["pacds"]`; samples at `/opt/pacds/samples/support-agent`;
  `PACDS_VERSION` in the environment. `CLAUDE_CODE_VERSION` stays a dev-only build argument.
- `deploy/compose.yaml`, `deploy/compose.eval.yaml` and the dev `compose.yaml` set `command: ["serve"]`; the deploy
  bundle's compose files default `PACDS_IMAGE` to `ghcr.io/rophy/pacds:<ver>`.
- `/healthz` returns `{"status": "ok", "version": "<ver>"}` from the package metadata.

## 5. Versioning and release

- **One version** for everything: `version` in `pyproject.toml`. Each release publishes the image and both tarballs.
  The toolkit imports the service's engine code (provider, trace and replay formats, prompts), so "same version" is
  the compatibility rule.
- **Meaning:** major — a breaking change for someone deploying or calling PACDS (the Jev request/response contract,
  `state.pacds`, error codes; removed or renamed config keys; case-set or run-directory formats the toolkit can no
  longer read); minor — backwards-compatible features and config options, including prompt changes; patch — fixes.
- **Visibility of mixes:** `eval run` records its own version and the target's (`/healthz`) in `run.json`; the report
  shows both; a major mismatch prints a warning.
- **First release: `2.1.0`**, with the Claude Code backend merged. Changing the default command to help needs no
  compatibility handling (nothing is published yet).
- **CI** (`.github/workflows/ci.yaml`, actions pinned to commit SHAs with the exact tag in a comment):
  1. test — every push and PR: `uv sync`, unit and contract tests, build the image (amd64, no push);
  2. e2e — push to `master`: `scripts/e2e.sh` (fake LLM, no cost);
  3. release — push to `master`, after e2e: read the version; when tag `v<ver>` does not exist (skip, not fail,
     otherwise) build multi-arch and push the image, create tag `v<ver>`, create the GitHub Release with generated
     notes plus the pull command, attach both tarballs. Permissions `contents: write`, `packages: write`.
- A release is a version bump merged to `master`. The first publish needs the GHCR package made public (or a pull
  token for the corporate mirror): a one-time setting.

## 6. Support-agent sample

```
samples/support-agent/
  README.md                configure, run on a ticket, adapt the skills
  skills/tech-support/SKILL.md, skills/pacds/SKILL.md
  agent.py                 about 150 lines, no dependency on PACDS code
  requirements.txt         openai, httpx
  example-ticket.json
```

- `python agent.py ticket.json`: reads a ticket (report, log files or URLs, repository URL, deployed ref), runs an
  OpenAI-compatible tool loop with the two skills as its system prompt; tools `call_pacds` (builds the Jev body with
  `state.pacds`, uploads or references logs, calls `POST /v1/systemone` with a bearer token) and `submit_decision`
  (class, escalate, confidence, a short reply draft).
- Configuration from the environment: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `PACDS_URL`, `PACDS_TOKEN` or
  `PACDS_OIDC_*`.
- No traces, replay, case formats or Claude Code: those stay in `pacds_eval/support_agent/`, which reads the same
  skill files. The two agents share skills, not code.

## 7. Migration order

Each step lands separately with the suite green:

1. Merge the Claude Code backend branch (needs the owner's go-ahead).
2. Move the toolkit to `src/pacds_eval/` (`git mv`), rewrite imports, `python -m` paths, scripts and docs; case data
   to `cases/`.
3. Skills to `samples/support-agent/skills/`, prompts to package data.
4. The `pacds` dispatcher (help, `serve`, `check-llm`, `eval ...`); port `eval.sh --target` to `pacds eval run`;
   `eval audit` from `tests/security`; `eval.sh` becomes the dev wrapper.
5. `/healthz` version; versions in `run.json`.
6. The standalone sample.
7. The image (`ENTRYPOINT`, samples, compose `command`).
8. CI and the tarball build script.
9. Docs: `deployment.md` and `evaluation-runbook.md` rewritten for "pull the image, unpack the bundle"; README
   "what is published".

## 8. Testing

- Unit and contract tests green after every step. New: the service never imports `pacds_eval`; `pacds` without
  arguments prints help and exits 0; the sample runs against a fake LLM and a stubbed PACDS; `eval audit` against a
  stub that leaks and one that does not.
- `pacds eval run --target` against the Compose dev stack with the fake LLM produces the run directory `eval.sh`
  produces today (`run.json`, results, traces, `errors.json`, report).
- Replay parity: an existing recorded run (e.g. `eval-runs/20260929T224109Z`) through `--replay-from` is served
  entirely from the recording.
- Image smoke test in CI: no arguments prints help; `serve` answers `/healthz` with the version; `check-llm --help` and
  `eval --help` run; the samples path exists.
- Release dry run on a branch with pushing disabled (tarballs, multi-arch build) before the first publish.

## 9. Out of scope

PyPI; image signing; CVE scanning in CI; the accuracy work of 2026-09-30 (changelog/docs investigation step, routing
score for A/B).
