"""Gain and phase from a stepped sine, from the samples and command times a run recorded.

``analyse_stepped_sine(trace, spec)`` measures each frequency of a
``SteppedSine`` experiment from the runner's ``Trace`` (live, or loaded back
with ``records.load_run``). It needs no adapter, and works from the recorded
timestamps of every command and reading; nothing assumes even spacing.

What is measured. The servo's response to the command it was actually given.
The runner sends a new position command once per loop, and the servo holds each
one until the next arrives, so what it follows is a staircase, not a smooth
sine. At 100 commands a second that staircase lags the ideal sine by about half
a command interval (9 deg at 5 Hz). So the input is the held command, rebuilt
from the recorded command times and values, and its fundamental is computed
exactly by integrating each held stretch. The servo's own delay, from the
adapter sending a command to it acting, is part of the measured response.

Definitions. For one frequency f, with omega = 2 pi f:

- Analysed window: from the segment's start plus ``transient_cycles`` whole
  cycles (the start-up transient, discarded) to the segment's end, so it holds
  ``cycles - transient_cycles`` whole cycles.
- Command fundamental C: (2 / T) times the integral over the window of the held
  command times exp(-j omega t), T the window's length; the command is then
  Re(C exp(j omega t)) plus its mean.
- Response fundamental Y: a least-squares fit, at the recorded timestamps, of
  a + b (t - t_mid) + Re(Y exp(j omega t)) to the readings in the window. The
  straight line takes up any slow drift, so it does not leak into the sine.
- ``gain`` = |Y| / |C|, ``gain_db`` = 20 log10(gain), ``phase_rad`` = arg(Y / C):
  negative when the response lags. The lowest measured frequency's phase is
  given in (-pi, pi]; each higher one is unwrapped to the branch nearest the
  one below it, so a lag past -pi is reported as such.
- ``uncertainties``: twice the standard error of ``gain_db`` and ``phase_rad``
  from the fit's residuals (about 95 % if the residuals were independent
  noise). Without noise the residuals are the reading's quantisation, so this
  is a measure of the resolution, not a statistical bound. It leaves out
  timestamp error and any distortion that aliases onto f; on the simulated
  servo the error against the model was up to 1.7 times it, and under 0.2 dB
  and 1.3 deg.
- ``response_offset_rad``: the fitted level a, the readings' level at the
  middle of the window with the sine taken out, minus the commanded centre.
  Under a gravity load a servo sits off its command, so a non-zero offset is
  expected; its size says how far.

When a point is not measured. See ``MIN_SAMPLES_PER_CYCLE``, ``MIN_CYCLES``,
``MIN_RESPONSE_STEPS`` and ``MAX_PHASE_UNCERTAINTY_RAD`` below, and each
point's ``reasons``.

Direction. A position servo lags its command at every frequency, so its phase
is negative. One that turns the wrong way still follows a small sine closely
enough to stay inside the safety monitor's follow check, but its phase is
about 180 deg minus the healthy lag: a large lead. The result's ``warnings``
say so when the lowest frequency measured leads by more than
``LEAD_WARNING_RAD``.

Zero. A servo whose reading is offset from where the arm hangs is internally
consistent: it follows its commands as a healthy one does, and its gain and
phase are ordinary. What shows the offset is ``response_offset_rad`` (gravity
pulls the arm towards where it really hangs) and the trace quality's
``position_before_enable_rad``. Whether they are wrong depends on the bench's
zero and load, which this analysis does not know; ``check`` judges that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from servobench.analysis.quality import (
    Timing,
    TraceQuality,
    check_reasons,
    positions,
    timing_of,
    trace_quality,
)
from servobench.experiments.runner import Trace
from servobench.experiments.spec import APPROACH, SINE, SteppedSine
from servobench.units import Dimension, format_quantity

# Readings per cycle below which a point is not measured. The runner reads and
# commands in turn on one bus, so the response carries the command staircase's
# images at multiples of the command rate, and with readings at that same rate
# they alias straight back onto f. With fewer than 8 readings (and commands) a
# cycle the staircase is eight or fewer levels, its first image lies at most
# 7 f away, and a servo that does not attenuate it much leaves an error that the
# fit cannot see. On the simulated servo the error stays inside 0.1 dB and
# 1 deg at 8 or more readings per cycle (see the tests).
MIN_SAMPLES_PER_CYCLE = 8.0
# Whole cycles left after the transient. With one cycle there is nothing to
# compare it with: a transient that has not died away looks like part of the sine.
MIN_CYCLES = 2
# Response amplitude, in reading steps, below which a point is not measured:
# a sine a few reading steps high is mostly quantisation.
MIN_RESPONSE_STEPS = 3.0
# A point whose phase uncertainty (twice its standard error) is larger than this
# is not measured: 5 deg, so a measured phase is good to at least that.
MAX_PHASE_UNCERTAINTY_RAD = math.radians(5.0)
# The two halves of the analysed window, fitted separately, may differ by this
# much before the point gets a warning that the response was still changing.
HALVES_WARNING_DB = 0.5
HALVES_WARNING_RAD = math.radians(3.0)
# A position servo lags its command at every frequency. A lead larger than this at
# the lowest frequency measured is flagged: the response moves against the command.
LEAD_WARNING_RAD = math.radians(45.0)

MEASURED = (
    "analysed_from_s",
    "analysed_to_s",
    "cycles_analysed",
    "samples_per_cycle",
    "commands_per_cycle",
    "command_amplitude_rad",
    "response_amplitude_rad",
    "response_offset_rad",
    "gain",
    "gain_db",
    "phase_rad",
    "residual_rms_rad",
)
UNCERTAIN = ("gain_db", "phase_rad")

DEFINITIONS = {
    "analysed_from_s": "start of the analysed window, after the transient cycles (adapter clock)",
    "analysed_to_s": "end of the analysed window, the end of the segment (adapter clock)",
    "cycles_analysed": "whole cycles in the analysed window",
    "samples_per_cycle": "position readings in the window per cycle",
    "commands_per_cycle": "commands sent in the window per cycle",
    "command_amplitude_rad": "amplitude of the fundamental of the held (staircase) command",
    "response_amplitude_rad": "amplitude of the least-squares sine fitted to the readings",
    "response_offset_rad": "level of the readings at the middle of the window, sine removed,"
    " minus the commanded centre",
    "gain": "response_amplitude_rad / command_amplitude_rad",
    "gain_db": "20 log10(gain)",
    "phase_rad": "phase of the response's fundamental minus the command's; negative is a lag."
    " Lowest measured frequency in (-pi, pi], higher ones unwrapped from it",
    "residual_rms_rad": "RMS of the readings about the fitted sine and line",
    "uncertainties": "twice the standard error of gain_db and phase_rad from the fit residuals",
}


@dataclass(frozen=True)
class FrequencyPoint:
    """Gain and phase at one frequency. A value that could not be measured is None,
    with the reason in ``reasons`` under its name."""

    segment: int  # index into the trace's segments
    label: str
    frequency_hz: float
    centre_rad: float
    commanded_amplitude_rad: float
    analysed_from_s: float | None
    analysed_to_s: float | None
    cycles_analysed: int | None
    samples_per_cycle: float | None
    commands_per_cycle: float | None
    command_amplitude_rad: float | None
    response_amplitude_rad: float | None
    response_offset_rad: float | None
    gain: float | None
    gain_db: float | None
    phase_rad: float | None
    residual_rms_rad: float | None
    timing: Timing  # of the readings in the analysed window
    # gain_db and phase_rad -> twice their standard error, in their own unit
    uncertainties: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_reasons(self, MEASURED)
        for name in UNCERTAIN:
            if (getattr(self, name) is None) != (name not in self.uncertainties):
                raise ValueError(f"{name}: an uncertainty is given exactly when a value is")


@dataclass(frozen=True)
class FrequencyAnalysis:
    """Every frequency of one ``SteppedSine`` experiment, in the order they ran."""

    quality: TraceQuality
    points: tuple[FrequencyPoint, ...]
    warnings: tuple[str, ...]
    definitions: dict[str, str] = field(default_factory=lambda: dict(DEFINITIONS))


def analyse_stepped_sine(trace: Trace, spec: SteppedSine) -> FrequencyAnalysis:
    """Measure gain and phase at every frequency in ``trace``, the record of running ``spec``."""
    if not isinstance(spec, SteppedSine):
        raise TypeError(f"analyse_stepped_sine needs a SteppedSine spec, got {type(spec).__name__}")
    planned = tuple(segment for segment in trace.segments if segment.kind != APPROACH)
    if planned != spec.segments():
        raise ValueError("the trace's segments are not those of this spec; is it the right run?")
    quality = trace_quality(trace)
    times, values = positions(trace)
    fits = [
        _analyse_one(trace, index, times, values, quality)
        for index, segment in enumerate(trace.segments)
        if segment.kind == SINE
    ]
    _unwrap(fits)
    points = tuple(_point(fit) for fit in fits)
    return FrequencyAnalysis(quality=quality, points=points, warnings=_overall_warnings(points))


# -- one frequency ---------------------------------------------------------------------


@dataclass
class _Fit:
    """A point being built; ``phase_rad`` is unwrapped across points before freezing."""

    base: dict
    result: dict
    reasons: dict[str, str]
    warnings: list[str]
    uncertainties: dict[str, float]
    timing: Timing


def _analyse_one(
    trace: Trace, index: int, times: np.ndarray, values: np.ndarray, quality: TraceQuality
) -> _Fit:
    segment = trace.segments[index]
    frequency_hz = segment.frequency_hz
    fit = _Fit(
        base=dict(
            segment=index,
            label=segment.label,
            frequency_hz=frequency_hz,
            centre_rad=segment.position_rad,
            commanded_amplitude_rad=segment.amplitude_rad,
        ),
        result=dict.fromkeys(MEASURED),
        reasons={},
        warnings=[],
        uncertainties={},
        timing=timing_of(()),
    )

    def refuse(reason: str) -> _Fit:
        for name in MEASURED:
            if fit.result[name] is None:
                fit.reasons[name] = reason
        return fit

    if index >= len(trace.segment_start_s):
        return refuse(f"the run stopped before this frequency started{_stop_note(trace)}")
    period_s = 1.0 / frequency_hz
    start_s = trace.segment_start_s[index]
    from_s = start_s + segment.transient_cycles * period_s
    to_s = start_s + segment.cycles * period_s
    cycles = segment.cycles - segment.transient_cycles
    finished = index + 1 < len(trace.segment_start_s) or trace.stop is None
    if not finished:
        return refuse(
            f"the run stopped during this frequency{_stop_note(trace)}, before its analysed"
            f" window ({_s(from_s - start_s)} to {_s(to_s - start_s)} into it) was complete"
        )
    fit.result.update(analysed_from_s=from_s, analysed_to_s=to_s, cycles_analysed=cycles)

    in_window = (times >= from_s) & (times <= to_s)
    t, y = times[in_window], values[in_window]
    fit.timing = timing_of(t)
    fit.result["samples_per_cycle"] = t.size / cycles
    commands = [c for c in trace.commands if c.t_s <= to_s]
    held = [c for c in commands if c.t_s >= from_s]
    fit.result["commands_per_cycle"] = len(held) / cycles

    if cycles < MIN_CYCLES:
        return refuse(
            f"only {cycles} whole cycle is left after the {segment.transient_cycles}-cycle"
            f" transient; at least {MIN_CYCLES} are needed, so that a transient that has not"
            " died away cannot pass for part of the sine. Run more cycles"
        )
    rate = fit.timing.sample_rate_hz
    rate_note = "" if rate is None else f" (readings came at {_hz(rate)})"
    for name, count in (("readings", t.size), ("commands", len(held))):
        if count / cycles < MIN_SAMPLES_PER_CYCLE:
            return refuse(
                f"only {count / cycles:.1f} {name} per cycle at {_hz(frequency_hz)}{rate_note};"
                f" at least {MIN_SAMPLES_PER_CYCLE:g} are needed. Above about"
                f" {_hz(_highest_hz(rate))} this bus cannot measure a frequency response"
            )
    if not commands or commands[0].t_s > from_s:
        return refuse("no command was in force at the start of the analysed window")

    _, command_c = _held_fundamental(commands, from_s, to_s, frequency_hz)
    offset, response_y, residual_rms, covariance = _fit_sine(t, y, frequency_hz)
    fit.result.update(
        command_amplitude_rad=abs(command_c),
        response_amplitude_rad=abs(response_y),
        response_offset_rad=offset - segment.position_rad,
        residual_rms_rad=residual_rms,
    )
    resolution = quality.position_resolution_rad or 0.0
    if resolution and abs(response_y) < MIN_RESPONSE_STEPS * resolution:
        return refuse(
            f"the response is only {abs(response_y) / resolution:.1f} reading steps high"
            f" ({_deg(abs(response_y))} for a commanded {_deg(segment.amplitude_rad)});"
            f" at least {MIN_RESPONSE_STEPS:g} are needed, or quantisation dominates. Use a"
            " larger amplitude, if the limits allow"
        )
    # Standard error of each quadrature component, from the fit covariance.
    component_se = math.sqrt(0.5 * (covariance[2, 2] + covariance[3, 3]))
    phase_u = 2.0 * component_se / abs(response_y)
    if phase_u > MAX_PHASE_UNCERTAINTY_RAD:
        return refuse(
            f"the response ({_deg(abs(response_y))}) is too small against the scatter of the"
            f" readings about the fit ({_deg(residual_rms)} RMS): its phase is uncertain by"
            f" {math.degrees(phase_u):.1f} deg, more than"
            f" {math.degrees(MAX_PHASE_UNCERTAINTY_RAD):.0f} deg. Use a larger amplitude or"
            " more cycles"
        )

    ratio = response_y / command_c
    gain = abs(ratio)
    fit.result.update(
        gain=gain, gain_db=20.0 * math.log10(gain), phase_rad=math.atan2(ratio.imag, ratio.real)
    )
    fit.uncertainties.update(
        gain_db=20.0 / math.log(10.0) * 2.0 * component_se / abs(response_y), phase_rad=phase_u
    )

    # The two halves fitted separately: a response still settling differs between them.
    middle_s = from_s + (cycles // 2) * period_s
    halves = []
    for low_s, high_s in ((from_s, middle_s), (middle_s, to_s)):
        part = (t >= low_s) & (t <= high_s)
        _, part_c = _held_fundamental(commands, low_s, high_s, frequency_hz)
        _, part_y, _, _ = _fit_sine(t[part], y[part], frequency_hz)
        halves.append(part_y / part_c)
    change_db = abs(20.0 * math.log10(abs(halves[1]) / abs(halves[0])))
    change_rad = abs(np.angle(halves[1] / halves[0]))
    if change_db > HALVES_WARNING_DB or change_rad > HALVES_WARNING_RAD:
        fit.warnings.append(
            f"the first and second halves of the analysed window differ by {change_db:.2f} dB"
            f" and {math.degrees(change_rad):.1f} deg: the response may still have been"
            " settling, or the servo's behaviour changed during the sine. Discard more"
            " transient cycles or run more cycles"
        )
    return fit


def _held_fundamental(
    commands, from_s: float, to_s: float, frequency_hz: float
) -> tuple[float, complex]:
    """Mean and complex fundamental of the held command over [from_s, to_s].

    Each command is held from the time it was sent until the next one. The
    fundamental is (2 / T) * integral(c(t) exp(-j omega t) dt), integrated exactly.
    """
    omega = 2.0 * math.pi * frequency_hz
    sent = np.array([c.t_s for c in commands])
    value = np.array([c.value for c in commands])
    starts = np.clip(sent, from_s, to_s)
    ends = np.clip(np.append(sent[1:], to_s), from_s, to_s)
    length = to_s - from_s
    mean = float(np.sum(value * (ends - starts)) / length)
    integral = np.sum(value * (np.exp(-1j * omega * starts) - np.exp(-1j * omega * ends)))
    return mean, complex(2.0 / length * integral / (1j * omega))


def _fit_sine(
    t: np.ndarray, y: np.ndarray, frequency_hz: float
) -> tuple[float, complex, float, np.ndarray]:
    """Least squares y ~ a + b (t - t_mid) + p cos(wt) + q sin(wt).

    Returns the mean level a, the complex amplitude Y = p - jq (so the sine is
    Re(Y exp(jwt))), the residual RMS, and the parameters' covariance.
    """
    omega = 2.0 * math.pi * frequency_hz
    mid = 0.5 * (t[0] + t[-1])
    design = np.column_stack((np.ones_like(t), t - mid, np.cos(omega * t), np.sin(omega * t)))
    params, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ params
    dof = max(t.size - design.shape[1], 1)
    variance = float(residual @ residual) / dof
    covariance = variance * np.linalg.inv(design.T @ design)
    level = float(params[0])
    return level, complex(params[2], -params[3]), float(np.sqrt(np.mean(residual**2))), covariance


def _highest_hz(rate_hz: float | None) -> float:
    return math.nan if rate_hz is None else rate_hz / MIN_SAMPLES_PER_CYCLE


def _unwrap(fits: list[_Fit]) -> None:
    """Phase of the lowest measured frequency in (-pi, pi]; each higher one nearest the last."""
    measured = sorted(
        (fit for fit in fits if fit.result["phase_rad"] is not None),
        key=lambda fit: fit.base["frequency_hz"],
    )
    previous = None
    for fit in measured:
        phase = fit.result["phase_rad"]
        if previous is None:
            phase = math.pi if phase == -math.pi else phase
        else:
            phase += 2.0 * math.pi * round((previous - phase) / (2.0 * math.pi))
        fit.result["phase_rad"] = phase
        previous = phase


def _point(fit: _Fit) -> FrequencyPoint:
    return FrequencyPoint(
        **fit.base,
        **fit.result,
        timing=fit.timing,
        uncertainties=fit.uncertainties,
        reasons=fit.reasons,
        warnings=tuple(fit.warnings),
    )


def _overall_warnings(points: tuple[FrequencyPoint, ...]) -> tuple[str, ...]:
    measured = [point for point in points if point.phase_rad is not None]
    if not measured:
        return ("no frequency could be measured; each point's reasons say why",)
    lowest = min(measured, key=lambda point: point.frequency_hz)
    if lowest.phase_rad <= LEAD_WARNING_RAD:
        return ()
    return (
        f"at the lowest frequency measured, {_hz(lowest.frequency_hz)}, the response leads the"
        f" command by {math.degrees(lowest.phase_rad):.0f} deg (gain {lowest.gain_db:.1f} dB)."
        " A position servo lags its command; a lead near 180 deg at a low frequency means"
        " the actuator moves opposite to the command (reversed direction). Unless that"
        " frequency is far above the servo's bandwidth, stop and check its direction before"
        " running anything else",
    )


def _stop_note(trace: Trace) -> str:
    return "" if trace.stop is None else f" ({trace.stop.kind}: {trace.stop.message})"


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=4)


def _hz(value_hz: float) -> str:
    return format_quantity(value_hz, Dimension.FREQUENCY, "Hz", digits=4)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=4)
