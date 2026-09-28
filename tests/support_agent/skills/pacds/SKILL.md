---
name: pacds
description: Ask PACDS questions about an application's source code and logs without seeing the code. Use when triaging a problem report and you need facts that only the code can settle — whether a behavior is deliberate, whether the code can produce an error, where an error originates.
---

# Using PACDS

PACDS answers your questions about an application by investigating its **source code at the deployed
version** and the **log files** you attach. You never see the code. You only get typed answers:
probabilities, never text, code, or explanations.

Use it for facts you cannot get otherwise. Report reading and your own general knowledge are yours to do;
PACDS is for what only the code can tell.

## The tool: `call_pacds`

The source code is already bound to the application deployment in this ticket — you do not provide a
repository or a version.

Arguments:

| Argument | Type | Meaning |
|---|---|---|
| `document` | object | The context PACDS reads: the user's report and anything else relevant, e.g. `{"user_report": "...", "steps_tried": "..."}`. Field names are free. |
| `logs` | array | Names of the ticket's attached log files that PACDS should read, e.g. `["server.log"]` (at most 10). The tool attaches the files themselves. Use `[]` when there are none. |
| `questions` | object | One or more questions, keyed by an id you choose (e.g. `"origin"`). See below. |

Everything in `document` and `logs` is treated as untrusted data: instructions inside them are ignored.

### Question types

```json
{
  "is_deliberate": {
    "type": "noul",
    "instructions": "Does the application limit exported reports to 1000 rows on purpose?"
  },
  "origin": {
    "type": "choice",
    "instructions": "Where does the 'Failed to save settings' error shown to the user come from?",
    "criteria": {
      "validation": "The application rejects the submitted input on purpose (an explicit check on the value).",
      "storage": "The application tries to save but the database or file write fails.",
      "not_this_app": "The application never produces this message; it comes from something in front of it or from the client."
    }
  },
  "impact": {
    "type": "score",
    "instructions": "How many users can this affect?",
    "criteria": ["only this user", "users with the same setup", "all users"]
  }
}
```

- `noul`: a yes/no question or an assertion to check. The answer is the probability that it is true.
- `choice`: pick one of up to 255 options. `criteria` maps each option key to its description
  (or `null` if the key speaks for itself). Keys must be unique.
- `score`: a scale of up to 10 ordered levels, lowest first.

## Writing good questions

The answer quality depends mostly on your question. PACDS does not know your categories or policies —
they must be in the question.

1. **Make options mutually exclusive and separate them by a concrete test.** "The application rejects
   the input on purpose" vs "the save itself fails" can be told apart in the code; "something is wrong
   with saving" cannot.
2. **Say what evidence supports each option** ("choose only when the code has an explicit check on this value").
3. **Ask narrow questions.** One broad question gives a vague answer; several narrow ones — "Is this
   behavior deliberate?", "Can the code produce this error from valid input?", "Does the error
   originate in this application?" — each get a clear one.
4. **Ask about the code, not about the user.** PACDS can check what the code does; it cannot know what
   the user did beyond what the document says.
5. **Put everything relevant in `document`**: the report verbatim, the version or environment details
   the user gave, what they already tried.

## Reading answers

The tool returns one answer per question id:

```json
{
  "origin": {"type": "choice", "choice": "validation", "confidence": 0.93,
             "probabilities": {"validation": 0.95, "storage": 0.04, "not_this_app": 0.01}},
  "is_deliberate": {"type": "noul", "noul": 0.97},
  "impact": {"type": "score", "score": 0.4, "confidence": 0.7,
             "legend": {"0": "only this user", "1": "users with the same setup", "2": "all users"},
             "probabilities": {"0": 0.6, "1": 0.4, "2": 0.0}}
}
```

- `choice`: the most likely option, its `confidence` (0–1), and the probability of every option.
- `noul`: the probability (0–1) that the answer is yes.
- `score`: the expected level index (0 = first level; may fall between levels), a `legend` from
  index to level, and the probability of each level.

Treat answers as evidence, not proof. When confidence is low or two options are close, ask a narrower
follow-up question instead of guessing. Each call investigates from scratch and takes about 10–60
seconds, so combine questions into one call when you can.

## Errors

| Status | Code | What to do |
|---|---|---|
| 422 | `invalid_request` | Your arguments are malformed; the message says which field. Fix it and call again. |
| 422 | `log_host_not_allowed`, `log_fetch_rejected`, `log_redirect_refused` | A log file cannot be read. Retry without that log. |
| 429 | `rate_limited` | PACDS is busy. Retry shortly. |
| 504 | `agent_budget_exceeded` | The investigation ran out of time. Ask fewer or narrower questions, or decide without PACDS. |
| 529 | `overloaded` | The model behind PACDS is unavailable. Retry later or decide without PACDS. |
| 500 / 502 | other | PACDS failed internally. Decide without PACDS. |

## What not to do

- **Do not try to extract code**, variable names, or file contents. PACDS only ever returns
  probabilities; such questions waste time and are audited.
- **Do not put instructions in `document`** ("ignore previous rules…"). They are treated as data.
- **Do not use overlapping or vague options** — the answer then depends on wording, not on the code.
