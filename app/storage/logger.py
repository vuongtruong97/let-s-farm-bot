from __future__ import annotations

import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from app.config import AppConfig

_CONFIGURED = False
# Every ADB call is logged at DEBUG, so an unrotated bot.log reached 100MB+.
LOG_MAX_BYTES = 5_000_000
LOG_BACKUPS = 3
ROLLOVER_RETRY_S = 60.0


class ActionFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        component = getattr(record, "component", record.name.split(".")[-1].upper())
        return f"{stamp} [{component}] {record.getMessage()}"


def setup_logging(config: AppConfig | None = None, console: bool = True) -> logging.Logger:
    """console=False for the tray app: pythonw has no console to write to."""
    global _CONFIGURED
    logger = logging.getLogger("farmbot")
    if _CONFIGURED:
        return logger
    logger.setLevel(logging.DEBUG)
    formatter = ActionFormatter()

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.INFO)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    cfg = config or AppConfig()
    log_path = cfg.log_dir() / "bot.log"
    file_handler = _RotatingHandler(
        log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _CONFIGURED = True
    return logger


def get_logger(component: str = "APP") -> logging.Logger:
    logger = logging.getLogger("farmbot")
    if not logger.handlers:
        setup_logging()
    return logging.LoggerAdapter(logger, {"component": component.upper()})


class _RotatingHandler(RotatingFileHandler):
    """Rotation that survives Windows refusing the rename.

    The web UI and a CLI command can hold bot.log at the same time; renaming an
    open file fails there. Keep appending and try again a minute later instead
    of failing (and printing a traceback) on every record.
    """

    _retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if time.monotonic() < self._retry_at:
            return 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + ROLLOVER_RETRY_S
            if self.stream is None:
                self.stream = self._open()
