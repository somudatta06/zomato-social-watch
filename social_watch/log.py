"""Logging: stderr + rotated file via loguru."""
from __future__ import annotations

import sys

from loguru import logger

from .config import LOG_DIR


def setup_logging(level: str = "INFO") -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}</cyan> - <level>{message}</level>"
        ),
    )
    logger.add(
        LOG_DIR / "social_watch.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} - {message}",
    )
