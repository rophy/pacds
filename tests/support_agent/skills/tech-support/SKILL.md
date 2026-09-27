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

### Telling the classes apart

Two questions separate them:

1. **Is the application's code responsible for the symptom?**
   - If the code produces the behavior **deliberately** (an explicit condition, validation, permission
     check, documented limit, comment, confirmation dialog), it works as designed → **B**, not D.
   - If the code produces it **accidentally** (faulty logic that the developers did not intend) → **D**.
   - If the code **cannot** produce the symptom for this input — it handles the input correctly — the
     cause is outside the application → A or C.
2. **If outside the application, who controls the cause?**
   - The operator of this deployment runs or configures it → **C**.
   - Nobody in your organization controls it (the user's side or a third party) → **A**.

A report that "something does not work" is not proof of a bug. Many reports that read like bugs are B
or C once the facts are known.

## Procedure

1. Read the report and the attached logs. Note what the user did, what they expected, and what happened.
2. List the classes that are still plausible.
3. Identify the **fact that would decide** between them — usually one of the two questions above.
4. Gather that fact. When the deciding fact is in the application's code (is this deliberate? can the
   code produce this error? where does it originate?), use the code-investigation tool you have.
   Do not guess what the code does.
5. Decide the class. If the evidence stays inconclusive, pick the most likely class and give it a low
   confidence rather than escalating by default.
6. Submit the decision: the class, whether to escalate to the development team (only for D), and your
   confidence (0–1).
