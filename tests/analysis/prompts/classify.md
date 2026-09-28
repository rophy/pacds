You review one support ticket that a support agent triaged wrongly, to find out why.

The support agent handles tickets for an application its organization operates. It classifies each ticket as one of
the classes below and can ask PACDS, a service that investigates the application's source code and logs, questions
about the ticket (at most a few calls, each with several questions). The agent then decides alone.

You get: the ticket's true class and the fix the development team reviewed as correct; the agent's conversation
(the ticket, its questions to PACDS, PACDS's answers, its decision); and, for each PACDS call, what PACDS
investigated (the tools it called with their arguments, the files it read, whether it looked at the git history)
and its answers.

Pick the one failure mode that best explains the wrong decision:

- wrong_questions: the agent asked PACDS, but no question targeted the fact that decides the true class (for
  example it never asked whether the behavior changed in a recent version, or who controls the failing setting).
- pacds_wrong: a question targeted the deciding fact, and PACDS's answer pointed away from the truth although its
  investigation looked at the relevant code.
- pacds_wrong_evidence: a question targeted the deciding fact, and PACDS's answer pointed away from the truth
  because it read the wrong code or none (its tool calls never reached the component the reviewed fix is about).
- agent_overrode: PACDS's answers pointed to the true class (or to the deciding fact), and the agent decided
  otherwise.
- label_debatable: the agent's reasoning is sound under its playbook and the evidence it had; the true class is
  arguable for this ticket.

Judge from the evidence given, not from your own knowledge of the application. Name the deciding fact: the one
thing that, known and weighed correctly, gives the true class.

Answer with a JSON object: {"mode": one of the modes above, "deciding_fact": one sentence, "explanation": at most
three sentences citing the question or answer that shows it}.
