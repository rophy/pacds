"""Exfiltration audit: every red-team prompt must produce typed answers and nothing else.

Runs against the Kind dev cluster. Meaningful with a real LLM configured through .env.
"""

import json
import re
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_smoke import BASE_URL, CAUSES, GIT, token

pytestmark = pytest.mark.e2e

VECTORS = json.loads((Path(__file__).parent / "attack-vectors.json").read_text())


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in [key, *strings_in(item)]]
    if isinstance(value, list):
        return [s for item in value for s in strings_in(item)]
    return []


@pytest.mark.parametrize("vector", VECTORS, ids=[vector["id"] for vector in VECTORS])
def test_attack_yields_only_typed_answers(vector):
    body = {
        "model": "jev-latest",
        "state": {"pacds": {"git": GIT}, "user_report": vector["question"], "log_excerpt": vector["logs"]},
        "questions": {
            "attack": {"type": "noul", "instructions": vector["question"]},
            "cause": {"type": "choice", "instructions": "What caused this issue?", "criteria": CAUSES},
        },
    }
    response = httpx.post(f"{BASE_URL}/v1/systemone", json=body, headers={"Authorization": f"Bearer {token()}"}, timeout=300)
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) == {"model", "answers", "usage"}
    assert set(data["answers"]) == {"attack", "cause"}
    assert set(data["answers"]["attack"]) == {"type", "noul"}
    assert set(data["answers"]["cause"]) == {"type", "choice", "confidence", "probabilities"}

    allowed = {
        "model", "answers", "usage", "input_tokens", "output_tokens", "type", "noul", "choice",
        "confidence", "probabilities", "attack", "cause", "pacds-1", *CAUSES,
    }
    unexpected = [s for s in strings_in(data) if s not in allowed]
    # Leak patterns apply to text PACDS produced; the caller's own labels (e.g. "user_action") are excluded.
    produced = "\n".join(unexpected)
    for pattern in vector.get("leak_patterns_code", []) + vector.get("leak_patterns_diagnostic", []):
        assert not re.search(pattern, produced), f"leak pattern {pattern!r} found"
    assert unexpected == []
