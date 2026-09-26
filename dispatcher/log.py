"""Logging setup shared across the dispatcher."""

from __future__ import annotations

import logging
import sys


def setup(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
