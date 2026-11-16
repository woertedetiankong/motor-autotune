"""Clocks. Every adapter owns one; the runner reads and waits only through it.

Times are seconds on a monotonic scale with an arbitrary origin: only
differences between two readings of the same clock mean anything. A time from
one clock must never be compared with a time from another.

``MonotonicClock`` is for real hardware. ``VirtualClock`` is for the simulated
servo: its time moves only when someone waits on it or a simulated bus
transaction takes time, so a simulated experiment runs as fast as the computer
allows and gives the same result every time. This module is the only place in
the library allowed to call ``time.sleep`` or read the computer's clock.
"""

from __future__ import annotations

import datetime
import math
import time
from abc import ABC, abstractmethod


class Clock(ABC):
    """A monotonic time source that can also be waited on."""

    @abstractmethod
    def now_s(self) -> float:
        """The current time in seconds. Never decreases."""

    @abstractmethod
    def wait_until_s(self, t_s: float) -> None:
        """Return once ``now_s()`` is at least ``t_s``. A time in the past returns at once."""

    def wait_s(self, duration_s: float) -> None:
        """Wait for ``duration_s`` seconds (zero or more)."""
        if not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError(f"wait duration must be zero or more seconds, got {duration_s!r} s")
        self.wait_until_s(self.now_s() + duration_s)


class MonotonicClock(Clock):
    """Real time from the operating system's monotonic clock, for real hardware."""

    def now_s(self) -> float:
        return time.monotonic()

    def wait_until_s(self, t_s: float) -> None:
        _check_time(t_s)
        while True:
            remaining_s = t_s - time.monotonic()
            if remaining_s <= 0:
                return
            time.sleep(remaining_s)


def utc_now() -> datetime.datetime:
    """The computer's calendar date and time, in UTC.

    Only for naming and dating run records, which people read. Never for timing
    an experiment: that is always the adapter's clock.
    """
    return datetime.datetime.now(datetime.UTC)


class VirtualClock(Clock):
    """Simulated time. Starts at ``start_s`` and moves only when waited on."""

    def __init__(self, start_s: float = 0.0) -> None:
        _check_time(start_s)
        self._now_s = float(start_s)

    def now_s(self) -> float:
        return self._now_s

    def wait_until_s(self, t_s: float) -> None:
        _check_time(t_s)
        if t_s > self._now_s:
            self._now_s = float(t_s)


def _check_time(t_s: float) -> None:
    if not math.isfinite(t_s):
        raise ValueError(f"time must be a finite number of seconds, got {t_s!r} s")
