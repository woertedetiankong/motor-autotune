"""Frequency-response analysis on the simulated servo, against the model's own response.

The model's own frequency response at a frequency is taken two ways:

- The true answer for the tests: the model's exact, noise-free response to the
  commands the run actually sent, finely sampled (``analysis_support.true_trace``),
  through the same analysis. Coulomb friction makes the model nonlinear: at
  3 deg amplitude the response differs from the model's small-signal
  linearisation by up to 0.7 dB and 3.8 deg, and at 1 deg by up to 2 dB and
  11 deg. The measurement is right about that; the linearisation is not.
- An independent check of the definitions: with Coulomb friction removed the
  model is linear apart from gravity's sin, and the measured gain and phase
  match its transfer function, computed in closed form here.
"""

from __future__ import annotations

import cmath
import dataclasses
import math

import pytest
from analysis_support import (
    DEG,
    LOAD,
    NOISELESS,
    WIDE_BENCH,
    completed,
    run,
    true_trace,
)

from servobench.analysis import analyse_stepped_sine
from servobench.analysis.frequency import MIN_SAMPLES_PER_CYCLE
from servobench.experiments.runner import Outcome
from servobench.experiments.spec import SteppedSine
from servobench.model import DEFAULT_PARAMS, GRAVITY_M_S2

SPEC = SteppedSine(
    0.0, 3 * DEG, (0.5, 1.0, 2.0, 4.0, 6.0, 8.0), cycles=6, transient_cycles=2, settle_s=0.5
)
NOISY_SEEDS = range(10)


def linear_response(frequency_hz: float, params=DEFAULT_PARAMS, centre_rad: float = 0.0) -> complex:
    """The model's transfer function from command to position, linearised about the centre."""
    s = 2j * math.pi * frequency_hz
    inertia = params.inertia_kg_m2 + LOAD.inertia_kg_m2
    damping = params.kd_n_m_s_rad + params.viscous_n_m_s_rad
    gravity = LOAD.mass_kg * GRAVITY_M_S2 * LOAD.com_distance_m * math.cos(centre_rad)
    return (
        cmath.exp(-s * params.delay_s)
        * params.kp_n_m_rad
        / (inertia * s * s + damping * s + params.kp_n_m_rad + gravity)
    )


def phase_error_rad(a: float, b: float) -> float:
    return abs(math.remainder(a - b, 2 * math.pi))


@pytest.mark.parametrize("noisy", [False, True], ids=["without noise", "with noise and jitter"])
def test_gain_and_phase_match_the_model_within_1_db_and_5_deg(noisy):
    factor = 2 if noisy else 1
    for config in [dict(seed=seed) for seed in NOISY_SEEDS] if noisy else [NOISELESS]:
        trace = completed(SPEC, **config)
        measured = analyse_stepped_sine(trace, SPEC)
        truth = analyse_stepped_sine(true_trace(trace), SPEC)
        assert measured.warnings == ()
        for point, true in zip(measured.points, truth.points, strict=True):
            # Every frequency here has at least 11 readings a cycle: all are measured.
            assert point.samples_per_cycle >= 11
            assert point.reasons == {}, point.reasons
            assert abs(point.gain_db - true.gain_db) <= factor * 1.0
            assert phase_error_rad(point.phase_rad, true.phase_rad) <= factor * math.radians(5)


@pytest.mark.parametrize("noisy", [False, True], ids=["without noise", "with noise and jitter"])
def test_without_friction_gain_and_phase_match_the_closed_form_transfer_function(noisy):
    params = dataclasses.replace(DEFAULT_PARAMS, coulomb_n_m=0.0)
    factor = 2 if noisy else 1
    trace = completed(SPEC, params=params, **(dict(seed=3) if noisy else NOISELESS))
    for point in analyse_stepped_sine(trace, SPEC).points:
        expected = linear_response(point.frequency_hz, params)
        assert abs(point.gain_db - 20 * math.log10(abs(expected))) <= factor * 1.0
        assert phase_error_rad(point.phase_rad, cmath.phase(expected)) <= factor * math.radians(5)


