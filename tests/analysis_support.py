"""Shared by the analysis tests: runs on the simulated servo, and the true answer from the model.

The true answer for a run is the model's own response to the commands that run
actually sent, at the times they were sent: ``model.simulate`` replays them
with the same parameters and load, without noise or quantisation, on the
model's 0.5 ms grid, starting where the simulated servo started. The same
analysis is then applied to that noise-free, finely sampled trace. A metric's
true value is therefore the metric of the model's exact response, under the
same definitions; the tests in ``test_analysis_definitions.py`` check those
definitions against closed-form responses, independently of the model.
"""

from __future__ import annotations

import dataclasses
import math
import re

import numpy as np

from servobench.adapters.sim import SimConfig, SimServo
from servobench.contract import POSITION, Sample
from servobench.experiments.runner import Outcome, RunResult, Trace, run_experiment
from servobench.limits import TEMPLATE, parse_bench
from servobench.model import DEFAULT_DT_S, DEFAULT_PARAMS, PendulumLoad, ServoParams, simulate

DEG = math.radians(1.0)
LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)  # the template's arm
NOISELESS = dict(noise=False, jitter_s=0.0)  # quantisation stays on: it is the sensor


def bench(**settings: str):
    """The template bench file with some settings replaced, each given as TOML text."""
    text = TEMPLATE
    for key, value in settings.items():
        text, count = re.subn(rf"^{key} = .*$", f"{key} = {value}", text, flags=re.MULTILINE)
        assert count == 1, key
    return parse_bench(text.encode())


TEMPLATE_BENCH = bench()
# For servos and experiments outside the template's limits. The template is not changed.
WIDE_BENCH = bench(
    speed_max='"400 deg/s"',
    current_max='"2 A"',
    follow_error_max='"8 deg"',
    experiment_max='"60 s"',
)


def run(
    spec,
    bench_=TEMPLATE_BENCH,
    *,
    params: ServoParams = DEFAULT_PARAMS,
    faults=(),
    **config,
) -> RunResult:
    servo = SimServo(SimConfig(faults=frozenset(faults), **config), params=params, load=LOAD)
    return run_experiment(servo, bench_, spec, rehearsal_params=params)


def completed(spec, bench_=TEMPLATE_BENCH, **kwargs) -> Trace:
    result = run(spec, bench_, **kwargs)
    assert result.outcome is Outcome.COMPLETED, result.summary
    return result.trace


def true_trace(trace: Trace, params: ServoParams = DEFAULT_PARAMS) -> Trace:
    """``trace`` with its readings replaced by the model's exact response every 0.5 ms."""
    times = np.arange(0.0, trace.t_end_s, DEFAULT_DT_S)  # the simulated servo's grid, from 0
    response = simulate(
        params,
        LOAD,
        [command.t_s for command in trace.commands],
        [command.value for command in trace.commands],
        times,
        initial_position_rad=SimConfig().initial_position_rad,
    )
    during = times >= trace.t_start_s
    samples = tuple(
        Sample(float(t_s), {POSITION: float(position_rad)})
        for t_s, position_rad in zip(times[during], response.position_rad[during], strict=True)
    )
    return dataclasses.replace(trace, samples=(trace.samples[0], *samples))
