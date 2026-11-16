"""Step-response analysis on the simulated servo, against the model's true answer.

The true answer is the same analysis applied to the model's exact, noise-free
response to the commands the run sent (``analysis_support.true_trace``).
"Without noise" means the simulator's measurement noise and timing jitter are
off; the reading is still quantised to 4096 steps per turn, because that is
the sensor.

Tolerances are the plan's (step 6), except two that the plan does not give or
that the readings cannot support, both reported to the owner:

- Settling time: the plan's 5 % (10 % with noise), or the uncertainty the
  analysis reports for it if that is larger. A settling time is set by where
  the response last crosses the edge of a 2 % band, and a response that runs
  along that edge (the default servo's 3 % overshoot) or approaches it
  slowly (an overdamped one) moves its crossing by 10 % for a level error of
  one reading step. Every reported settling time must also be within its own
  reported uncertainty.
- Steady-state error: half a reading step (one with noise), the most a
  quantised reading of a constant can be off.
"""

from __future__ import annotations

import dataclasses
import math

import pytest
from analysis_support import (
    DEG,
    NOISELESS,
    TEMPLATE_BENCH,
    WIDE_BENCH,
    completed,
    run,
    true_trace,
)

from servobench.analysis import analyse_step
from servobench.experiments.runner import Outcome
from servobench.experiments.spec import Step
from servobench.model import DEFAULT_PARAMS

RESOLUTION_RAD = 2 * math.pi / 4096
NOISY_SEEDS = range(10)

# (name, servo parameters, bench, spec)
CASES = [
    (
        "default",
        DEFAULT_PARAMS,
        TEMPLATE_BENCH,
        Step(0.0, (10 * DEG, -10 * DEG, 14 * DEG), dwell_s=1.0, settle_s=0.5),
    ),
    (
        "underdamped",
        dataclasses.replace(DEFAULT_PARAMS, kd_n_m_s_rad=0.02),
        WIDE_BENCH,
        Step(0.0, (10 * DEG, -14 * DEG, 20 * DEG), dwell_s=1.5, settle_s=0.5),
    ),
    (
        "overdamped",
        dataclasses.replace(DEFAULT_PARAMS, kd_n_m_s_rad=0.2),
        WIDE_BENCH,
        Step(0.0, (10 * DEG, -14 * DEG, 20 * DEG), dwell_s=1.5, settle_s=0.5),
    ),
]


def pairs(case, noisy: bool):
    """(measured, true) for every step of the case's runs."""
    _, params, bench, spec = case
    runs = [dict(seed=seed) for seed in NOISY_SEEDS] if noisy else [NOISELESS]
    out = []
    for config in runs:
        trace = completed(spec, bench, params=params, **config)
        measured = analyse_step(trace, spec)
        truth = analyse_step(true_trace(trace, params), spec)
        assert measured.warnings == ()
        for step, true in zip(measured.steps, truth.steps, strict=True):
            assert all(getattr(true, name) is not None for name in TIMES + ("overshoot_percent",))
            out.append((step, true))
    return out


TIMES = ("delay_s", "rise_time_s", "settling_time_s")


@pytest.mark.parametrize("noisy", [False, True], ids=["without noise", "with noise and jitter"])
@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_step_metrics_match_the_model_within_the_plan_tolerances(case, noisy):
    factor = 2 if noisy else 1
    results = pairs(case, noisy)
    for step, true in results:
        where = f"{case[0]}, {step.label}"
        assert step.delay_s is not None, step.reasons
        assert step.rise_time_s is not None, step.reasons
        period_s = step.timing.interval_median_s
        assert abs(step.delay_s - true.delay_s) <= factor * period_s, where
        assert abs(step.rise_time_s - true.rise_time_s) <= factor * 0.05 * true.rise_time_s, where
        assert abs(step.overshoot_percent - true.overshoot_percent) <= factor * 1.0, where
        error = abs(step.steady_state_error_rad - true.steady_state_error_rad)
        assert error <= factor * 0.5 * RESOLUTION_RAD, where


