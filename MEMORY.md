# MEMORY — evaluation work status (2026-09-28)

Handoff note for the next session. Read this first, then `docs/superpowers/specs/2026-09-28-eval-analysis-framework-design.md`.

## Where things are

- **Branch:** `claude/git-config-cloud-setup-hqootl` (pushed; not merged to `master`). `master` is at `1c3cc80`;
  everything below is on the branch only.
- **Issue:** rophy/pacds#1 — collect more hard replay cases (Debezium Google Group).
- **Lost with the old container:** `eval-runs/` (git-ignored) and scratchpad files. The numbers below are the
  only record of those runs.

## What exists now (all committed)

| Area | Where | Notes |
|---|---|---|
| No-cost e2e vs real-LLM eval split | `scripts/e2e.sh` (always fake LLM), `scripts/eval.sh` (real LLM), `scripts/stack.sh` | `eval.sh --audit / --replay "<args>" / --support "<args>"`, `--reuse`, `--keep` |
| Run records | `eval-runs/<UTC>/`: `run.json`, `eval.log`, results, `compose.log` (saved before teardown), `errors.json` | PACDS log lines carry `request=<id>` |
| Debezium candidates | `tests/replay/candidates/debezium-*/` (121 fixtures) + `rejected.json` (7) | run with `--candidates` |
| Case catalog | `tests/replay/CATALOG.md` via `python -m tests.replay.catalog [--check] [--after DATE]` | source, date, set, class, review tier, baseline p, per-model cutoff marks |
| Labels | 51 hard candidates reviewed (two blind reviews each, `reviews` in case.json; tier `certain` 18 / `probable` 33) | classes A 1, B 31, C 13, D 6; other 70 candidates `unreviewed` (baseline gets them right) |
| Playbook | `tests/support_agent/skills/tech-support/SKILL.md` | classify by **where the fix is made**; boundary rules; regression step; "whose fix is it" step |
| Support agent | attaches logs **by name** (tool wrapper signs URLs at call time) | the model mangled presigned URLs before |
| PACDS history | `git.history_depth` (default 500) + tools `git_log`, `git_show` | only commits up to the deployed version |
| Provider robustness | `llm.max_output_tokens` (`LLM_MAX_OUTPUT_TOKENS`), retry one call on 502/504 | needed for reasoning models / flaky gateways |
| Base image | `mirror.gcr.io/library/python:3.12-slim` | avoids Docker Hub rate limits |

## Results so far (gpt-6-luna on OpenCode Go)

Baseline = same model, same question, report + logs only, no code.

**Old GitHub replay set (23 cases):** clear set 12/12 for every configuration (no signal; keep only as a no-harm
check). Hard set (11): PACDS fixed question 7/11, baseline 5/11, support agent +PACDS 9/11, −PACDS 7/11.

**Debezium, 51 reviewed hard cases, support agent, 3 runs each (run 2026-09-28T08:44, before history/playbook
steps 2):**

| Class | runs | +PACDS | −PACDS |
|---|---|---|---|
| A | 3 | 33% | 33% |
| B | 93 | 65% | 51% |
| C | 39 | 23% | 15% |
| D | 18 | 11% | 11% |
| all | 153 | **47%** | **37%** |

Per case (≥2/3 right): 23 vs 19 of 51; PACDS gained 7, lost 3. Baseline on the same labels ~20%.

**Miss analysis of that run (hand-classified from transcripts):**
- D misses (18): `pacds_wrong` 8 — PACDS confidently denies regressions (deliberate-looking code; it had no
  history then) — `wrong_questions` 5, `agent_overrode` 3, `defensible` 2.
- C misses (30): `wrong_questions` 13 — agent never asks *who controls the failing setting* (DB grants,
  publications, topic config, platform classpath) — `agent_overrode` 8, `no_pacds` 5, `pacds_wrong` 2,
  `defensible` 2.
- Dataset caveat: Debezium group reporters operate everything themselves, so B vs C is inherently blurry.

**Run after adding PACDS history + playbook steps (2026-09-28T11:40): inconclusive.** OpenCode Go's
**5-hour usage limit** was hit; only 88/153 +PACDS tickets finished (43%; D 3/8, C 3/24). PACDS used the new
tools heavily (587 `git_log`, 224 `git_show`).

**Token cost:** a full run (51×3, both variants) ≈ 30M input tokens; PACDS is 27M of it (208 investigations,
avg 130K input, median 6 turns). Estimated **~79% of PACDS input is old tool results resent each turn**
(`search_code` and `read_file` dominate).

## Providers

- **OpenCode Go** (`gpt-6-luna`, cutoff 2026-05-18, Responses API, header `x-opencode-session`): works; 5-hour
  usage window caps full runs.
- **Plugsky** (`https://plugsky.com/v1`, chat completions, proxy-injected credential): account is on a
  **trial plan capped at `plugsky-plus`**. `plugsky-plus`/`-pro` = Nemotron 3 Nano Omni 30B-A3B (reasoning);
  `plugsky-max` = Nemotron 3 Ultra 550B. Silent fallback models; responses only report the tier name.
  Needs `LLM_MAX_OUTPUT_TOKENS=16000` (else "final answer did not complete: length"); ~12% of JSON-format calls
  return 502 (now retried). First pilot (before fixes, concurrency 4): baseline 87/153 failed, 9/66 right.
  Sequential pilot was stopped before producing results. Run with env overrides, e.g.
  `env LLM_BASE_URL=https://plugsky.com/v1 LLM_MODEL=plugsky-plus LLM_API= LLM_SESSION_HEADER= LLM_API_KEY=x LLM_MAX_OUTPUT_TOKENS=16000 ./scripts/eval.sh ...`

## Decided direction (next work)

Stop full reruns after each tweak. Build the **evaluation analysis framework** first
(`docs/superpowers/specs/2026-09-28-eval-analysis-framework-design.md`):

1. **Capture** — PACDS per-request traces (dev-gated `trace.dir`), client traces, richer manifest.
2. **Analysis** — `python -m tests.analysis report|compare`, `--from-run/--select`, regression sample.
3. **Replay** — recorded model calls; re-run only what a change affects.
4. **Classification** — checked-in failure-mode classifier.
Then token reductions (tool result size, trimming resent history, prompt caching), measured via traces.

Open decisions (see spec §7): classifier model; regression sample fixed now vs per milestone.
**Run archive storage: user chose S3.** Expected env vars (set by the user in the environment settings; verify
at session start — they were not visible in the old session):
`PACDS_EVAL_ARCHIVE_S3_URI` (e.g. `s3://<bucket>/pacds/eval-runs/`), `PACDS_EVAL_ARCHIVE_REGION`,
`PACDS_EVAL_ARCHIVE_ACCESS_KEY_ID`, `PACDS_EVAL_ARCHIVE_SECRET_ACCESS_KEY` (names may differ — check `env`).
Do **not** use `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`: the cloud environment sets placeholder values
(`prox…`) for those. Do not reuse `PACDS_S3_ACCESS_KEY`/`PACDS_S3_SECRET_KEY` (dev MinIO). The bucket host must
be allowed under Network access. First task: verify S3 access, then phase 1.

## Environment gotchas

- The container restarts occasionally; `dockerd` does not come back. Start it:
  `rm -f /var/run/docker.pid; nohup dockerd >/tmp/dockerd.log 2>&1 &` then wait for `docker info`.
- Never `pkill -f <pattern>` where the pattern appears in your own command line (it kills the shell).
- Check test exit codes directly, not through a pipe (`cmd > log; echo $?`).
- Commit policy: see `CLAUDE.md` (no Claude/co-author lines).
