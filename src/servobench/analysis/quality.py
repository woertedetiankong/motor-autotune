"""How good a trace's samples are: timing actually achieved, reading resolution.

Everything here works from the timestamps the adapter recorded. Nothing assumes
the samples are evenly spaced: on a real servo polled over a serial bus they
are not, and on the simulated one a command transaction, a fault poll or a
missed reply leaves a longer gap.

Definitions, used by every analysis:

- ``sample_rate_hz``: samples per second over a stretch of samples, ``(n - 1)``
  intervals divided by the time from the first to the last.
- ``interval_median_s`` and ``interval_max_s``: the median and the longest
  time between consecutive samples. The longest shows gaps: a missed reply, a
  fault poll, a command between two reads.
- ``jitter_s``: the root-mean-square deviation of the intervals from their
  mean (period jitter). It says how uneven the spacing is; it cannot say how
  far each timestamp is from the instant the device really sampled, which the
  samples do not show.
- ``position_resolution_rad``: the smallest difference between two distinct
  position readings in the trace. For a quantised reading this is its step,
  for example 2 pi / 4096 rad for a 4096-step encoder; for an unquantised one
  it is only an upper bound.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from servobench.contract import POSITION
from servobench.experiments.runner import Trace

# Two readings closer than this are the same reading written with rounding error.
_SAME_READING_RAD = 1e-12


@dataclass(frozen=True)
class Timing:
    """How a stretch of samples was actually spaced in time. None when there are too few."""

    samples: int
    duration_s: float | None  # first to last sample
    sample_rate_hz: float | None  # (samples - 1) / duration_s
    interval_median_s: float | None
    interval_max_s: float | None
    jitter_s: float | None  # RMS deviation of the intervals from their mean


def timing_of(times_s: Sequence[float] | np.ndarray) -> Timing:
    """The timing of samples taken at ``times_s`` (in order, on one clock)."""
    times = np.asarray(times_s, dtype=float)
    if times.size < 2:
        return Timing(int(times.size), None, None, None, None, None)
    intervals = np.diff(times)
    duration_s = float(times[-1] - times[0])
    return Timing(
        samples=int(times.size),
        duration_s=duration_s,
        sample_rate_hz=(times.size - 1) / duration_s if duration_s > 0 else None,
        interval_median_s=float(np.median(intervals)),
        interval_max_s=float(intervals.max()),
        jitter_s=float(np.sqrt(np.mean((intervals - intervals.mean()) ** 2))),
    )


@dataclass(frozen=True)
class TraceQuality:
    """What every analysis reports about the trace it worked from."""

    simulated: bool
    completed: bool  # the plan ran to the end
    stop: str | None  # why the run stopped early: "<kind>: <message>"
    samples: int  # position readings during the plan
    missed_reads: int  # reads during the run that got no reply
    timing: Timing  # over every position reading during the plan
    position_resolution_rad: float | None
    # The reading just before enable. With torque off and a pendulum load
    # hanging free, this is where the arm hangs (within its friction).
    position_before_enable_rad: float | None


def positions(trace: Trace) -> tuple[np.ndarray, np.ndarray]:
    """(times_s, positions_rad) of the position readings taken during the plan."""
    start_s = trace.t_start_s
    pairs = [
        (sample.t_s, sample.values[POSITION])
        for sample in trace.samples
        if POSITION in sample.values and start_s is not None and sample.t_s >= start_s
    ]
    if not pairs:
        return np.empty(0), np.empty(0)
    times, values = (np.asarray(column, dtype=float) for column in zip(*pairs, strict=True))
    return times, values


def position_resolution_rad(trace: Trace) -> float | None:
    """The smallest difference between two distinct position readings, or None."""
    values = np.unique(
        [sample.values[POSITION] for sample in trace.samples if POSITION in sample.values]
    )
    gaps = np.diff(values)
    gaps = gaps[gaps > _SAME_READING_RAD]
    return float(gaps.min()) if gaps.size else None


def trace_quality(trace: Trace) -> TraceQuality:
    times, _ = positions(trace)
    stop = None if trace.stop is None else f"{trace.stop.kind}: {trace.stop.message}"
    before = None
    if trace.samples and trace.t_enable_s is not None:
        first = trace.samples[0]
        if first.t_s <= trace.t_enable_s and POSITION in first.values:
            before = first.values[POSITION]
    missed = sum(
        1 for read in trace.missed if trace.t_start_s is not None and read.t_s >= trace.t_start_s
    )
    return TraceQuality(
        simulated=trace.simulated,
        completed=trace.stop is None and trace.t_start_s is not None,
        stop=stop,
        samples=int(times.size),
        missed_reads=missed,
        timing=timing_of(times),
        position_resolution_rad=position_resolution_rad(trace),
        position_before_enable_rad=before,
    )


def check_reasons(result: object, names: Sequence[str]) -> None:
    """Every metric in ``names`` that is None has a reason in ``result.reasons``, and no other."""
    reasons = result.reasons
    for name in names:
        value = getattr(result, name)
        if value is None and not reasons.get(name):
            raise ValueError(f"{type(result).__name__}.{name} is missing and has no reason")
        if value is not None and name in reasons:
            raise ValueError(f"{type(result).__name__}.{name} has a value and a reason")
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{type(result).__name__}.{name} is not finite: {value!r}")
    unknown = set(reasons) - set(names)
    if unknown:
        raise ValueError(f"reasons for unknown metrics: {sorted(unknown)}")
