"""The model fit's machinery: plan step 8. The full command is tested in test_characterize.py."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from servobench.adapters.sim import SimConfig, SimServo
from servobench.analysis import fit
from servobench.analysis.fit import (
    FitExperiment,
    coordinates_of,
    fit_model,
    noise_floor,
    params_of,
)
from servobench.experiments.runner import run_experiment
from servobench.experiments.spec import Step, SteppedSine
from servobench.limits import TEMPLATE, parse_bench
from servobench.model import DEFAULT_DT_S, DEFAULT_PARAMS, PendulumLoad, simulate

DEG = math.radians(1.0)
LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)
BENCH = parse_bench(TEMPLATE.encode())
Q = 2 * math.pi / 4096


def test_coordinates_and_parameters_convert_both_ways():
    values = coordinates_of(DEFAULT_PARAMS)
    assert values[fit.DAMPING] == pytest.approx(0.09)
    assert values[fit.KD_SHARE] == pytest.approx(0.06 / 0.09)
    again = params_of(values)
    for item in dataclasses.fields(DEFAULT_PARAMS):
        assert getattr(again, item.name) == pytest.approx(getattr(DEFAULT_PARAMS, item.name))


def test_a_direction_the_readings_do_not_see_has_an_infinite_uncertainty():
    jacobian = np.array([[1.0, 0.0, 2.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]])
    covariance = fit._covariance(jacobian, 1.0)
    assert math.isinf(covariance[1, 1])
    assert np.isfinite(covariance[0, 0]) and np.isfinite(covariance[2, 2])


def test_the_noise_floor_is_the_rounding_without_noise_and_the_noise_with_much():
    assert noise_floor([0.0], Q).noise_floor_rad == pytest.approx(Q / math.sqrt(12), rel=0.01)
    assert noise_floor([2 * Q], Q).noise_floor_rad == pytest.approx(2 * Q, rel=1e-6)
    assert noise_floor([0.01], None).noise_floor_rad == pytest.approx(0.01)


def test_the_uncertainty_inflation_sees_a_constant_offset_that_lag_one_correlation_misses():
    rng = np.random.default_rng(0)
    white = rng.normal(size=2000)
    assert fit._inflation(white, [2000]) < 1.5
    # A rounding error that stays constant for 200 readings at a time.
    held = np.repeat(rng.uniform(-0.5, 0.5, size=10), 200) + 0.1 * rng.normal(size=2000)
    assert fit._inflation(held, [2000]) > 20


def _fine(experiment: FitExperiment, params, dt_s: float) -> FitExperiment:
    times = np.concatenate(([experiment.t0_s], experiment.sample_times_s))
    response = simulate(
        params, LOAD, experiment.command_times_s, experiment.command_positions_rad, times,
        initial_position_rad=experiment.initial_position_rad,
        initial_setpoint_rad=experiment.initial_position_rad, dt_s=dt_s,
    )  # fmt: skip
    return dataclasses.replace(experiment, positions_rad=response.position_rad[1:])


def test_a_fit_through_the_model_is_unbiased_against_a_continuous_time_servo():
    # The plan-step-8 review finding: a fit through the 0.5 ms model, against
    # readings from a servo integrated 16 times more finely (as close to
    # continuous time as matters here), without noise or rounding. With the
    # backward Euler of step 7 the damping came out 1 % low and the delay 6 %
    # high on this servo.
    servo = SimServo(
        SimConfig(noise=False, jitter_s=0.0, position_steps_per_turn=None), load=LOAD
    )
    experiments = []
    for spec in (
        Step(0.0, (10 * DEG, -10 * DEG), 1.0, 0.5),
        SteppedSine(0.0, 1.5 * DEG, (2.0, 4.0, 8.0), 3, 1, 0.5),
    ):
        result = run_experiment(servo, BENCH, spec)
        reference = FitExperiment.from_trace(spec.kind, result.trace)
        experiments.append(_fine(reference, DEFAULT_PARAMS, DEFAULT_DT_S / 16))
    truth = coordinates_of(DEFAULT_PARAMS)
    result = fit_model(experiments, LOAD, starts=[truth])
    for name in (fit.KP, fit.DAMPING, fit.INERTIA, fit.COULOMB, fit.DELAY):
        assert result.best[name] == pytest.approx(truth[name], rel=0.005), name
