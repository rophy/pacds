# Context and cost (2026-09-29)

Phase 5 of docs/superpowers/specs/2026-09-29-corporate-deployment-design.md. Data: the traced milestone of
2026-09-28 (gpt-6-luna, 51 Debezium cases × 3, support agent with PACDS), 227 live PACDS investigations.

## Where the tokens go

An investigation resends its transcript on every turn: 85% of PACDS input is resent history, 76% resent tool
results (search_code and read_file most). With a prefix cache that is cheap: only the new part of each prompt is
computed. The exception was the final answer request, which dropped the tools and so shared no prefix with the
investigation: 14% of input, computed in full every time.

## Offline simulation

`python -m tests.analysis context RUN1 RUN2 ...` (no LLM calls) replays the recorded investigations under a policy,
holding the model's trajectory fixed. It reproduces the recorded totals (39.5M input, 11.4M uncached).

| policy | input | uncached | largest prompt p50 / p90 / max | investigations touched |
|---|---|---|---|---|
| final without tools, no budget (before) | 39.5M | 11.4M | 25K / 38K / 60K | 0 |
| final with tools, no budget (now) | 39.5M | **5.7M** | 25K / 38K / 60K | 0 |
| final with tools, budget 48,000 | 39.3M | 5.8M | 25K / 38K / 48K | 2 |
| final with tools, budget 32,000 | 35.9M | 6.5M | 24K / 31K / 32K | 51 |
| final with tools, budget 24,000 | 30.7M | 7.2M | 22K / 24K / 24K | 120 |

- Keeping the tools in the final request (`tool_choice: none`) halves the uncached input without changing the
  conversation. Now the default (`llm.final_keeps_tools`).
- A context budget (`llm.context_budget_tokens`) trims total input but raises uncached input: each removal changes
  an early message, so the cache restarts. It is for context windows smaller than an investigation needs, not a
  saving; off by default.

## Where the schema goes matters

A probe against OpenCode Go (Responses API) with a 12K-token transcript, first request of each shape after the
investigation's:

| final request | cached |
|---|---|
| no tools, JSON schema (before) | 0 |
| tools, `tool_choice: none`, JSON schema | 0 on first use (the schema is rendered into the prompt), full on repeats |
| tools, `tool_choice: none`, `json_object` or no format | 12,028 of 12,116 |

vLLM applies a JSON schema by guided decoding, outside the prompt, so keeping the tools is enough there. On a hosted
API that renders the schema into the prompt, the final request is cached only with `llm.structured_outputs: false`.
In the live check below the finals ran against a cold cache (the investigations were replayed), so it measures
accuracy, not latency.

## Accuracy check

The milestone's support-agent run repeated with the final request keeping its tools, replaying every unchanged call
(`--replay-from` the milestone): 2,028 PACDS calls and 297 agent calls replayed, 324 and 104 live.

| | milestone | final with tools |
|---|---|---|
| all (153 tickets) | 52% (80/153) | 52% (79/153) |
| cases flipped | | 2 each way, McNemar p = 1.0 |

No change. Run `20260929T144100Z` (with the partial runs `20260929T140248Z` and `20260929T143838Z` as recordings;
the container restarted during the first).

## Replay fixes found on the way

Replaying the milestone, which was assembled from a run and a re-run of the tickets its weekly usage limit failed,
served the recorded rate-limit failures instead of the re-run's answers. Replay now never serves infrastructure
failures (connection errors, 429, overload, 5xx): those calls go live. Deterministic errors (e.g. 400) still replay,
after any successful recording of the same request.
