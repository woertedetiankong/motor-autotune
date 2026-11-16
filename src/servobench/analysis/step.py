"""Step-response metrics, from the samples and command times a run recorded.

``analyse_step(trace, spec)`` measures every step of a ``Step`` experiment, the
steps back to the centre included, from the runner's ``Trace`` (live, or
loaded back with ``records.load_run``). It needs no adapter. Every time is
taken from the recorded timestamps; nothing assumes even spacing.

Definitions. For one step:

- ``t_command_s``: when the adapter sent the step's command (its
  ``CommandRecord``), on the adapter's clock. Every time below is measured
  from it, so the device's own command delay is part of the response.
- The step's response runs from ``t_command_s`` until the next command was
  sent (or the trace ended).
- ``initial_position_rad``: the mean of the readings in the last
  ``END_WINDOW_SHARE`` (20 %) of the previous segment, before the command.
  ``final_position_rad``: the mean of the readings in the last 20 % of this
  step's response. Each must hold at least ``MIN_WINDOW_SAMPLES`` readings and
  be settled: a straight line fitted to the readings may change across the
  window by at most ``SETTLED_DRIFT_SHARE`` (1 %) of the commanded step, or one
  reading step if that is larger. Measured values are used, not commanded
  ones, because under a gravity load a servo settles short of its command.
- ``measured_step_rad`` = final - initial; ``step_ratio`` = measured step /
  commanded step. The normalised response is (reading - initial) / measured step.
- ``delay_s``: from the command to the first time the normalised response
  reaches ``DELAY_LEVEL`` (50 %): the delay time of IEEE 181 and Ogata. It is
  the delay to the middle of the motion, not the dead time before the first
  movement, which a 4096-step reading cannot place to within a sample.
- ``rise_time_s``: from the first time the response reaches ``RISE_LOW`` (10 %)
  to the first time it reaches ``RISE_HIGH`` (90 %).
- ``overshoot_percent``: how far the response goes past the final value, as a
  percentage of the measured step: 100 * (peak - 1) for the normalised
  response's peak, or 0 if it never passes 1. The peak is the vertex of a
  least-squares parabola through the readings within ``PEAK_WINDOW_SHARE`` of
  the rise time on each side of the highest point of the running median of
  three (see settling time), which averages the noise and finds a peak that
  falls between two samples. Where the readings there show no clear peak (too
  few, flat, or the vertex outside them), that highest median point is used.
- ``settling_time_s``: from the command to the last time the response is
  outside a band of +/- ``SETTLING_BAND`` (2 %) of the measured step around
  the final value. For this one decision each reading is replaced by the
  median of itself and its two neighbours, so that one noisy reading does not
  count as leaving the band. A running median of three leaves a rising or
  falling stretch of readings unchanged; it only removes isolated spikes.
- ``steady_state_error_rad``: commanded position - final position. Its sign
  says which side of the command the actuator stopped: positive below it.

Every crossing time is interpolated linearly between the two readings either
side of it. A crossing between two readings more than ``MAX_GAP_INTERVALS``
median sample intervals apart (a missed reply) is not measurable.

Reading quality and uncertainty. A position reading is quantised: 4096 steps
per turn is 0.088 deg a step, so a 10 deg step is 114 reading steps and one
reading step is 0.9 % of it. Without noise a constant position always gives
the same reading, so the final position can be off by up to half a step and
nothing in the readings shows by how much; each reading near a level is off by
up to another half step. Each step therefore reports
``reading_uncertainty_rad``, u = the larger of one reading step and twice the
noise, and in ``uncertainties``, for each metric, the largest change in it
when the levels that define it (10 %, 50 % and 90 %, the edges of the settling
band) are moved by u either way; for overshoot, u as a percentage of the step.
A level the response crosses slowly gives a large uncertainty. That happens to
settling time when the response runs along the edge of the band, for example a
3 % overshoot against a 2 % band: there, half a reading step moves the
settling time by tens of milliseconds. A time whose uncertainty is more than
``MAX_UNCERTAINTY_SHARE`` (25 %) of its value is not reported, and neither is
a settling time when the readings at the end of the step are not steadily
inside the band narrowed by u. The steady-state error's uncertainty is half a
reading step plus twice the standard error of the final mean. The
uncertainties are sensitivities to the readings' resolution and noise, not
guaranteed bounds, and leave out timestamp error: on the simulated servo with
noise and jitter the error against the model was up to 1.4 times the delay's
uncertainty and 1.1 times the others, and within it for settling time.

Direction. A step that ends on the opposite side of where it started from the
command, or does not move at all, gets no delay, rise, overshoot or settling
time: they would describe a motion that was not commanded. Its positions and
``step_ratio`` (near -1 for a reversed actuator) are still reported, and the
analysis's ``warnings`` say how many steps moved the wrong way.

Zero. A servo whose reading is offset from where its arm hangs follows its
commands normally, so its step metrics are ordinary. The offset shows in the
trace quality's ``position_before_enable_rad`` and in the steady-state errors
(gravity pulls the arm towards where it really hangs). Judging them needs the
bench's zero and load; ``check`` does that, not this analysis.
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
from servobench.experiments.runner import CommandRecord, Trace
from servobench.experiments.spec import APPROACH, STEP, Step
from servobench.units import Dimension, format_quantity

DELAY_LEVEL = 0.5
RISE_LOW = 0.1
RISE_HIGH = 0.9
SETTLING_BAND = 0.02
# The windows that fix the initial and final positions: this share of the segment's end.
END_WINDOW_SHARE = 0.2
# A window is settled if its readings drift by no more than this share of the commanded step.
SETTLED_DRIFT_SHARE = 0.01
MIN_WINDOW_SAMPLES = 5
# A step smaller than this many times the readings' uncertainty u (at least one
# reading step) is not measured: u would be more than 5 % of the step, so
# overshoot would be uncertain by more than 5 percentage points, and the 2 %
# settling band would be narrower than half a reading step.
MIN_STEP_RESOLUTIONS = 20
# A crossing between two readings further apart than this many median intervals is not measured.
MAX_GAP_INTERVALS = 3.0
# The parabola for the peak uses the readings within this share of the rise time
# of the highest point: wide enough to average several readings, narrow enough
# that the asymmetry of a damped peak moves the vertex by under 0.1 percentage
# point on closed-form second-order responses (damping ratio 0.3 to 0.7).
PEAK_WINDOW_SHARE = 0.3
PEAK_MIN_SAMPLES = 5
# A metric whose uncertainty is more than this share of its value is not reported.
MAX_UNCERTAINTY_SHARE = 0.25
# A step that covers less than this share, or more than its inverse, of the command is flagged.
STEP_RATIO_WARNING = 0.5

METRICS = ("delay_s", "rise_time_s", "overshoot_percent", "settling_time_s")
UNCERTAIN = (*METRICS, "steady_state_error_rad")  # the metrics that carry an uncertainty
MEASURED = (
    "t_command_s",
    "initial_position_rad",
    "final_position_rad",
    "measured_step_rad",
    "step_ratio",
    *METRICS,
    "steady_state_error_rad",
    "noise_rad",
    "reading_uncertainty_rad",
)

DEFINITIONS = {
    "t_command_s": "when the step's command was sent, on the adapter's clock",
    "initial_position_rad": "mean reading over the last 20 % of the segment before the step",
    "final_position_rad": "mean reading over the last 20 % of the step's response",
    "measured_step_rad": "final_position_rad - initial_position_rad",
    "step_ratio": "measured_step_rad / commanded step",
    "delay_s": "command to the response first reaching 50 % of the measured step",
    "rise_time_s": "response first reaching 10 % to first reaching 90 % of the measured step",
    "overshoot_percent": "peak past the final value, in percent of the measured step; 0 if none",
    "settling_time_s": "command to the last time outside +/-2 % of the measured step around"
    " the final value",
    "steady_state_error_rad": "commanded position - final_position_rad",
    "noise_rad": "standard deviation of the readings over the last 20 % of the step's response",
    "reading_uncertainty_rad": "u: the larger of the reading step and twice noise_rad",
    "uncertainties": "per metric, the largest change when the levels that define it (10 %,"
    " 50 %, 90 %, the band edges) move by u either way; for overshoot, u in percent of the"
    " step; for steady-state error, half a reading step plus twice the standard error of the"
    " final mean. From resolution and noise only, not timestamp error",
}


@dataclass(frozen=True)
class StepResponse:
    """The measured response to one step. A metric that could not be measured is None,
    with the reason in ``reasons`` under its name."""

    segment: int  # index into the trace's segments
    label: str
    commanded_from_rad: float
    commanded_to_rad: float
    t_command_s: float | None
    initial_position_rad: float | None
    final_position_rad: float | None
    measured_step_rad: float | None
    step_ratio: float | None
    delay_s: float | None
    rise_time_s: float | None
    overshoot_percent: float | None
    settling_time_s: float | None
    steady_state_error_rad: float | None
    noise_rad: float | None
    reading_uncertainty_rad: float | None
    timing: Timing  # of the readings in the step's response
    # metric name -> +/- uncertainty in the metric's unit, for each metric reported
    uncertainties: dict[str, float] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_reasons(self, MEASURED)
        for name in UNCERTAIN:
            if (getattr(self, name) is None) != (name not in self.uncertainties):
                raise ValueError(f"{name}: an uncertainty is given exactly when a value is")

    @property
    def commanded_step_rad(self) -> float:
        return self.commanded_to_rad - self.commanded_from_rad


@dataclass(frozen=True)
class StepAnalysis:
    """Every step of one ``Step`` experiment."""

    quality: TraceQuality
    steps: tuple[StepResponse, ...]
    warnings: tuple[str, ...]
    definitions: dict[str, str] = field(default_factory=lambda: dict(DEFINITIONS))


def analyse_step(trace: Trace, spec: Step) -> StepAnalysis:
    """Measure every step in ``trace``, the record of running ``spec``."""
    if not isinstance(spec, Step):
        raise TypeError(f"analyse_step needs a Step spec, got {type(spec).__name__}")
    _check_trace_matches(trace, spec)
    quality = trace_quality(trace)
    times, values = positions(trace)
    steps = tuple(
        _analyse_one(trace, index, times, values, quality)
        for index, segment in enumerate(trace.segments)
        if segment.kind == STEP
    )
    return StepAnalysis(quality=quality, steps=steps, warnings=_overall_warnings(steps))


def _check_trace_matches(trace: Trace, spec: Step) -> None:
    planned = tuple(segment for segment in trace.segments if segment.kind != APPROACH)
    if planned != spec.segments():
        raise ValueError("the trace's segments are not those of this spec; is it the right run?")


# -- one step --------------------------------------------------------------------------


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _analyse_one(
    trace: Trace, index: int, times: np.ndarray, values: np.ndarray, quality: TraceQuality
) -> StepResponse:
    segment = trace.segments[index]
    r0, r1 = segment.from_position_rad, segment.position_rad
    commanded = r1 - r0
    result: dict[str, float | None] = dict.fromkeys(MEASURED)
    reasons: dict[str, str] = {}
    warnings: list[str] = []
    uncertainties: dict[str, float] = {}
    response_timing = timing_of(())

    def done() -> StepResponse:
        return StepResponse(
            segment=index,
            label=segment.label,
            commanded_from_rad=r0,
            commanded_to_rad=r1,
            timing=response_timing,
            uncertainties=uncertainties,
            reasons=reasons,
            warnings=tuple(warnings),
            **result,
        )

    def refuse(names, reason: str) -> None:
        for name in names:
            if result[name] is None and name not in reasons:
                reasons[name] = reason

    command = _first_command(trace, index)
    if command is None:
        refuse(MEASURED, f"the run stopped before this step was commanded{_stop_note(trace)}")
        return done()
    t_c = command.t_s
    result["t_command_s"] = t_c

    later = [c.t_s for c in trace.commands if c.segment > index]
    t_end = later[0] if later else trace.t_end_s
    in_response = (times > t_c) & (times < t_end)
    response_timing = timing_of(times[in_response])
    if not later and trace.stop is not None:
        ran_s = (trace.t_end_s or t_c) - t_c
        refuse(
            MEASURED,
            f"the run stopped {_s(ran_s)} into this step's {_s(segment.duration_s)} dwell"
            f"{_stop_note(trace)}; the step has no final position",
        )
        return done()

    resolution = quality.position_resolution_rad or 0.0
    allowed_drift = max(SETTLED_DRIFT_SHARE * abs(commanded), resolution)

    # The final position, from the end of this step's response.
    final_from = t_end - END_WINDOW_SHARE * (t_end - t_c)
    final_mask = in_response & (times >= final_from)
    try:
        final = _settled_mean(
            times[final_mask],
            values[final_mask],
            allowed_drift,
            "at the end of the step",
            "make dwell_s longer",
        )
    except _Refused as refused:
        refuse(MEASURED[2:], refused.reason)
    else:
        result["final_position_rad"] = final
        result["steady_state_error_rad"] = r1 - final
        noise = float(np.std(values[final_mask]))
        result["noise_rad"] = noise
        result["reading_uncertainty_rad"] = max(resolution, 2.0 * noise)
        # Half a reading step, which a reading without noise cannot resolve, and
        # twice the standard error of the mean.
        uncertainties["steady_state_error_rad"] = 0.5 * resolution + 2.0 * noise / math.sqrt(
            np.count_nonzero(final_mask)
        )

    # The initial position, from the end of the segment before.
    previous_start = trace.segment_start_s[index - 1] if index > 0 else trace.t_start_s
    previous_s = t_c - previous_start
    initial_from = t_c - END_WINDOW_SHARE * previous_s
    initial_mask = (times >= max(initial_from, previous_start)) & (times < t_c)
    try:
        initial = _settled_mean(
            times[initial_mask],
            values[initial_mask],
            allowed_drift,
            "before the step was commanded",
            "make settle_s or dwell_s longer",
        )
    except _Refused as refused:
        refuse(
            ("initial_position_rad", "measured_step_rad", "step_ratio", *METRICS), refused.reason
        )
    else:
        result["initial_position_rad"] = initial

    if result["final_position_rad"] is None or result["initial_position_rad"] is None:
        refuse(MEASURED, "see initial_position_rad and final_position_rad")
        return done()

    measured = result["final_position_rad"] - result["initial_position_rad"]
    result["measured_step_rad"] = measured
    ratio = measured / commanded
    result["step_ratio"] = ratio

    if measured * commanded <= 0:
        how = "did not move" if measured == 0 else f"moved {_deg(measured)}"
        refuse(
            METRICS,
            f"for a commanded step of {_deg(commanded)} the actuator {how}: the opposite way"
            " to the command, or not at all. Its direction may be reversed; the step metrics"
            " would describe a motion that was not commanded",
        )
        return done()
    reading_u = result["reading_uncertainty_rad"]
    if reading_u and abs(measured) < MIN_STEP_RESOLUTIONS * reading_u:
        refuse(
            METRICS,
            f"the measured step of {_deg(measured)} is only {abs(measured) / reading_u:.1f}"
            f" times the readings' uncertainty of {_deg(reading_u)} (reading step"
            f" {_deg(resolution)}, noise {_deg(result['noise_rad'])}); at least"
            f" {MIN_STEP_RESOLUTIONS} times is needed, because below that the uncertainty is"
            " more than 5 % of the step. Use a larger step",
        )
        return done()
    if ratio < STEP_RATIO_WARNING or ratio > 1 / STEP_RATIO_WARNING:
        warnings.append(
            f"the actuator moved {ratio:.0%} of the commanded step; the metrics describe the"
            " motion it made, relative to where it stopped, not the motion commanded"
        )

    # The normalised response, from the last reading before the command.
    before = np.flatnonzero(initial_mask)
    mask = in_response.copy()
    mask[before[-1]] = True
    t = times[mask]
    x = (values[mask] - result["initial_position_rad"]) / measured
    median_s = response_timing.interval_median_s or float(np.median(np.diff(t)))
    max_gap_s = MAX_GAP_INTERVALS * median_s
    u = result["reading_uncertainty_rad"] / abs(measured)  # as a share of the step

    def crossing(level: float) -> float:
        return _first_crossing(t, x, level, max_gap_s)

    try:
        delay = crossing(DELAY_LEVEL)
        shifted = [crossing(DELAY_LEVEL - u), crossing(DELAY_LEVEL + u)]
    except _Refused as refused:
        refuse(("delay_s",), refused.reason)
    else:
        result["delay_s"] = delay - t_c
        uncertainties["delay_s"] = max(abs(other - delay) for other in shifted)
    try:
        rise = crossing(RISE_HIGH) - crossing(RISE_LOW)
        shifted = [
            crossing(high) - crossing(low)
            for low in (RISE_LOW - u, RISE_LOW + u)
            for high in (RISE_HIGH - u, RISE_HIGH + u)
        ]
    except _Refused as refused:
        refuse(("rise_time_s",), refused.reason)
    else:
        result["rise_time_s"] = rise
        uncertainties["rise_time_s"] = max(abs(other - rise) for other in shifted)

    result["overshoot_percent"] = 100.0 * max(0.0, _peak(t, x, result["rise_time_s"]) - 1.0)
    uncertainties["overshoot_percent"] = 100.0 * u

    smoothed = _median_of_three(x)
    final_from_s = float(times[final_mask][0])
    narrow = SETTLING_BAND - u
    try:
        if narrow <= 0:
            raise _Refused(
                f"the readings are uncertain by {u:.1%} of the step (reading step"
                f" {_deg(resolution)}, noise {_deg(result['noise_rad'])}), as much as the"
                f" +/-{SETTLING_BAND:.0%} settling band. Use a larger step"
            )
        settled = _settling_time(t, smoothed, SETTLING_BAND, max_gap_s)
        early = _settling_time(t, smoothed, SETTLING_BAND + u, max_gap_s)
        try:
            late = _settling_time(t, smoothed, narrow, max_gap_s)
        except _Refused:
            late = math.inf
        if late >= final_from_s:
            raise _Refused(
                f"the readings at the end of the step are not steadily inside the"
                f" +/-{SETTLING_BAND:.0%} band less their uncertainty (+/-{narrow:.1%}; reading"
                f" step {_deg(resolution)}, noise {_deg(result['noise_rad'])}), so when the"
                " response settled cannot be told from the noise and the reading step. Use a"
                " larger step"
            )
    except _Refused as refused:
        refuse(("settling_time_s",), refused.reason)
    else:
        result["settling_time_s"] = settled - t_c
        uncertainties["settling_time_s"] = max(settled - early, late - settled)

    # A time that the readings' uncertainty could move by more than a quarter is not reported.
    for name in ("delay_s", "rise_time_s", "settling_time_s"):
        value = result[name]
        if value is not None and uncertainties[name] > MAX_UNCERTAINTY_SHARE * value:
            spread_s = uncertainties.pop(name)
            result[name] = None
            hint = (
                " The response runs close to the edge of the settling band, so the last time"
                " it leaves the band depends on fractions of a reading step."
                if name == "settling_time_s"
                else ""
            )
            refuse(
                (name,),
                f"measured {_s(value)}, but moving its levels by the readings' uncertainty"
                f" ({u:.1%} of the step: reading step {_deg(resolution)}, noise"
                f" {_deg(result['noise_rad'])}) moves it by {_s(spread_s)}, more than"
                f" {MAX_UNCERTAINTY_SHARE:.0%} of it.{hint} A larger step would measure it",
            )

    return done()


def _first_command(trace: Trace, index: int) -> CommandRecord | None:
    for command in trace.commands:
        if command.segment == index:
            return command
    return None


def _settled_mean(
    t: np.ndarray, y: np.ndarray, allowed_drift_rad: float, where: str, remedy: str
) -> float:
    if t.size < MIN_WINDOW_SAMPLES:
        raise _Refused(
            f"only {t.size} readings {where}, too few to fix the position (at least"
            f" {MIN_WINDOW_SAMPLES} are needed); {remedy}"
        )
    slope = np.polyfit(t - t[0], y, 1)[0] if np.ptp(t) > 0 else 0.0
    drift = float(slope * (t[-1] - t[0]))
    if abs(drift) > allowed_drift_rad:
        raise _Refused(
            f"the position was still changing {where}: by {_deg(drift)} over the last"
            f" {_s(t[-1] - t[0])}, more than the {_deg(allowed_drift_rad)} allowed; {remedy}"
        )
    return float(np.mean(y))


def _first_crossing(t: np.ndarray, x: np.ndarray, level: float, max_gap_s: float) -> float:
    above = np.flatnonzero(x >= level)
    if above.size == 0:
        raise _Refused(f"the response never reached {level:.0%} of the measured step")
    k = int(above[0])
    if k == 0:
        raise _Refused(f"the response was already past {level:.0%} at its first reading")
    if t[k] - t[k - 1] > max_gap_s:
        raise _Refused(
            f"readings are missing where the response crossed {level:.0%}: a gap of"
            f" {_s(t[k] - t[k - 1])} between readings"
        )
    return _interpolate(t[k - 1], x[k - 1], t[k], x[k], level)


def _settling_time(t: np.ndarray, x: np.ndarray, band: float, max_gap_s: float) -> float:
    """When ``x`` last entered the band +/- ``band`` around 1, interpolated."""
    outside = np.flatnonzero(np.abs(x - 1.0) > band)
    j = int(outside[-1])  # the first reading, before the step, is always outside
    if j == x.size - 1:
        raise _Refused("the response was outside the settling band at its last reading")
    if t[j + 1] - t[j] > max_gap_s:
        raise _Refused(
            f"readings are missing where the response entered the settling band: a gap of"
            f" {_s(t[j + 1] - t[j])} between readings"
        )
    edge = 1.0 + band if x[j] > 1.0 else 1.0 - band
    return _interpolate(t[j], x[j], t[j + 1], x[j + 1], edge)


def _median_of_three(x: np.ndarray) -> np.ndarray:
    """Each value replaced by the median of itself and its neighbours; the ends kept."""
    out = x.copy()
    if x.size >= 3:
        out[1:-1] = np.median(np.stack((x[:-2], x[1:-1], x[2:])), axis=0)
    return out


def _peak(t: np.ndarray, x: np.ndarray, rise_time_s: float | None) -> float:
    """The response's peak: a parabola's vertex if there is a clear one, else the highest
    reading of the running median of three (which a single noisy reading cannot raise)."""
    smoothed = _median_of_three(x)
    k = int(np.argmax(smoothed))
    highest = float(smoothed[k])
    if rise_time_s is None or rise_time_s <= 0:
        return highest
    near = np.abs(t - t[k]) <= PEAK_WINDOW_SHARE * rise_time_s
    if np.count_nonzero(near) < PEAK_MIN_SAMPLES:
        return highest
    tn = t[near] - t[k]
    a, b, c = np.polyfit(tn, x[near], 2)
    if a >= 0:  # no peak there: the readings are flat or rising
        return highest
    vertex_s = -b / (2 * a)
    if not tn[0] <= vertex_s <= tn[-1]:
        return highest
    return float(c - b * b / (4 * a))


def _interpolate(t0: float, x0: float, t1: float, x1: float, level: float) -> float:
    if x1 == x0:
        return float(t1)
    return float(t0 + (level - x0) * (t1 - t0) / (x1 - x0))


def _overall_warnings(steps: tuple[StepResponse, ...]) -> tuple[str, ...]:
    measured = [step for step in steps if step.step_ratio is not None]
    wrong = [step for step in measured if step.step_ratio <= 0]
    if not wrong:
        return ()
    return (
        f"{len(wrong)} of {len(measured)} measured steps moved opposite to their command."
        " If every step did, the actuator turns the wrong way (reversed direction): stop and"
        " check its direction setting and wiring before running anything else",
    )


def _stop_note(trace: Trace) -> str:
    return "" if trace.stop is None else f" ({trace.stop.kind}: {trace.stop.message})"


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=4)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=4)