def test_the_held_command_not_the_ideal_sine_is_the_reference():
    # Commands go out about every 10 ms; held between them, the command lags the
    # ideal sine by about 5 ms, 14 deg at 8 Hz. Taking the ideal sine as the input
    # would put that lag into the servo's phase.
    params = dataclasses.replace(DEFAULT_PARAMS, coulomb_n_m=0.0)
    trace = completed(SPEC, params=params, **NOISELESS)
    point = analyse_stepped_sine(trace, SPEC).points[-1]
    assert point.frequency_hz == 8.0
    expected = cmath.phase(linear_response(8.0, params))
    interval_s = 1 / point.commands_per_cycle / point.frequency_hz
    ideal_reference_rad = point.phase_rad - math.pi * point.frequency_hz * interval_s
    assert phase_error_rad(point.phase_rad, expected) < math.radians(1.5)
    assert phase_error_rad(ideal_reference_rad, expected) > math.radians(10)


def test_a_frequency_too_high_for_the_sampling_rate_is_not_measurable():
    # About 100 readings a second during a sine: 15 Hz gets fewer than 7 a cycle.
    spec = SteppedSine(0.0, 1 * DEG, (2.0, 15.0), cycles=8, transient_cycles=2, settle_s=0.5)
    analysis = analyse_stepped_sine(completed(spec), spec)
    low, high = analysis.points
    assert low.gain_db is not None
    assert high.samples_per_cycle < MIN_SAMPLES_PER_CYCLE
    for name in ("gain", "gain_db", "phase_rad", "command_amplitude_rad"):
        assert getattr(high, name) is None
        assert "readings per cycle at 15 Hz" in high.reasons[name]
        assert "cannot measure a frequency response" in high.reasons[name]
    # The sampling it did achieve is still reported.
    assert high.timing.sample_rate_hz == pytest.approx(100, rel=0.1)


def test_on_a_slow_bus_frequencies_with_enough_readings_are_measured_and_the_rest_refused():
    # A 40 Hz bus: a reading and a command each loop, so 20 readings a second.
    spec = SteppedSine(
        0.0, 3 * DEG, (0.5, 1.0, 2.0, 2.5, 3.0, 5.0), cycles=6, transient_cycles=2, settle_s=0.5
    )
    trace = completed(spec, WIDE_BENCH, sample_rate_hz=40.0, **NOISELESS)
    measured = analyse_stepped_sine(trace, spec)
    truth = analyse_stepped_sine(true_trace(trace), spec)
    for point, true in zip(measured.points, truth.points, strict=True):
        if point.samples_per_cycle >= MIN_SAMPLES_PER_CYCLE:
            assert point.gain_db is not None, point.reasons
            assert abs(point.gain_db - true.gain_db) <= 1.0
            assert phase_error_rad(point.phase_rad, true.phase_rad) <= math.radians(5)
        else:
            assert point.gain_db is None
            assert "readings per cycle" in point.reasons["gain_db"]
    assert [p.gain_db is None for p in measured.points] == [False, False, False, True, True, True]


def test_too_few_cycles_after_the_transient_is_not_measurable():
    spec = SteppedSine(0.0, 3 * DEG, (2.0,), cycles=3, transient_cycles=2, settle_s=0.5)
    point = analyse_stepped_sine(completed(spec), spec).points[0]
    assert point.cycles_analysed == 1
    assert point.phase_rad is None
    assert "at least 2 are needed" in point.reasons["phase_rad"]


def test_a_response_of_a_few_reading_steps_is_not_measurable():
    spec = SteppedSine(0.0, 0.3 * DEG, (1.0, 9.0), cycles=6, transient_cycles=2, settle_s=0.5)
    # 0.3 deg is 3.4 reading steps commanded; friction and the servo's roll-off leave less.
    for point in analyse_stepped_sine(completed(spec, seed=1), spec).points:
        assert point.gain_db is None
        assert "reading steps high" in point.reasons["gain_db"]
        assert point.response_amplitude_rad is not None  # what it measured is still there


