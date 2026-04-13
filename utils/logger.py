"""
Structured logger with file rotation.
"""
from __future__ import annotations
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config.settings import LOG_LEVEL, LOG_DIR

_loggers: dict[str, logging.Logger] = {}

FMT = "%(asctime)s | %(levelname)-8s | %(name)-35s | %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


class SafeRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            # On Windows, if file is opened by another program, just keep writing
            pass

def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    if not logger.handlers:
        formatter = logging.Formatter(FMT, datefmt=DATE_FMT)

        # Console
        sys.stdout.reconfigure(encoding='utf-8')
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # File (rotating, safe for Windows)
        log_file = LOG_DIR / "trading_bot.log"
        fh = SafeRotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    logger.propagate = False
    _loggers[name] = logger
    return logger
