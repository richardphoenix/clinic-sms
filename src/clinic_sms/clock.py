from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

CLINIC_TZ = ZoneInfo("America/Los_Angeles")


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(CLINIC_TZ)


class SimClock:
    """Manually advanced clock for tests and simulations."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be timezone-aware")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta
