from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SENSITIVE_PATTERNS = (
    re.compile(r"\bpassword\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bmnemonic\s*[:=]\s*.+", re.IGNORECASE),
    re.compile(r"\bseed\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bprivate[_\s]?key\b", re.IGNORECASE),
)


class SensitiveDataFilter(logging.Filter):
    """Redact sensitive patterns from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            for pattern in _SENSITIVE_PATTERNS:
                msg = pattern.sub("[REDACTED]", msg)
            record.msg = msg
        if record.args:
            record.args = tuple(
                self._redact(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True

    @staticmethod
    def _redact(value: str) -> str:
        for pattern in _SENSITIVE_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, level, logging.INFO)

    # Root: warnings only — keeps docker/console clean
    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    # Our application logs at the configured level
    app_logger = logging.getLogger("sn_reg_bot")
    app_logger.setLevel(log_level)

    # Silence noisy third-party loggers
    for name in (
        "httpx",
        "httpcore",
        "telegram",
        "telegram.ext",
        "apscheduler",
        "urllib3",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sensitive_filter = SensitiveDataFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(sensitive_filter)
    console.setLevel(log_level)
    app_logger.addHandler(console)
    app_logger.propagate = False

    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(sensitive_filter)
    file_handler.setLevel(log_level)
    app_logger.addHandler(file_handler)
