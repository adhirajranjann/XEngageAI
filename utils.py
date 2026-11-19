"""Logging, sleeping and clock helpers shared by every module."""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import datetime, timezone


log = logging.getLogger("xengage")


def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji-safe on Windows
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("xengage.log", encoding="utf-8"),
        ],
    )


def pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