def test_a_noisy_reading_makes_a_small_response_not_measurable():
    spec = SteppedSine(0.0, 2 * DEG, (1.0,), cycles=4, transient_cycles=1, settle_s=0.5)
    noisy_reading = dict(seed=0, position_noise_rad=1.0 * DEG)
    point = analyse_stepped_sine(completed(spec, **noisy_reading), spec).points[0]
    assert point.phase_rad is None
    assert "too small against the scatter" in point.reasons["phase_rad"]


def test_a_reversed_servo_shows_a_lead_near_180_deg_and_a_warning():
    # A small sine stays inside the monitor's follow check even turning the wrong way.
    spec = SteppedSine(0.0, 3 * DEG, (0.5, 2.0, 6.0), cycles=4, transient_cycles=1, settle_s=0.5)
    result = run(spec, faults=("reversed_direction",))
    assert result.outcome is Outcome.COMPLETED
    healthy = analyse_stepped_sine(completed(spec), spec)
    reversed_ = analyse_stepped_sine(result.trace, spec)
    for point, ok in zip(reversed_.points, healthy.points, strict=True):
        assert point.gain_db == pytest.approx(ok.gain_db, abs=0.3)
        assert phase_error_rad(point.phase_rad, ok.phase_rad + math.pi) < math.radians(3)
    assert math.degrees(reversed_.points[0].phase_rad) > 160
    assert len(reversed_.warnings) == 1
    assert "leads the command by 167 deg" in reversed_.warnings[0]
    assert "reversed direction" in reversed_.warnings[0]
    assert healthy.warnings == ()


def test_a_wrong_zero_shows_as_an_offset_not_in_gain_or_phase():
    spec = SteppedSine(0.0, 3 * DEG, (0.5, 2.0), cycles=4, transient_cycles=1, settle_s=0.5)
    healthy = analyse_stepped_sine(completed(spec), spec)
    offset = analyse_stepped_sine(completed(spec, faults=("wrong_zero",)), spec)
    assert offset.quality.position_before_enable_rad == pytest.approx(20 * DEG, abs=0.2 * DEG)
    for point, ok in zip(offset.points, healthy.points, strict=True):
        assert point.gain_db == pytest.approx(ok.gain_db, abs=0.2)
        assert phase_error_rad(point.phase_rad, ok.phase_rad) < math.radians(1)
        assert abs(ok.response_offset_rad) < 0.05 * DEG
        assert point.response_offset_rad > 0.4 * DEG  # gravity pulls towards where it hangs
    assert offset.warnings == ()


def test_frequencies_after_a_safety_abort_say_the_run_stopped():
    # The overheating servo reaches temperature_max about 11 s in, during the 1 Hz sine.
    spec = SteppedSine(0.0, 3 * DEG, (2.0, 1.0, 0.5), cycles=12, transient_cycles=2, settle_s=0.5)
    result = run(spec, WIDE_BENCH, faults=("overheating",))
    assert result.outcome is Outcome.ABORTED
    first, during, after = analyse_stepped_sine(result.trace, spec).points
    assert first.gain_db is not None
    assert "the run stopped during this frequency" in during.reasons["gain_db"]
    assert "temperature" in during.reasons["gain_db"]
    assert "the run stopped before this frequency started" in after.reasons["gain_db"]


def test_phase_lags_past_minus_180_deg_are_unwrapped():
    # A 30 ms command delay takes the lag past -180 deg by 8 Hz.
    spec = SteppedSine(
        0.0, 4 * DEG, (2.0, 4.0, 6.0, 8.0), cycles=10, transient_cycles=3, settle_s=0.5
    )
    params = dataclasses.replace(DEFAULT_PARAMS, coulomb_n_m=0.0, delay_s=0.03)
    trace = completed(spec, WIDE_BENCH, params=params, **NOISELESS)
    points = analyse_stepped_sine(trace, spec).points
    assert all(point.phase_rad is not None for point in points)
    phases = [point.phase_rad for point in points]
    assert all(b < a for a, b in zip(phases, phases[1:], strict=False))
    assert phases[-1] < -math.pi
    for point in points:
        expected = cmath.phase(linear_response(point.frequency_hz, params))
        assert phase_error_rad(point.phase_rad, expected) <= math.radians(5)
