# Evaluation analysis framework — Design

**Date:** 2026-09-28
**Status:** Phases 1–4 done (2026-09-28); first traced milestone in `docs/evaluation/2026-09-28-findings.md` §5
**Context:** rophy/pacds#1 (hard replay cases), `docs/evaluation/2026-09-27-findings.md`

## 1. Problem

The evaluation loop so far has been: change a prompt or a rule, rerun the whole suite, compare one accuracy
number. Each full run costs about 30M input tokens and an hour, and it keeps too little to explain its own
result, so every round ends in a new number and a new guess.

What a run keeps today:

| Component | Kept | Missing |
|---|---|---|
| Support agent | transcript, PACDS questions/answers, request ids | per-call tokens, latency, finish reason, served model |
| PACDS | audit record (questions, answers, status); one log line per tool call (tool, args cut to 300 chars, result length) | tool results, the model's text between steps, raw final output, per-call usage and latency, retries |
| Baseline / fixed-question replay | predicted class and probabilities | the model's raw output |
| Run | `run.json` (commit, model, args), `compose.log`, `errors.json` | config and prompt snapshot; anything that survives the container |

Deep analysis (why the agent missed infrastructure cases, why PACDS denied regressions) had to be
reconstructed by hand from partial data. PACDS's own reasoning could not be inspected at all.

## 2. Goals

1. **One run, many analyses.** A run records everything needed to explain every answer, so analysis happens
   offline and can be repeated without new LLM calls.
2. **Evidence-driven changes.** Every change is motivated by a finding in a recorded run and validated
   against the cases it targets.
3. **Pay only for what changed.** Changes downstream of an LLM call are validated by replaying recorded
   calls; live calls happen only for work whose inputs changed.
4. **Comparable runs.** Runs are compared case by case, with the uncertainty that 51 cases × 3 repeats allow.

Non-goals: a general experiment-tracking platform; changing the PACDS API; online dashboards.

## 3. Design

### 3.1 Traces (capture)

**PACDS investigation trace** — one file per request, written by the server when `trace.dir` is configured
(off by default):

```
traces/pacds/<request_id>.json
{
  "request_id", "started", "subject", "git": {"url", "ref", "sha", "history_depth"},
  "document", "logs": [{"name", "bytes"}], "questions",
  "config": {"model", "api", "max_turns", "time_budget_seconds", "max_output_tokens"},
  "prompt_sha256": {"system": "...", "final": "..."},
  "calls": [                                  # every model call, in order
    {"n": 1, "phase": "investigate" | "final" | "correction",
     "messages_added": [...],                 # delta since the previous call (not the whole transcript)
     "response": {"content", "tool_calls", "finish_reason", "model"},
     "usage": {"input", "output", "cached"}, "latency_ms", "attempts": [{"status", "latency_ms"}]}
  ],
  "tools": [{"call": 1, "name", "arguments", "result", "result_chars", "latency_ms", "error"}],
  "final": {"raw", "answers", "validated", "error"},
  "status", "error", "latency_ms"
}
```

- Messages are stored as deltas so a trace grows linearly, not quadratically; the full transcript of any call
  is the concatenation of the deltas up to it.
- **Traces contain source code.** They are written only to the server-side trace directory, never returned to
  a client, and `trace.dir` is refused unless `trace.enabled_for: development` is also set, so a production
  config cannot turn them on by accident. The dev stack mounts the run directory's `traces/pacds/`.

**Client-side traces** — the support agent and the baseline record each model call the same way (messages
delta, response, usage, latency, finish reason, attempts), keyed by case and repeat:
`traces/agent/<case>-<n>.json`, `traces/baseline/<case>-<n>.json`. The agent trace links each `call_pacds`
to the PACDS request id.

**Run manifest** — `run.json` gains: resolved PACDS config without secrets, the SHA-256 of every prompt and
skill file, the case set with each case's label and review tier, and the repeat count.

### 3.2 Run layout and persistence

```
eval-runs/<UTC time>/
  run.json  eval.log  compose.log  errors.json
  results/{replay-N,support-N}.json
  traces/{pacds,agent,baseline}/*.json
  report/{report.md, cases.jsonl, costs.json}
```

