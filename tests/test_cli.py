"""Every command CLAUDE.md tells the user to run must exist.

Added after a cleanup deleted four live commands by accident and no test
noticed (the functions stayed, their argparse entries were gone)."""
import re
from pathlib import Path

import pytest

import run

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTED = sorted(set(re.findall(r"run\.py (--[a-z][a-z-]+)",
                                   (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))))
REMOVED = {"--discover-session", "--seed-saved-answers"}   # recorded in CLAUDE.md as removed


def test_the_brief_documents_commands():
    assert len(DOCUMENTED) > 10


@pytest.mark.parametrize("flag", [f for f in DOCUMENTED if f not in REMOVED])
def test_documented_command_is_accepted(flag):
    options = {a for action in run.build_parser()._actions for a in action.option_strings}
    assert flag in options
