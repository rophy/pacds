# PACDS

## Git commit policy

Commit message format:

```
<type>: <short description>

[optional body explaining why/what changed]
```

- NO "Generated with Claude Code" footer
- NO "Co-Authored-By: Claude" line
- NO mention of "Claude" or "Happy" anywhere
- Keep messages short (1-5 lines preferred)
- Types: feat, fix, refactor, chore, docs, build, test

## Evaluation

- `scripts/e2e.sh` is no-cost (fake LLM); `scripts/eval.sh` uses the real LLM from `LLM_*` and costs usage.
- Status and results: `docs/evaluation/` (latest findings note) and
  `docs/superpowers/specs/2026-09-28-eval-analysis-framework-design.md` (§5 phase status).
- Every `eval.sh` run is archived on exit to S3 when `PACDS_EVAL_ARCHIVE_S3_URI` is set
  (`python -m tests.eval_run list | fetch NAME`). The archive credentials cannot delete.
- Analysis is offline (`python -m tests.analysis report|compare|select|sample`, see `tests/analysis/__main__.py`).
  Targeted runs: runner args `--from-run RUN --select misses|errors|class=X|tier=X|flipped=RUN`, plus the regression
  sample in `<cases dir>/regression-sample.json`. A milestone split across runs is `RUN1,RUN2,...`.
- `eval.sh --replay-from RUN` re-runs with recorded model calls: only work whose request changed calls the LLM.
- `eval.sh --target URL` evaluates a deployed PACDS instead of the Compose stack (docs/evaluation-runbook.md);
  deployment is `deploy/` + docs/deployment.md. Case sets live outside the repo (`--cases-dir` / `PACDS_CASES_DIR`);
  `python -m tests.replay.casebook` imports, reviews and labels them.
- OpenCode Go has a weekly usage limit besides the 5-hour one; a 429's `Retry-After` says when it resets.
- Never edit the checkout an `eval.sh` run is executing from: it launches each step from the tree, and bash reads
  scripts as it goes. Develop in a git worktree meanwhile.

## Environment gotchas (cloud container)

- Use `PACDS_EVAL_ARCHIVE_*` for the run archive. Never use `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (placeholder
  values set by the environment) or `PACDS_S3_ACCESS_KEY`/`PACDS_S3_SECRET_KEY` (dev MinIO).
- The container restarts occasionally and `dockerd` does not come back. Start it with
  `rm -f /var/run/docker.pid; nohup dockerd >/tmp/dockerd.log 2>&1 &`, then wait for `docker info`.
- Never `pkill -f <pattern>` where the pattern appears in your own command line (it kills the shell).
- Check test exit codes directly, not through a pipe (`cmd > log; echo $?`).
