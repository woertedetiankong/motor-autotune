"""The metric definitions, checked against responses whose answers are known in closed form.

These tests build traces by hand, sampled at uneven times and without
quantisation or noise, so they check the definitions and the arithmetic
independently of the servo model (the accuracy tests use the model's replay as
the true answer, through these same definitions).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from servobench.analysis import analyse_step, analyse_stepped_sine, timing_of
from servobench.contract import POSITION, Sample
from servobench.experiments.runner import CommandRecord, Trace
from servobench.experiments.spec import Step, SteppedSine

DEG = math.radians(1.0)
AMPLITUDE_RAD = 10 * DEG
DEAD_S = 0.006


def uneven_times(start_s: float, end_s: float, seed: int = 1) -> np.ndarray:
    """Sample times from start_s to end_s, 3 to 7 ms apart at random."""
    rng = np.random.default_rng(seed)
    gaps = rng.uniform(0.003, 0.007, size=int((end_s - start_s) / 0.003) + 2)
    times = start_s + np.cumsum(gaps)
    return times[times < end_s]


def step_trace(spec: Step, unit_response, times=None, resolution_rad=None) -> Trace:
    """A trace of ``spec`` whose response to each step is ``unit_response(time since command)``,
    with readings quantised to ``resolution_rad`` if given."""
    segments = spec.segments()
    t0 = 0.01
    starts = np.cumsum([t0] + [segment.duration_s for segment in segments[:-1]])
    end_s = starts[-1] + segments[-1].duration_s
    # Each command is sent a little after its segment starts, as the runner's loop does.
    command_s = starts + 0.0004
    commands = tuple(
        CommandRecord(float(t), POSITION, segment.position_rad, index)
        for index, (t, segment) in enumerate(zip(command_s, segments, strict=True))
    )
    times = uneven_times(t0, end_s) if times is None else times

    def position(t: float) -> float:
        value = segments[0].position_rad
        for segment, sent in zip(segments[1:], command_s[1:], strict=True):
            if t > sent:
                value = segment.from_position_rad + (
                    segment.position_rad - segment.from_position_rad
                ) * unit_response(t - sent)
        if resolution_rad is not None:
            value = round(value / resolution_rad) * resolution_rad
        return value

    samples = (Sample(0.0, {POSITION: 0.0}),) + tuple(
        Sample(float(t), {POSITION: position(float(t))}) for t in times
    )
    return Trace(
        simulated=True,
        segments=segments,
        segment_start_s=tuple(float(s) for s in starts),
        samples=samples,
        commands=commands,
        missed=(),
        enable_position_rad=0.0,
        t_enable_s=0.005,
        t_start_s=t0,
        t_end_s=float(end_s),
        stop=None,
    )


def first_order(tau_s: float):
    return lambda t: 0.0 if t < DEAD_S else 1.0 - math.exp(-(t - DEAD_S) / tau_s)


def second_order(zeta: float, omega_rad_s: float):
    wd = omega_rad_s * math.sqrt(1 - zeta * zeta)
    phi = math.acos(zeta)

    def x(t: float) -> float:
        if t < DEAD_S:
            return 0.0
        s = t - DEAD_S
        return 1 - math.exp(-zeta * omega_rad_s * s) * math.sin(wd * s + phi) / math.sqrt(
            1 - zeta * zeta
        )

    return x


def brute_force(unit_response, end_s=1.0, dt_s=1e-6):
    """Delay, rise, overshoot and settling of a unit response, on a 1 microsecond grid."""
    t = np.arange(0.0, end_s, dt_s)
    x = np.array([unit_response(s) for s in t])
    first = lambda level: t[np.argmax(x >= level)]  # noqa: E731
    outside = np.flatnonzero(np.abs(x - 1) > 0.02)
    return {
        "delay_s": first(0.5),
        "rise_time_s": first(0.9) - first(0.1),
        "overshoot_percent": 100 * max(0.0, x.max() - 1),
        "settling_time_s": t[outside[-1] + 1],
    }


SPEC = Step(0.0, (AMPLITUDE_RAD,), dwell_s=1.0, settle_s=0.5)


def test_first_order_response_matches_closed_form_with_uneven_samples():
    tau_s = 0.05
    result = analyse_step(step_trace(SPEC, first_order(tau_s)), SPEC)
    for step in result.steps:
        assert step.reasons == {}
        # Closed forms for 1 - exp(-(t - L)/tau): 50 % at L + tau ln 2, 10-90 % in tau ln 9,
        # inside 2 % from L + tau ln 50, no overshoot.
        assert step.delay_s == pytest.approx(DEAD_S + tau_s * math.log(2), abs=0.3e-3)
        assert step.rise_time_s == pytest.approx(tau_s * math.log(9), abs=0.3e-3)
        assert step.settling_time_s == pytest.approx(DEAD_S + tau_s * math.log(50), abs=0.3e-3)
        assert step.overshoot_percent == pytest.approx(0.0, abs=1e-3)
        assert step.steady_state_error_rad == pytest.approx(0.0, abs=1e-6)
        assert step.step_ratio == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("zeta", [0.3, 0.5, 0.7])
def test_second_order_response_matches_closed_form_and_brute_force(zeta):
    response = second_order(zeta, 40.0)
    expected = brute_force(response)
    # The overshoot of an underdamped second-order step response, in closed form.
    closed_form = 100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta * zeta))
    assert expected["overshoot_percent"] == pytest.approx(closed_form, abs=1e-3)
    result = analyse_step(step_trace(SPEC, response), SPEC)
    for step in result.steps:
        assert step.reasons == {}
        assert step.overshoot_percent == pytest.approx(closed_form, abs=0.1)
        for name in ("delay_s", "rise_time_s", "settling_time_s"):
            # Linear interpolation between readings 3 to 7 ms apart.
            assert getattr(step, name) == pytest.approx(expected[name], abs=0.5e-3), name


def test_even_and_uneven_sampling_give_the_same_metrics():
    response = second_order(0.5, 40.0)
    even = analyse_step(step_trace(SPEC, response, np.arange(0.012, 2.5, 0.005)), SPEC)
    uneven = analyse_step(step_trace(SPEC, response), SPEC)
    for a, b in zip(even.steps, uneven.steps, strict=True):
        for name in ("delay_s", "rise_time_s", "settling_time_s"):
            assert getattr(a, name) == pytest.approx(getattr(b, name), abs=0.5e-3)
        assert a.overshoot_percent == pytest.approx(b.overshoot_percent, abs=0.1)


def test_a_settling_band_grazed_by_the_peak_is_refused_when_readings_are_coarse():
    # Overshoot 2.1 %, just over the 2 % band: the settling time jumps between the
    # rise and the fall for a change of 0.1 % in level. With readings uncertain by
    # a reading step of 0.9 % of the step it cannot be told, so it is refused.
    response = second_order(0.776, 40.0)
    assert brute_force(response)["overshoot_percent"] == pytest.approx(2.1, abs=0.05)
    trace = step_trace(SPEC, response, resolution_rad=2 * math.pi / 4096)
    for step in analyse_step(trace, SPEC).steps:
        assert step.reading_uncertainty_rad == pytest.approx(2 * math.pi / 4096)
        assert step.overshoot_percent == pytest.approx(
            2.1, abs=step.uncertainties["overshoot_percent"]
        )
        assert step.settling_time_s is None
        assert "edge of the settling band" in step.reasons["settling_time_s"]


def test_a_sine_fit_on_uneven_samples_recovers_gain_and_phase():
    spec = SteppedSine(0.0, 3 * DEG, (2.0,), cycles=6, transient_cycles=2, settle_s=0.5)
    settle, sine = spec.segments()
    t0 = 0.01
    starts = (t0, t0 + settle.duration_s)
    end_s = starts[1] + sine.duration_s
    gain, lag_rad, offset_rad = 0.7, math.radians(-40.0), 0.3 * DEG
    # Commands roughly every 10 ms, unevenly; each is the ideal sine at the moment it is sent.
    command_s = np.concatenate(([t0], uneven_times(starts[1], end_s, seed=2)[::2]))
    commands = [CommandRecord(t0, POSITION, 0.0, 0)] + [
        CommandRecord(float(t), POSITION, sine.setpoint_rad(float(t) - starts[1]), 1)
        for t in command_s[1:]
    ]
    # The response to the held command's fundamental: the staircase lags the
    # ideal sine, so the expected phase is taken from the staircase, computed
    # here by brute force on a fine grid.
    grid = np.arange(starts[1] + 2 / 2.0, end_s, 1e-6)
    held = np.array([c.value for c in commands])[np.searchsorted(command_s, grid, side="right") - 1]
    omega = 2 * math.pi * 2.0
    c1 = 2 * np.mean(held * np.exp(-1j * omega * grid))
    response_c = gain * np.exp(1j * lag_rad) * c1
    times = uneven_times(starts[1], end_s, seed=3)
    samples = (Sample(0.0, {POSITION: 0.0}),) + tuple(
        Sample(
            float(t), {POSITION: offset_rad + float(np.real(response_c * np.exp(1j * omega * t)))}
        )
        for t in times
    )
    trace = Trace(
        simulated=True,
        segments=spec.segments(),
        segment_start_s=starts,
        samples=samples,
        commands=tuple(commands),
        missed=(),
        enable_position_rad=0.0,
        t_enable_s=0.005,
        t_start_s=t0,
        t_end_s=float(end_s),
        stop=None,
    )
    point = analyse_stepped_sine(trace, spec).points[0]
    assert point.reasons == {}
    assert point.gain == pytest.approx(gain, rel=1e-4)
    assert point.phase_rad == pytest.approx(lag_rad, abs=1e-4)
    assert point.response_offset_rad == pytest.approx(offset_rad, abs=1e-9)
    assert point.command_amplitude_rad == pytest.approx(abs(c1), rel=1e-4)
    # The staircase's fundamental is smaller and later than the ideal sine's.
    assert point.command_amplitude_rad < spec.amplitude_rad


def test_timing_reports_rate_gaps_and_jitter_from_the_timestamps():
    times = [0.0, 0.005, 0.010, 0.016, 0.020, 0.035, 0.040]
    timing = timing_of(times)
    intervals = np.diff(times)
    assert timing.samples == 7
    assert timing.duration_s == pytest.approx(0.04)
    assert timing.sample_rate_hz == pytest.approx(6 / 0.04)
    assert timing.interval_median_s == pytest.approx(0.005)
    assert timing.interval_max_s == pytest.approx(0.015)
    assert timing.jitter_s == pytest.approx(np.std(intervals))
    assert timing_of([1.0]).sample_rate_hz is None
