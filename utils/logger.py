"""Structured logging: colourless console output + a rotating file log."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(funcName)s:%(lineno)d | %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO", date_folders: bool = True) -> None:
    """Configure root logging once per process.

    With `date_folders`, logs land in logs/<YYYY-MM-DD>/automation.log so each
    day's run history stays separate.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if date_folders:
        log_dir = log_dir / datetime.now().strftime("%Y-%m-%d")
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "automation.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    root.addHandler(file_handler)

    # Playwright is extremely chatty at DEBUG level.
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
