"""Print the resolved PACDS config as JSON without secrets, for evaluation run records (scripts/eval.sh).

Usage: python -m pacds.devtools.show_config [PATH]   (default $PACDS_CONFIG)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from pacds.config import Config, load_config

SECRET_KEYS = frozenset({"api_key"})


def without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "<redacted>" if key in SECRET_KEYS and item else without_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_secrets(item) for item in value]
    return value


def public_config(config: Config) -> dict[str, Any]:
    return without_secrets(config.model_dump(mode="json"))


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PACDS_CONFIG", "/etc/pacds/config.yaml"))
    print(json.dumps(public_config(load_config(path)), indent=2))


if __name__ == "__main__":
    main()
