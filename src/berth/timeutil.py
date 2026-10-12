"""Small helpers for clock times and time-of-day profiles."""

from __future__ import annotations

import re
from datetime import datetime

CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
MINUTES_PER_DAY = 1440

TIME_HELP = "Use HH:MM (like 18:30) or a full date and time (like 2026-03-02T18:30)"


def parse_clock(text: str, allow_24: bool = False) -> int:
    """Parse "HH:MM" and return minutes after midnight."""
    match = CLOCK_RE.match(str(text))
    if not match:
        raise ValueError(f"'{text}' is not a valid clock time. Use HH:MM, like 07:30")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if minutes > 59 or hours > 24 or (hours == 24 and (minutes != 0 or not allow_24)):
        raise ValueError(f"'{text}' is not a valid clock time. Use HH:MM, like 07:30")
    return hours * 60 + minutes


def parse_time(text: str) -> int | datetime:
    """Parse a vehicle time.

    Returns minutes after midnight (int) for "HH:MM", or a datetime for a full
    date and time. A UTC offset (like +02:00 or Z) is kept; build_problem
    converts all times to one clock.
    """
    text = str(text).strip()
    if CLOCK_RE.match(text):
        return parse_clock(text)
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a valid time. {TIME_HELP}") from None
    return value


def check_time(text: str) -> str:
    """Pydantic helper: check that a time string can be parsed."""
    parse_time(text)
    return str(text).strip()


def clock_label(minute_of_day: int) -> str:
    minute_of_day %= MINUTES_PER_DAY
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"
