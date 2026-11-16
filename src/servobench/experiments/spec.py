"""Experiment definitions: hold, step and stepped sine.

A spec says what to command, in SI units, as a sequence of segments timed from
the start of the experiment. It knows nothing about the actuator and sends
nothing: the only way to run one is ``servobench.experiments.runner``.

A spec is checked twice before anything runs:

- When it is created, that it is well formed: finite numbers, positive
  durations, at least one amplitude or frequency. A malformed spec raises
  ``SpecError``.
- Against the bench limits, by ``check(limits)``: every commanded position stays
  inside the travel range less ``command_margin``, the experiment fits inside
  ``experiment_max``, and a sine's commanded speed stays under ``speed_max``.
  The runner refuses a spec that has any problem, before it contacts the
  actuator.

What a spec cannot check is how the actuator responds; for example a step whose
commanded positions are all in range may still overshoot or move too fast.
That is what the runner's rehearsal and safety monitor are for.

The runner adds one segment in front of every spec: an ``approach`` ramp from
wherever the actuator is to the spec's first position, at the bench file's
``approach_speed``, or slower so that it lasts at least as long as the runner
needs every segment to last (see ``build_plan``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any, ClassVar

from servobench.limits import Limits
from servobench.units import Dimension, format_quantity


class SpecError(ValueError):
    """An experiment spec is malformed."""


# Segment kinds.
APPROACH = "approach"  # ramp from where the actuator is to where the experiment starts
SETTLE = "settle"  # hold the first position before the experiment proper
HOLD = "hold"  # hold a position
STEP = "step"  # jump to a position and hold it
SINE = "sine"  # a sine around a centre position
SEGMENT_KINDS = (APPROACH, SETTLE, HOLD, STEP, SINE)


@dataclass(frozen=True)
class Segment:
    """One stretch of an experiment with a single kind of command.

    ``position_rad`` is the setpoint held (settle, hold, step), the end of the
    ramp (approach), or the centre (sine).
    """

    kind: str
    label: str
    duration_s: float
    position_rad: float
    # approach: where the ramp starts; step: the setpoint before the step.
    from_position_rad: float | None = None
    amplitude_rad: float | None = None  # sine
    frequency_hz: float | None = None  # sine
    cycles: int | None = None  # sine: whole cycles in the segment
    transient_cycles: int | None = None  # sine: leading cycles the analysis should discard

    @property
    def constant(self) -> bool:
        """Whether the setpoint is the same throughout, so one command is enough."""
        return self.kind in (SETTLE, HOLD, STEP)

    def setpoint_rad(self, tau_s: float) -> float:
        """The commanded position ``tau_s`` seconds after the segment starts."""
        if self.kind == APPROACH:
            share = min(max(tau_s / self.duration_s, 0.0), 1.0) if self.duration_s > 0 else 1.0
            return self.from_position_rad + share * (self.position_rad - self.from_position_rad)
        if self.kind == SINE:
            phase = 2.0 * math.pi * self.frequency_hz * min(max(tau_s, 0.0), self.duration_s)
            return self.position_rad + self.amplitude_rad * math.sin(phase)
        return self.position_rad

    @property
    def extremes_rad(self) -> tuple[float, float]:
        """The lowest and highest position this segment commands."""
        if self.kind == SINE:
            return (self.position_rad - self.amplitude_rad, self.position_rad + self.amplitude_rad)
        if self.kind == APPROACH:
            ends = (self.from_position_rad, self.position_rad)
            return (min(ends), max(ends))
        return (self.position_rad, self.position_rad)

    def to_dict(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Segment:
        return cls(**{item.name: data.get(item.name) for item in fields(cls)})


class ExperimentSpec:
    """What every spec provides. The concrete specs are ``Hold``, ``Step`` and ``SteppedSine``."""

    kind: ClassVar[str]
    sample_rate_hz: float | None  # None: as fast as the adapter reliably allows

    def segments(self) -> tuple[Segment, ...]:
        raise NotImplementedError

    @property
    def planned_duration_s(self) -> float:
        """The length of the experiment, not counting the approach the runner adds."""
        return math.fsum(segment.duration_s for segment in self.segments())

    @property
    def start_position_rad(self) -> float:
        return self.segments()[0].position_rad

    @property
    def commanded_range_rad(self) -> tuple[float, float]:
        """The lowest and highest position the experiment commands."""
        extremes = [segment.extremes_rad for segment in self.segments()]
        return (min(low for low, _ in extremes), max(high for _, high in extremes))

    def check(self, limits: Limits) -> list[str]:
        """Every way this spec breaks the bench limits; empty if it is inside them."""
        problems: list[str] = []
        low_rad, high_rad = self.commanded_range_rad
        band_low_rad, band_high_rad = command_band_rad(limits)
        if low_rad < band_low_rad or high_rad > band_high_rad:
            problems.append(
                f"{self.kind}: commands positions from {_deg(low_rad)} to {_deg(high_rad)},"
                f" outside the allowed command range {_deg(band_low_rad)} to"
                f" {_deg(band_high_rad)} (the travel range {_deg(limits.position_min_rad)} to"
                f" {_deg(limits.position_max_rad)} less command_margin"
                f" {_deg(limits.command_margin_rad)})"
            )
        if self.planned_duration_s > limits.experiment_max_s:
            problems.append(
                f"{self.kind}: lasts {_s(self.planned_duration_s)}, longer than experiment_max"
                f" {_s(limits.experiment_max_s)}"
            )
        return problems

    def to_dict(self) -> dict[str, Any]:
        """The spec as plain data, SI values under unit-suffixed names."""
        data: dict[str, Any] = {"type": self.kind}
        for item in fields(self):  # type: ignore[arg-type]
            value = getattr(self, item.name)
            data[item.name] = list(value) if isinstance(value, tuple) else value
        return data


def command_band_rad(limits: Limits) -> tuple[float, float]:
    """The positions that may be commanded: the travel range less the command margin."""
    return (
        limits.position_min_rad + limits.command_margin_rad,
        limits.position_max_rad - limits.command_margin_rad,
    )


@dataclass(frozen=True)
class Hold(ExperimentSpec):
    """Hold one position for a while."""

    kind: ClassVar[str] = "hold"
    position_rad: float
    duration_s: float
    sample_rate_hz: float | None = None

    def __post_init__(self) -> None:
        _set(self, "position_rad", _finite(self.position_rad, "position_rad"))
        _set(self, "duration_s", _positive(self.duration_s, "duration_s"))
        _set(self, "sample_rate_hz", _rate(self.sample_rate_hz))

    def segments(self) -> tuple[Segment, ...]:
        return (
            Segment(HOLD, f"hold {_deg(self.position_rad)}", self.duration_s, self.position_rad),
        )


@dataclass(frozen=True)
class Step(ExperimentSpec):
    """Steps of one or more amplitudes around a centre.

    The actuator settles at the centre for ``settle_s``; then, for each
    amplitude in turn, it steps to ``centre + amplitude``, holds for ``dwell_s``,
    steps back to the centre and holds for ``dwell_s`` again. Amplitudes are
    signed: a negative one steps below the centre.
    """

    kind: ClassVar[str] = "step"
    centre_rad: float
    amplitudes_rad: tuple[float, ...]
    dwell_s: float
    settle_s: float
    sample_rate_hz: float | None = None

    def __post_init__(self) -> None:
        _set(self, "centre_rad", _finite(self.centre_rad, "centre_rad"))
        amplitudes = _sequence(self.amplitudes_rad, "amplitudes_rad")
        if any(amplitude == 0 for amplitude in amplitudes):
            raise SpecError("amplitudes_rad: a step of zero is not a step")
        _set(self, "amplitudes_rad", amplitudes)
        _set(self, "dwell_s", _positive(self.dwell_s, "dwell_s"))
        _set(self, "settle_s", _positive(self.settle_s, "settle_s"))
        _set(self, "sample_rate_hz", _rate(self.sample_rate_hz))

    def segments(self) -> tuple[Segment, ...]:
        centre = self.centre_rad
        out = [Segment(SETTLE, f"settle at {_deg(centre)}", self.settle_s, centre)]
        for number, amplitude in enumerate(self.amplitudes_rad, start=1):
            target = centre + amplitude
            out.append(
                Segment(
                    STEP,
                    f"step {number}: {_deg(centre)} to {_deg(target)}",
                    self.dwell_s,
                    target,
                    from_position_rad=centre,
                )
            )
            out.append(
                Segment(
                    STEP,
                    f"step {number} back: {_deg(target)} to {_deg(centre)}",
                    self.dwell_s,
                    centre,
                    from_position_rad=target,
                )
            )
        return tuple(out)


@dataclass(frozen=True)
class SteppedSine(ExperimentSpec):
    """A sine of one amplitude around a centre, at each frequency in turn.

    The actuator settles at the centre for ``settle_s``; then each frequency
    runs for ``cycles`` whole cycles, starting and ending at the centre. The
    first ``transient_cycles`` of each are the start-up transient, for the
    analysis to discard.
    """

    kind: ClassVar[str] = "stepped_sine"
    centre_rad: float
    amplitude_rad: float
    frequencies_hz: tuple[float, ...]
    cycles: int
    transient_cycles: int
    settle_s: float
    sample_rate_hz: float | None = None

    def __post_init__(self) -> None:
        _set(self, "centre_rad", _finite(self.centre_rad, "centre_rad"))
        _set(self, "amplitude_rad", _positive(self.amplitude_rad, "amplitude_rad"))
        frequencies = _sequence(self.frequencies_hz, "frequencies_hz")
        if any(frequency <= 0 for frequency in frequencies):
            raise SpecError("frequencies_hz: every frequency must be above zero")
        _set(self, "frequencies_hz", frequencies)
        _set(self, "cycles", _count(self.cycles, "cycles", minimum=1))
        _set(self, "transient_cycles", _count(self.transient_cycles, "transient_cycles", 0))
        if self.transient_cycles >= self.cycles:
            raise SpecError("transient_cycles must be fewer than cycles, or nothing is left")
        _set(self, "settle_s", _positive(self.settle_s, "settle_s"))
        _set(self, "sample_rate_hz", _rate(self.sample_rate_hz))

    @property
    def peak_command_speed_rad_s(self) -> float:
        """The fastest the commanded position moves: amplitude times 2 pi f at the top frequency."""
        return self.amplitude_rad * 2.0 * math.pi * max(self.frequencies_hz)

    def segments(self) -> tuple[Segment, ...]:
        centre = self.centre_rad
        out = [Segment(SETTLE, f"settle at {_deg(centre)}", self.settle_s, centre)]
        for frequency in self.frequencies_hz:
            out.append(
                Segment(
                    SINE,
                    f"sine {_hz(frequency)}, {_deg(self.amplitude_rad)} around {_deg(centre)}",
                    self.cycles / frequency,
                    centre,
                    amplitude_rad=self.amplitude_rad,
                    frequency_hz=frequency,
                    cycles=self.cycles,
                    transient_cycles=self.transient_cycles,
                )
            )
        return tuple(out)

    def check(self, limits: Limits) -> list[str]:
        problems = super().check(limits)
        if self.peak_command_speed_rad_s > limits.speed_max_rad_s:
            problems.append(
                f"{self.kind}: the commanded position moves at up to"
                f" {_deg_s(self.peak_command_speed_rad_s)} at {_hz(max(self.frequencies_hz))},"
                f" faster than speed_max {_deg_s(limits.speed_max_rad_s)}"
            )
        return problems


SPEC_TYPES: dict[str, type[ExperimentSpec]] = {
    spec_type.kind: spec_type for spec_type in (Hold, Step, SteppedSine)
}


def spec_from_dict(data: dict[str, Any]) -> ExperimentSpec:
    """The spec that ``ExperimentSpec.to_dict`` wrote."""
    spec_type = SPEC_TYPES.get(data.get("type"))
    if spec_type is None:
        raise SpecError(f"unknown experiment type {data.get('type')!r}; known: {list(SPEC_TYPES)}")
    values = {key: value for key, value in data.items() if key != "type"}
    return spec_type(**values)


def build_plan(
    spec: ExperimentSpec,
    start_position_rad: float,
    approach_speed_rad_s: float,
    *,
    min_duration_s: float = 0.0,
) -> tuple[Segment, ...]:
    """The spec's segments, with the approach ramp from ``start_position_rad`` in front.

    The ramp moves at ``approach_speed_rad_s``, or slower if that would make it
    shorter than ``min_duration_s``: a ramp of a fraction of a degree would
    otherwise last less than one sampling period, and be passed over.
    """
    if not (math.isfinite(approach_speed_rad_s) and approach_speed_rad_s > 0):
        raise SpecError("approach_speed_rad_s must be above zero")
    segments = spec.segments()
    distance_rad = abs(spec.start_position_rad - start_position_rad)
    if distance_rad == 0:
        return segments
    approach = Segment(
        APPROACH,
        f"approach {_deg(start_position_rad)} to {_deg(spec.start_position_rad)}",
        max(distance_rad / approach_speed_rad_s, min_duration_s),
        spec.start_position_rad,
        from_position_rad=start_position_rad,
    )
    return (approach, *segments)


# -- validation helpers -------------------------------------------------------------


def _set(spec: object, name: str, value: object) -> None:
    object.__setattr__(spec, name, value)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SpecError(f"{name} must be a finite number in SI units, got {value!r}")
    return float(value)


def _positive(value: object, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise SpecError(f"{name} must be above zero, got {value!r}")
    return number


def _rate(value: object) -> float | None:
    return None if value is None else _positive(value, "sample_rate_hz")


def _count(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SpecError(f"{name} must be a whole number of {minimum} or more, got {value!r}")
    return value


def _sequence(values: Iterable[object], name: str) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise SpecError(f"{name} must be a list of numbers in SI units, got {values!r}")
    numbers = tuple(_finite(value, name) for value in values)
    if not numbers:
        raise SpecError(f"{name} must have at least one value")
    return numbers


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=4)


def _deg_s(value_rad_s: float) -> str:
    return format_quantity(value_rad_s, Dimension.ANGULAR_VELOCITY, "deg/s", digits=4)


def _hz(value_hz: float) -> str:
    return format_quantity(value_hz, Dimension.FREQUENCY, "Hz", digits=4)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=4)
