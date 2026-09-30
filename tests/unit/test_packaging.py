import ast
import re
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).parents[2] / "src" / "pacds"


def _imports_toolkit_outside_functions(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    outside = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return any("pacds_eval" in ast.dump(n) for n in outside)


def test_service_never_imports_the_toolkit():
    for path in SERVICE.rglob("*.py"):
        if path == SERVICE / "cli.py":  # the dispatcher imports the toolkit lazily, inside a function body
            assert not _imports_toolkit_outside_functions(path), path
        else:
            assert not re.search(r"pacds_eval", path.read_text()), path
    check = "import pacds.main, pacds.app, pacds.cli, sys; assert not [m for m in sys.modules if m.startswith('pacds_eval')]"
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


ROOT = Path(__file__).parents[2]


def test_bundle_compose_defaults_the_image_and_the_repository_copy_requires_it(tmp_path):
    import tarfile

    subprocess.run(["scripts/build-bundles.sh", "9.9.9", str(tmp_path)], cwd=ROOT, check=True, capture_output=True)
    with tarfile.open(tmp_path / "pacds-deploy-9.9.9.tar.gz") as bundle:
        compose = bundle.extractfile("pacds-deploy-9.9.9/compose.yaml").read().decode()
    assert "${PACDS_IMAGE:-ghcr.io/rophy/pacds:9.9.9}" in compose and "PACDS_IMAGE:?" not in compose
    assert "${PACDS_IMAGE:?" in (ROOT / "deploy" / "compose.yaml").read_text()


def test_image_defaults_and_dockerignore():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "PACDS_CASES_DIR=/cases" in dockerfile and "PACDS_RUNS_DIR=/runs" in dockerfile
    assert "**/.env" in (ROOT / ".dockerignore").read_text().split()
