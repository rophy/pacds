from pathlib import Path

from pacds.config import load_config
from pacds.devtools.show_config import public_config

DEV = Path(__file__).parents[2] / "dev" / "pacds.yaml"
ENV = {"LLM_BASE_URL": "http://llm", "LLM_MODEL": "m", "LLM_API_KEY": "sk-secret", "LLM_SESSION_HEADER": "", "LLM_API": "",
       "LLM_MAX_OUTPUT_TOKENS": "", "PACDS_TRACE_DIR": "/traces"}


def test_public_config_has_no_api_key_but_everything_else():
    shown = public_config(load_config(DEV, env=ENV))
    assert "sk-secret" not in str(shown)
    assert shown["llm"]["api_key"] == "<redacted>" and shown["llm"]["model"] == "m"
    assert shown["trace"] == {"dir": "/traces", "enabled_for": "development"}
    assert shown["git"]["history_depth"] == 500
