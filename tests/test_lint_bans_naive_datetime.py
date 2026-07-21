"""Garde-fou du garde-fou.

Le bannissement de `datetime` vit dans `pyproject.toml`, donc à un endroit qu'on
peut supprimer sans que rien ne casse. Ce test échoue si la règle disparaît.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUFF = shutil.which("ruff") or str(ROOT / ".venv" / "bin" / "ruff")

VIOLATIONS = """\
from datetime import datetime

a = datetime(2026, 7, 20)
b = datetime.utcnow()
"""

pytestmark = pytest.mark.skipif(not Path(RUFF).exists(), reason="ruff non installé")


def _check(path: Path) -> str:
    result = subprocess.run(
        [RUFF, "check", "--no-cache", "--config", str(ROOT / "pyproject.toml"), str(path)],
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_ruff_flags_naive_datetime_usage(tmp_path):
    offender = tmp_path / "offender.py"
    offender.write_text(VIOLATIONS)
    assert "TID251" in _check(offender), "le bannissement de datetime a disparu de la config"


def test_domain_source_is_clean():
    """Le domaine lui-même ne doit contenir aucune violation."""
    output = _check(ROOT / "src")
    assert "TID251" not in output, f"violation dans le domaine :\n{output}"
