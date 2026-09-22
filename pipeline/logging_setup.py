"""Logging: readable on the console, complete in logs/."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONSOLE_FMT = "%(levelname)-8s %(name)-28s %(message)s"
_FILE_FMT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):  # idempotent across repeated calls
        root.removeHandler(handler)

    # Windows consoles still default to a legacy code page in some shells, which
    # turns any Hebrew log line into UnicodeEncodeError. Phase 5 emits Hebrew, so
    # force UTF-8 here rather than debugging it later.
    for stream in (sys.stderr, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # already-detached or non-reconfigurable stream
                pass

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(console)

    logfile = RotatingFileHandler(
        log_dir / "pipeline.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(logging.Formatter(_FILE_FMT))
    root.addHandler(logfile)

    # yfinance/urllib3 are chatty at DEBUG and drown the real signal.
    for noisy in ("urllib3", "yfinance", "peewee", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
