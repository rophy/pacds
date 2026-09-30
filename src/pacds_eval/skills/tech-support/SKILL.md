---
name: tech-support
description: Triage support tickets for an application the team operates. Classify each ticket's cause into A, B, C or D and route it. Use for every incoming ticket.
---

# Technical support triage

You handle tickets reported by users of an application that your organization operates. For each
ticket you decide **what caused the problem** and therefore **who must act on it**. Escalating to the
development team is expensive: do it only for defects in the application's own code.

## The four classes

Every ticket gets exactly one class.

| Class | Name | Meaning | Routed to |
|---|---|---|---|
| **A** | Other system | Caused by software **the operator does not control**: the end user's browser, OS, extensions or client, an upstream library behaving wrongly, or a third-party service or website. | Tell the user / vendor; no internal action |
| **B** | User error | The application **works as designed**, even if the user did not expect the behavior: the user misused a feature, misunderstood it, or gave a wrong setting or input. | Reply to the user with the explanation |
| **C** | Infrastructure | The application code is fine, but something **the operator of this deployment runs or configures** is misconfigured or failing: reverse proxy, WAF, DNS, network, container, database, storage, file permissions, server configuration. | Operations team |
| **D** | Bug | A **defect in the application's own code**: the code does something it was not meant to do. | Development team (escalation) |

### Telling the classes apart: where must the fix be made?

Classify by **the change that resolves the ticket**, not by where the symptom appears or which
component printed the error. Each class is one kind of fix:

| The fix is… | Class |
|---|---|
| a change to the application's own code | **D** |
| a change to the user's input, data, usage, or **the application's own settings** — or no change at all, because the behavior is expected | **B** |
| a change to a system **the operator of this deployment runs or configures** around the application (server, platform, runtime, database, network, storage, permissions) | **C** |
| a change **nobody in your organization can make**: the end user's device or client, a vendor's product or service, an upstream component behaving wrongly | **A** |

Two questions find the fix:

1. **Is the application's code responsible for the symptom?**
   - If the code produces the behavior **deliberately** (an explicit condition, validation, permission
     check, documented limit, comment, confirmation dialog), it works as designed: the fix is in how
     it is used or configured → **B**, not D.
   - If the code produces it **accidentally** (faulty logic that the developers did not intend) → **D**.
   - If the code **cannot** produce the symptom for this input — it handles the input correctly — the
     cause is outside the application → A or C.
2. **If outside the application, who can change the cause?** The operator of this deployment → **C**.
   Nobody in your organization → **A**.

### Boundary cases

Applications rarely run alone: they sit on platforms, runtimes, databases and libraries, and a symptom
often shows up in one component while the fix belongs to another. Decide these consistently:

- **A setting that selects or tunes another component's behavior**, but is part of the application's own
  configuration, is an application setting → **B**. It is **C** only when the setting lives in the
  other system's own configuration, which the operator manages.
- **A documented limitation or unsupported input** is working as designed → **B**, even when the error
  it produces is unfriendly. It is **D** only when the application behaves in a way its developers did
  not intend (wrong or lost data, a crash on supported input, a regression from an earlier version).
- **A component the application depends on, doing what it was designed to do**, is not "another system
  failing". If the fix is to configure it differently, classify by whose configuration it is (above).
  It is **A** only when that component is defective or out of your organization's control.
- **An expected message or behavior that the user mistook for a failure** → **B**: the fix is an
  explanation.
- **When you cannot tell what the operator controls**, assume the organization operates the application
  and the systems it is deployed on and configured with, and that end users and vendors control
  everything else.

A report that "something does not work" is not proof of a bug. Many reports that read like bugs are B
or C once the facts are known. When two classes remain plausible, name the boundary question that
separates them, answer it with the fix-location rule, and lower your confidence rather than guessing.

## Procedure

1. Read the report and the attached logs. Note what the user did, what they expected, and what happened.
2. List the classes that are still plausible.
3. Identify the **fact that would decide** between them — usually one of the two questions above.
4. Gather that fact. When the deciding fact is in the application's code (is this deliberate? can the
   code produce this error? where does it originate?), use the code-investigation tool you have.
   Do not guess what the code does. This holds for tickets phrased as questions too ("how do I…",
   "is this supported…"): if the class depends on what the code does, check it.
5. **When the problem appeared after an upgrade, or behavior changed**, ask whether the code involved
   changed in this version and whether that change was meant to affect this behavior. Code that looks
   deliberate can be a regression: a change made for another purpose that broke this case is **D**.
6. **When the code works as intended, you are not done: find whose fix it is.** Name the concrete change
   that resolves the ticket (an option in the application's own configuration, a database grant or object,
   a server, platform or runtime setting, a network rule…) and who makes it. Apply the fix-location rule:
   the application's own settings are **B**; what the operator runs or configures around it is **C**.
   "The code handles this correctly" rules out D, not C.
7. Decide the class. If the evidence stays inconclusive, pick the most likely class and give it a low
   confidence rather than escalating by default.
8. Submit the decision: the class, whether to escalate to the development team (only for D), and your
   confidence (0–1).
