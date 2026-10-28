from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Event:
    kind: str
    message: str
    data: dict = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def line(self) -> str:
        return f"{self.at.astimezone():%H:%M:%S}  [{self.kind.upper():5}] {self.message}"


class Events:
    def __init__(
        self, callback: Callable[[Event], None] | None = None, logger: logging.Logger | None = None
    ):
        self.callback = callback
        self.logger = logger

    def emit(self, kind: str, message: str, **data) -> None:
        event = Event(kind, message, data)
        if self.logger and kind not in ("bytes", "progress", "schedule"):
            self.logger.info(event.line())
        if self.callback:
            self.callback(event)


def log_setup(data_dir: Path) -> logging.Logger:
    directory = data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"remarkable_mirror.{data_dir}")
    if not logger.handlers:
        handler = RotatingFileHandler(directory / "mirror.log", maxBytes=2_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
