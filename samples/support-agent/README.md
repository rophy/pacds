# Support agent sample

A small, readable example of a support agent that uses PACDS. It is a starting point to copy and adapt, not a
supported component. `agent.py` is about 130 lines and depends only on `openai` and `httpx`.

It reads a ticket, lets an LLM triage it with the skills in `skills/`, and lets the LLM ask PACDS questions about the
ticket's code version and logs (at most 3 PACDS calls and 10 model turns). The decision is printed as JSON; the exit
code is 1 when the model reaches no decision.

## Get it

It ships with every release, as real files: in the `pacds-samples-<version>.tar.gz` asset of the GitHub release, and in
the image at `/opt/pacds/samples/support-agent`:

```
docker run --rm --entrypoint tar ghcr.io/rophy/pacds:<version> -C /opt/pacds/samples -cf - support-agent | tar -xf -
```

## Setup

```
pip install -r requirements.txt
```

Environment:

| Variable | Meaning |
| --- | --- |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | Any OpenAI-compatible chat-completions endpoint |
| `PACDS_URL` | PACDS base URL, e.g. `https://pacds.corp.example` |
| `PACDS_TOKEN` | A bearer token, or instead: |
| `PACDS_OIDC_TOKEN_URL`, `PACDS_OIDC_CLIENT_ID`, `PACDS_OIDC_CLIENT_SECRET` | Client-credentials grant against your identity provider (the token is fetched once per run; the run exits 2 if that fails) |

## Run

```
python agent.py example-ticket.json
```

A ticket is `{"report", "repo", "ref", "logs"}`: `repo` is an https git URL, `ref` a tag or full commit of the version
the user runs, and `logs` (optional) lists `{"name", "url"}` files your log store already serves, for example presigned
URLs. Uploading logs is out of scope. The model only names logs; their URLs are taken from the ticket.

## Adapting

The agent's behaviour lives in `skills/`: `tech-support/SKILL.md` is the triage procedure and holds the definitions of
classes A to D, `pacds/SKILL.md` teaches the model to write PACDS questions. Edit them to fit your product and your
routing. In this repository `skills` is a symlink to the copy PACDS's evaluation uses; in a release tarball or the image
it is a real directory. `agent.py` builds the system prompt from both files: "You handle support tickets for
{application}. Use your skills below. Submit a decision for every ticket."

## The PACDS request

When the model calls `call_pacds`, the agent POSTs to `{PACDS_URL}/v1/systemone` with `Authorization: Bearer <token>`:

```json
{
  "model": "pacds",
  "state": {
    "user_report": "Checkout returns 'Payment provider unavailable' for every card.",
    "pacds": {
      "git": {"url": "https://git.corp.example/acme/web-shop.git", "ref": "v2.4.1"},
      "logs": [{"name": "server.log", "url": "https://logs.corp.example/tickets/1042/server.log?sig=example"}]
    }
  },
  "questions": {
    "provider_timeout": {"type": "noul", "instructions": "Does the code call the payment provider with a timeout shorter than 5 seconds?"}
  }
}
```

The `document` argument of the tool becomes the top-level keys of `state`; the agent adds `state.pacds`. A response:

```json
{"model": "pacds", "answers": {"provider_timeout": {"type": "noul", "noul": 0.94}}, "usage": {...}}
```

On failure the body is an error object, which the agent hands to the model unchanged. See `skills/pacds/SKILL.md` for the
question types (`noul`, `choice`, `score`) and how to phrase them.
