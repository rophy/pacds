"""Playbook-specific behavior checks on the questions a support agent asks PACDS.

Heuristic keyword matches, one per step of tests/support_agent/skills/tech-support/SKILL.md that last round's miss
analysis tied to wrong decisions. Swap this list when the playbook changes; the rest of the report does not depend
on it.
"""

from __future__ import annotations

import json
import re
from typing import Any

QUESTION_CHECKS: dict[str, re.Pattern[str]] = {
    # Is the behavior produced on purpose? (C/B vs D)
    "deliberate": re.compile(r"deliberate|intention|intended|by design|on purpose|expected behaviou?r", re.I),
    # Did a change in this application cause it? (D misses: regressions that look deliberate)
    "regression": re.compile(r"regress|upgrad|previous version|used to|recent(ly)? (change|commit)|changed in|since version|history", re.I),
    # Who controls the failing setting? (C misses: DB grants, publications, topic config, platform classpath)
    "ownership": re.compile(r"who (controls|configures|owns|manages|sets)|operator|outside (of )?(the|this) application|"
                            r"external (system|component|service)|grant|privilege|permission|environment|infrastructure", re.I),
}


def question_text(questions: Any) -> str:
    return json.dumps(questions) if not isinstance(questions, str) else questions


def matched_checks(questions: Any) -> set[str]:
    text = question_text(questions)
    return {name for name, pattern in QUESTION_CHECKS.items() if pattern.search(text)}