A full run is estimated at 20–40 MB of traces (tool results are about 4M tokens; the rest is deltas),
a few MB compressed. Finished runs are kept in S3 (decided, §7.1), because the container is reclaimed and
`eval-runs/` is git-ignored: `scripts/eval.sh` uploads `<run>.tar.gz` plus `<run>.run.json` when
`PACDS_EVAL_ARCHIVE_S3_URI` is set; `python -m tests.eval_run list|fetch` gets them back. The archive
credentials allow put and get but not delete, so the archive is append-only.

As built (phase 1): results files stay at the run root (`replay-N.json`, `support-N.json`), each listing its
cases with labels and its repeat count; client traces go to `traces/<results name>/<case>-<repeat>.json`, so
two support variants in one run do not collide; PACDS's resolved config (secrets removed) is kept in
`pacds-config.json` and `run.json`.

### 3.3 Analysis (offline, no LLM calls)

`python -m tests.analysis report <run>` writes `report/`:

- **Outcomes** — accuracy by class and by case with Wilson intervals; per-case outcomes across repeats;
  confusion matrix; escalation accuracy.
- **Per-case record** (`cases.jsonl`) — label, reviewed fix, each repeat's decision, the PACDS questions and
  answers, and for each PACDS request: turns, files read, history consulted, tokens, final answer.
- **Cost** — input/output tokens by component (agent, PACDS investigate, PACDS final, baseline), by tool,
  and the share of input that is resent history; cost per case and per correct decision.
- **Reliability** — errors by code and provider status, retries, finish reasons, time-budget exhaustion.
- **Behavior** — questions per ticket, how often the agent asks about deliberateness, origin, ownership,
  history; tool mix and turn counts in PACDS.

`python -m tests.analysis compare <runA> <runB>` pairs runs case by case: cases that flipped each way, a
paired test (exact McNemar on per-case majority), and cost differences.

### 3.4 Failure-mode classification (LLM-assisted, recorded)

The miss analysis done by hand in this round becomes a recorded step:
`python -m tests.analysis classify <run>` sends each miss (agent transcript + linked PACDS traces + label and
reviewed fix) to a reviewer model with a checked-in prompt and a fixed taxonomy:

| Mode | Meaning |
|---|---|
| `no_pacds` | the agent did not ask |
| `wrong_questions` | no question targeted the deciding fact |
| `pacds_wrong` | PACDS answered the deciding question away from the truth |
| `pacds_wrong_evidence` | ... and its trace shows it read the wrong code or none |
| `agent_overrode` | PACDS pointed to the truth; the agent decided otherwise |
| `label_debatable` | the agent's reasoning is sound under the playbook |
| `infrastructure` | the ticket failed for provider or harness reasons |

Results are cached per (run, case, repeat, prompt hash), so re-running the report never repays for them.

### 3.5 Replay (validate changes without re-investigating)

Every model call is recorded with a hash of its request. Replay mode serves recorded responses for identical
requests and makes live calls only on a miss, recording those too.

| Change | What replays | What runs live |
|---|---|---|
| Analysis or report code | everything | nothing |
| PACDS final-answer instruction or answer parsing | investigations | final-answer calls only |
| Question wording in the fixed-question replay | investigations | final-answer calls only |
| Support playbook / agent prompt | PACDS answers for unchanged questions | agent calls; PACDS only for new questions |
| PACDS investigation prompt or tools | nothing | full investigations — targeted cases only |

Replay is exact only for identical requests; a playbook change that makes the agent ask new questions still
pays for those PACDS calls. The report states how many calls were replayed and how many were live.

### 3.6 Targeted runs

The harness and runner accept `--from-run <run> --select <filter>`: `misses`, `class=D`,
`mode=wrong_questions`, `flipped`, or explicit case ids, plus a fixed **regression sample** (a stratified
subset of about 12 cases, 3 per class) that every targeted run also includes, so a fix for one class cannot
silently break another.

## 4. Workflow

1. **Run** at a milestone (full set, 3 repeats), fully traced.
2. **Report and classify** offline.
3. **Hypothesis** from the evidence: a failure mode, the cases showing it, the trace excerpts that explain it.
4. **Change**, then **validate** by replay where the table allows, otherwise a targeted run on the affected
   cases plus the regression sample; compare against the milestone run case by case.
5. **Full run** only when a set of validated changes is ready, or when the model or provider changes.

Each step writes to the run directory; decisions and their evidence go into a dated findings note under
`docs/evaluation/`.