@pytest.mark.parametrize("noisy", [False, True], ids=["without noise", "with noise and jitter"])
@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_settling_time_is_within_its_reported_uncertainty_when_reported(case, noisy):
    # Proposed tolerance (see the module text): the plan's 5 % (10 % with noise)
    # or the reported uncertainty, whichever is larger.
    factor = 2 if noisy else 1
    results = pairs(case, noisy)
    reported = [(step, true) for step, true in results if step.settling_time_s is not None]
    for step, _ in results:
        if step.settling_time_s is None:
            assert step.reasons["settling_time_s"]
    # Not a vacuous pass: most settling times are reported.
    assert len(reported) >= 0.5 * len(results)
    for step, true in reported:
        error = abs(step.settling_time_s - true.settling_time_s)
        uncertainty = step.uncertainties["settling_time_s"]
        assert error <= uncertainty, f"{case[0]}, {step.label}"
        assert error <= max(factor * 0.05 * true.settling_time_s, uncertainty)


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_with_an_unquantised_reading_settling_time_meets_the_plan_tolerance(case):
    # The shortfall against the plan's 5 % is the reading's resolution, not the analysis.
    _, params, bench, spec = case
    trace = completed(spec, bench, params=params, position_steps_per_turn=None, **NOISELESS)
    measured = analyse_step(trace, spec)
    truth = analyse_step(true_trace(trace, params), spec)
    for step, true in zip(measured.steps, truth.steps, strict=True):
        assert abs(step.settling_time_s - true.settling_time_s) <= 0.05 * true.settling_time_s


def test_a_settling_time_the_reading_resolution_cannot_fix_is_refused_not_reported():
    # The default servo's 10 deg step back to the centre overshoots by 3.2 %
    # against the 2 % band: its settling time moves by about 12 ms per half
    # reading step. Without noise the quantised final position is 0.43 reading
    # steps off, which would put the settling time 9 % early.
    spec = Step(0.0, (10 * DEG,), dwell_s=1.0, settle_s=0.5)
    trace = completed(spec, **NOISELESS)
    back = analyse_step(trace, spec).steps[1]
    assert back.settling_time_s is None
    assert "edge of the settling band" in back.reasons["settling_time_s"]
    assert analyse_step(true_trace(trace), spec).steps[1].settling_time_s == pytest.approx(
        0.199, abs=0.001
    )


def test_a_reversed_servo_moves_the_wrong_way_and_gets_no_step_metrics():
    # 4 deg steps stay inside follow_error_max (5 deg), so the monitor lets the run finish.
    spec = Step(0.0, (4 * DEG, -4 * DEG), dwell_s=1.0, settle_s=0.5)
    result = run(spec, faults=("reversed_direction",))
    assert result.outcome is Outcome.COMPLETED
    analysis = analyse_step(result.trace, spec)
    assert len(analysis.steps) == 4
    for step in analysis.steps:
        assert step.step_ratio == pytest.approx(-1.0, abs=0.05)
        for name in ("delay_s", "rise_time_s", "overshoot_percent", "settling_time_s"):
            assert getattr(step, name) is None
            assert "opposite way to the command" in step.reasons[name]
    assert len(analysis.warnings) == 1
    assert "4 of 4 measured steps moved opposite" in analysis.warnings[0]


def test_a_wrong_zero_shows_in_the_rest_reading_and_the_steady_state_errors():
    # The servo follows its commands normally; only where it rests and which
    # way gravity pulls it show that its zero is 20 deg from where the arm hangs.
    spec = Step(0.0, (4 * DEG, -4 * DEG), dwell_s=1.0, settle_s=0.5)
    healthy = analyse_step(completed(spec), spec)
    offset = analyse_step(completed(spec, faults=("wrong_zero",)), spec)
    assert abs(healthy.quality.position_before_enable_rad) < 0.2 * DEG
    assert offset.quality.position_before_enable_rad == pytest.approx(20 * DEG, abs=0.2 * DEG)
    # Healthy: gravity pulls each step towards zero, so the errors change sign with the side.
    assert {math.copysign(1, s.steady_state_error_rad) for s in healthy.steps} == {-1.0, 1.0}
    # Offset: the arm really hangs at +20 deg on the reading, so it settles above every command.
    assert all(s.steady_state_error_rad < -0.3 * DEG for s in offset.steps)
    assert offset.warnings == ()


def test_a_step_too_small_for_the_reading_is_not_measured():
    spec = Step(0.0, (1 * DEG,), dwell_s=1.0, settle_s=0.5)  # 11 reading steps
    step = analyse_step(completed(spec, **NOISELESS), spec).steps[0]
    assert step.final_position_rad is not None
    assert step.steady_state_error_rad is not None
    for name in ("delay_s", "rise_time_s", "overshoot_percent", "settling_time_s"):
        assert getattr(step, name) is None
        assert "times the readings' uncertainty" in step.reasons[name]


def test_a_step_whose_band_is_inside_the_reading_uncertainty_gets_no_settling_time():
    spec = Step(0.0, (3 * DEG,), dwell_s=1.0, settle_s=0.5)  # 34 reading steps: u = 2.9 %
    step = analyse_step(completed(spec, **NOISELESS), spec).steps[0]
    assert step.delay_s is not None and step.rise_time_s is not None
    assert step.settling_time_s is None
    assert "as much as the" in step.reasons["settling_time_s"]


