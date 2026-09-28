# Corporate deployment — Design

**Date:** 2026-09-29
**Status:** Draft; phase 1 in progress
**Context:** accuracy testing moves to a corporate environment with a self-hosted LLM. This repository's job becomes
a PACDS service, evaluation framework, reference support agent and case-authoring tools that deploy there as-is.

## 1. Target environment (as stated)

| | |
|---|---|
| LLM | vLLM, OpenAI-compatible `/v1/chat/completions`, context window 128K or more |
| Platform | Docker / Compose on a VM |
| Deployed | PACDS service, evaluation framework, support agent + skills, case-authoring tools |
| Assumed | internal git host (HTTPS + token), S3-compatible log storage (MinIO/Ceph), corporate OIDC issuer, a corporate CA, possibly an HTTP proxy, internal image registry, no public internet |

Measured today (gpt-6-luna milestone, 236 investigations): the largest single request per investigation is 25K
tokens median, 38K p90, 60K max. A 128K window fits it; context management is a cost/latency item, not a blocker.

## 2. Principles

- **One image, configuration only.** Nothing corporate is baked into code or the image; registry, CA, hosts,
  issuer and model are configuration.
- **Private data never enters this repository.** Corporate cases, runs, traces and archives live in directories and
  buckets given by configuration.
- **Server-agnostic LLM layer.** vLLM is the target, but PACDS keeps to the OpenAI-compatible contract plus
  documented knobs; what the server must support is written down.
- **Production stays locked down.** Traces and replay remain development-only; the evaluation uses a separate PACDS
  instance with the dev-gated options, next to the production one.

## 3. Phases

| Phase | Delivers | Done when |
|---|---|---|
| 1. Deployable service | production config sample and Compose file, base images from a configurable registry, corporate CA bundle for every outbound connection (LLM, git, logs, JWKS), per-host private-IP allowlist for log storage, health endpoint, vLLM knobs (`extra_body`, structured-output switch, request timeout) | a clean VM with only the image, the sample config and a CA file runs PACDS against an OpenAI-compatible server; unit tests for each knob; the sample refuses traces |
| 2. Portable evaluation | eval against a deployed evaluation PACDS (`--target`), client-credentials tokens from any OIDC issuer, S3-compatible log storage and run archive (endpoint URL), no OpenCode/cloud assumptions | `eval.sh --target` runs replay, support, classify and report against a non-Compose PACDS with MinIO |
| 3. Case authoring | case sets outside the repo (`--cases-dir` / `PACDS_CASES_DIR`), `new`/`import` from a ticket export, blind-review workflow, catalog per case set | a corporate ticket export becomes a reviewed, cataloged case set without touching the repo |
| 4. Runbooks | deployment guide, evaluation runbook, vLLM requirements (tool parser, JSON schema, context length) | a new operator can deploy and run a milestone from the docs alone |
| 5. Context and cost | request budget and trimming of old tool results; measured with traces and replay | resend share and latency drop with no accuracy change on replay |

## 4. vLLM notes

- Tool calling needs the server started with `--enable-auto-tool-choice` and the model's `--tool-call-parser`.
- `response_format: json_schema` works through guided decoding; some model/parser combinations reject it together
  with tools or produce slow decoding, hence a switch to fall back to schema-in-prompt (the Anthropic path already
  does this).
- Reasoning models return their reasoning in a separate field; PACDS does not need it and must not resend it.
- Prefix caching (`--enable-prefix-caching`) is what keeps a resend-heavy agent loop fast; it needs byte-stable
  prefixes, which PACDS already has (append-only transcript).