## 5. Phases and acceptance criteria

Status: **phase 1 done** (2026-09-28, acceptance run `20260928T-phase1-check`: 8/8 PACDS requests traced, trace
usage equal to audit usage). **Phase 2 built** (`tests/analysis`): report, compare, select, regression sample
(`tests/replay/candidates/regression-sample.json`, 10 cases: A has only one reviewed case), `--from-run/--select`
in both runners; verified on the phase 1 run and a targeted baseline run. Its acceptance check (reproduce this
round's numbers and the resend estimate) needs a new traced milestone run, since earlier runs have no traces.
Taxonomy and escalation rule are read from each results file, so any case set in the `case.json` format works;
the playbook question checks (`tests/analysis/checks.py`) are the one dataset-specific part.
**Phase 3 built** (`pacds.engine.replay`): `scripts/eval.sh --replay-from RUN[,RUN...]` serves recorded responses
to identical requests in PACDS (dev-gated `trace.replay_from`, mounted read-only by `dev/compose.replay.yaml`), the
support agent and the baseline (`--replay-from` in the runners). Requests are matched by the SHA-256 already in every
trace; the same request recorded in several repeats is served once per recording. Replayed calls are marked in the
new traces and counted apart in the report. Unit tests cover both acceptance criteria; the live check follows the
milestone run. Caveat: `max_output_tokens` is not part of the request hash. **Acceptance 1 met**: replaying milestone
part 1 made zero live calls out of 1,078 and reproduced every decision. Recorded failures (connection, HTTP) are
replayed as failures; a call cancelled by the time budget cannot be and goes live. **Acceptance 2 met**: a changed final
instruction re-ran only the final calls (3 of 31) of three recorded investigations.
**Phase 4 built** (`python -m tests.analysis classify`, prompt `tests/analysis/prompts/classify.md`, model gpt-6-luna
as chosen): `infrastructure` and `no_pacds` by rule, the other modes by the model from a dossier (label, reviewed fix,
agent conversation, PACDS questions, answers and tool calls); cached per prompt hash; shown in the report and usable
as `--select mode=X`. About 7K input tokens per miss. Its acceptance check has to change: the hand analysis it was to
agree with came from runs whose transcripts were lost, so it will be checked against a fresh hand classification of
a sample of the milestone's misses instead. **Met**: 21 of 24 agree with a hand classification of milestone part 1's
misses (`docs/evaluation/2026-09-28-hand-classification.json`); the three differences are borderline.
**Phase 2 acceptance**: the report reproduces the runners' own accuracy for every run, and from traces alone the
resend share (85% of PACDS input; 79% was last round's estimate).

| Phase | Delivers | Done when |
|---|---|---|
| 1. Capture | PACDS traces (dev-gated), client traces, manifest | a 3-case run yields complete traces; every model call in PACDS's audit usage is accounted for in its trace; unit tests; the production config refuses `trace.dir` |
| 2. Analysis | `report`, `compare`, `--from-run/--select`, regression sample | the report reproduces this round's numbers and the 79% resend estimate from traces alone |
| 3. Replay | recorded calls, replay mode | re-running a finished run with no code changes makes zero live calls; a final-instruction change re-runs only final calls |
| 4. Classification | `classify` with checked-in prompt and cache | agrees with this round's hand analysis on the same misses |

Token reductions (smaller tool results, trimming resent history, prompt caching) come after phase 3: the
traces measure the saving and replay checks the answers do not change.

## 6. Security

- PACDS traces hold source code and log content. They exist only in development configurations, are written
  server-side, and are never part of any API response. A unit test asserts that the shipped production
  config sample cannot enable them.
- Client traces hold what clients already see (answers, their own prompts); attachment URLs are already kept
  out of the agent's context.
- Run archives inherit the trace sensitivity: a run on a private repository must be stored like that code.

## 7. Open questions

1. ~~Where are finished runs kept?~~ **Decided: S3** (`PACDS_EVAL_ARCHIVE_*` in the environment).
2. ~~Which model classifies failure modes?~~ **Decided: gpt-6-luna**, the model under test.
3. **Regression sample**: pick it once from the 51 reviewed cases now (stratified by class and review tier),
   or re-draw it per milestone? *Built as fixed until deliberately redrawn* (`python -m tests.analysis sample
   --candidates --write`); either policy works with it.