def test_with_noise_a_small_step_gets_no_settling_time():
    # 5 deg is 57 reading steps; the 2 % band is 1.1 of them, against readings
    # that scatter by about one. Every seed refuses, for one of two reasons.
    spec = Step(0.0, (5 * DEG,), dwell_s=1.0, settle_s=0.5)
    reasons = []
    for seed in range(5):
        step = analyse_step(completed(spec, seed=seed), spec).steps[0]
        assert step.settling_time_s is None
        reasons.append(step.reasons["settling_time_s"])
    assert any("not steadily inside" in reason for reason in reasons)
    assert all("not steadily inside" in r or "edge of the settling band" in r for r in reasons)


def test_a_step_that_has_not_settled_by_the_end_of_its_dwell_is_not_measured():
    spec = Step(0.0, (10 * DEG,), dwell_s=0.15, settle_s=0.5)
    step = analyse_step(completed(spec, **NOISELESS), spec).steps[0]
    assert step.final_position_rad is None
    assert "still changing at the end of the step" in step.reasons["final_position_rad"]
    assert "dwell_s" in step.reasons["final_position_rad"]
    assert step.reasons["overshoot_percent"] == step.reasons["final_position_rad"]


def test_a_dwell_too_short_to_hold_readings_is_not_measured():
    spec = Step(0.0, (10 * DEG,), dwell_s=0.02, settle_s=0.5)
    step = analyse_step(completed(spec, **NOISELESS), spec).steps[0]
    assert step.final_position_rad is None
    assert "too few to fix the position" in step.reasons["final_position_rad"]


def test_steps_after_a_safety_abort_say_the_run_stopped():
    spec = Step(0.0, (10 * DEG, -10 * DEG), dwell_s=6.0, settle_s=0.5)
    result = run(spec, faults=("overheating",))
    assert result.outcome is Outcome.ABORTED
    steps = analyse_step(result.trace, spec).steps
    stopped = [step for step in steps if step.final_position_rad is None]
    assert stopped
    assert "the run stopped" in stopped[0].reasons["final_position_rad"]
    assert "temperature" in stopped[0].reasons["final_position_rad"]
    assert "before this step was commanded" in stopped[-1].reasons["t_command_s"]
    assert steps[0].delay_s is not None  # the steps before the abort are still measured


def test_a_crossing_inside_a_gap_in_the_readings_is_not_measured():
    spec = Step(0.0, (10 * DEG,), dwell_s=1.0, settle_s=0.5)
    trace = completed(spec, **NOISELESS)
    step = analyse_step(trace, spec).steps[0]
    t_half_s = step.t_command_s + step.delay_s
    # Lose the readings for 30 ms around the 50 % crossing, as missed replies would.
    kept = tuple(s for s in trace.samples if not abs(s.t_s - t_half_s) < 0.015)
    gapped = analyse_step(dataclasses.replace(trace, samples=kept), spec).steps[0]
    assert gapped.delay_s is None
    assert "readings are missing where the response crossed 50%" in gapped.reasons["delay_s"]
    assert gapped.settling_time_s is not None


def test_uneven_timing_with_heavy_jitter_still_meets_the_noisy_tolerances():
    # Bus transactions with an extra delay of scale 1.5 ms: intervals from 5 to about 10 ms.
    # The wide bench: with this much jitter the monitor's speed estimate on the 14 deg
    # step passes the template's 180 deg/s, as a speed check on noisy timestamps should.
    spec = Step(0.0, (10 * DEG, 14 * DEG), dwell_s=1.0, settle_s=0.5)
    for seed in range(5):
        trace = completed(spec, WIDE_BENCH, seed=seed, jitter_s=1.5e-3)
        measured = analyse_step(trace, spec)
        assert measured.quality.timing.jitter_s > 0.5e-3
        truth = analyse_step(true_trace(trace), spec)
        for step, true in zip(measured.steps, truth.steps, strict=True):
            assert abs(step.delay_s - true.delay_s) <= 2 * step.timing.interval_median_s
            assert abs(step.rise_time_s - true.rise_time_s) <= 0.10 * true.rise_time_s
            assert abs(step.overshoot_percent - true.overshoot_percent) <= 2.0


def test_a_trace_from_another_spec_is_refused():
    spec = Step(0.0, (10 * DEG,), dwell_s=1.0, settle_s=0.5)
    trace = completed(spec, **NOISELESS)
    with pytest.raises(ValueError, match="not those of this spec"):
        analyse_step(trace, Step(0.0, (12 * DEG,), dwell_s=1.0, settle_s=0.5))
