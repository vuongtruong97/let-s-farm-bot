from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from app.config import AppConfig

_CONFIGURED = False


class ActionFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        component = getattr(record, "component", record.name.split(".")[-1].upper())
        return f"{stamp} [{component}] {record.getMessage()}"


def setup_logging(config: AppConfig | None = None) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("farmbot")
    if _CONFIGURED:
        return logger
    logger.setLevel(logging.DEBUG)
    formatter = ActionFormatter()

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    cfg = config or AppConfig()
    log_path = cfg.log_dir() / "bot.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
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
