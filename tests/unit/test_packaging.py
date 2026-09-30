import re
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).parents[2] / "src" / "pacds"


def test_service_never_imports_the_toolkit():
    for path in SERVICE.rglob("*.py"):
        if path.name == "cli.py":  # the dispatcher imports the toolkit lazily, inside its eval branch
            continue
        assert not re.search(r"pacds_eval", path.read_text()), path
    check = "import pacds.main, pacds.app, sys; assert not [m for m in sys.modules if m.startswith('pacds_eval')]"
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
