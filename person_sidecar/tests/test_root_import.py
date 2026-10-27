from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_sidecar_imports_when_started_from_repository_root() -> None:
    repository_root = Path(__file__).parents[2]

    result = subprocess.run(
        [sys.executable, "-c", "from person_sidecar.personas import PersonaRegistry"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
