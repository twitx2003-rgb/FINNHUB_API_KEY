import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """run.main() installs console handlers bound to the test's captured stderr;
    once that capture closes, later tests would log into a closed stream."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
