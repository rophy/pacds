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

## Environment gotchas (cloud container)

- Use `PACDS_EVAL_ARCHIVE_*` for the run archive. Never use `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (placeholder
  values set by the environment) or `PACDS_S3_ACCESS_KEY`/`PACDS_S3_SECRET_KEY` (dev MinIO).
- The container restarts occasionally and `dockerd` does not come back. Start it with
  `rm -f /var/run/docker.pid; nohup dockerd >/tmp/dockerd.log 2>&1 &`, then wait for `docker info`.
- Never `pkill -f <pattern>` where the pattern appears in your own command line (it kills the shell).
- Check test exit codes directly, not through a pipe (`cmd > log; echo $?`).
