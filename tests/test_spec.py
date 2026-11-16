"""Experiment specs: well formed, checked against the bench limits, and planned."""

import math

import pytest

from servobench.experiments.spec import (
    APPROACH,
    SETTLE,
    SINE,
    STEP,
    Hold,
    SpecError,
    Step,
    SteppedSine,
    build_plan,
    spec_from_dict,
)
from servobench.limits import TEMPLATE, parse_bench

LIMITS = parse_bench(TEMPLATE.encode()).limits  # travel +-90 deg, margin 10 deg
DEG = math.radians(1.0)


def test_step_segments_go_out_and_back_for_each_amplitude():
    spec = Step(centre_rad=5 * DEG, amplitudes_rad=(10 * DEG, -10 * DEG), dwell_s=1.0, settle_s=0.5)
    segments = spec.segments()
    assert [segment.kind for segment in segments] == [SETTLE, STEP, STEP, STEP, STEP]
    assert [segment.position_rad / DEG for segment in segments] == pytest.approx([5, 15, 5, -5, 5])
    assert [segment.from_position_rad for segment in segments[1:]] == pytest.approx(
        [5 * DEG, 15 * DEG, 5 * DEG, -5 * DEG]
    )
    assert spec.planned_duration_s == pytest.approx(4.5)
    assert spec.commanded_range_rad == pytest.approx((-5 * DEG, 15 * DEG))
    assert spec.check(LIMITS) == []


def test_stepped_sine_runs_whole_cycles_and_ends_at_the_centre():
    spec = SteppedSine(
        centre_rad=0.0,
        amplitude_rad=10 * DEG,
        frequencies_hz=(0.5, 2.0),
        cycles=4,
        transient_cycles=1,
        settle_s=0.5,
    )
    settle, slow, fast = spec.segments()
    assert settle.kind == SETTLE and slow.kind == fast.kind == SINE
    assert slow.duration_s == pytest.approx(8.0) and fast.duration_s == pytest.approx(2.0)
    assert slow.setpoint_rad(0.5) == pytest.approx(10 * DEG)  # a quarter cycle at 0.5 Hz
    assert fast.setpoint_rad(fast.duration_s) == pytest.approx(0.0, abs=1e-12)
    assert spec.peak_command_speed_rad_s == pytest.approx(10 * DEG * 2 * math.pi * 2.0)
    assert spec.check(LIMITS) == []


def test_commanded_range_outside_the_travel_less_margin_is_a_problem():
    # 85 deg is inside the 90 deg travel but not inside the 80 deg command range.
    problems = Step(0.0, (85 * DEG,), 1.0, 0.5).check(LIMITS)
    assert len(problems) == 1
    assert "outside the allowed command range -80 deg to 80 deg" in problems[0]
    assert Hold(-81 * DEG, 1.0).check(LIMITS) != []
    assert Hold(-80 * DEG, 1.0).check(LIMITS) == []
    sine = SteppedSine(75 * DEG, 10 * DEG, (0.5,), 2, 1, 0.5)  # peaks at 85 deg
    assert "outside the allowed command range" in sine.check(LIMITS)[0]


def test_experiment_longer_than_experiment_max_is_a_problem():
    assert "longer than experiment_max 30 s" in Hold(0.0, 31.0).check(LIMITS)[0]
    assert Hold(0.0, 30.0).check(LIMITS) == []


def test_sine_commanded_faster_than_speed_max_is_a_problem():
    # 20 deg at 2 Hz commands up to 251 deg/s; speed_max is 180 deg/s.
    problems = SteppedSine(0.0, 20 * DEG, (0.5, 2.0), 2, 1, 0.5).check(LIMITS)
    assert any("faster than speed_max 180 deg/s" in problem for problem in problems)


@pytest.mark.parametrize(
    "make",
    [
        lambda: Hold(math.nan, 1.0),
        lambda: Hold(0.0, 0.0),
        lambda: Hold(True, 1.0),
        lambda: Hold("10 deg", 1.0),
        lambda: Hold(0.0, 1.0, sample_rate_hz=0.0),
        lambda: Step(0.0, (), 1.0, 0.5),
        lambda: Step(0.0, (0.0,), 1.0, 0.5),
        lambda: Step(0.0, "0.1", 1.0, 0.5),
        lambda: Step(0.0, (math.inf,), 1.0, 0.5),
        lambda: Step(0.0, (0.1,), -1.0, 0.5),
        lambda: SteppedSine(0.0, 0.1, (0.0,), 2, 1, 0.5),
        lambda: SteppedSine(0.0, -0.1, (1.0,), 2, 1, 0.5),
        lambda: SteppedSine(0.0, 0.1, (1.0,), 2, 2, 0.5),
        lambda: SteppedSine(0.0, 0.1, (1.0,), 2.5, 1, 0.5),
    ],
)
def test_malformed_specs_are_rejected(make):
    with pytest.raises(SpecError):
        make()


def test_spec_round_trips_through_plain_data():
    for spec in (
        Hold(0.1, 2.0),
        Step(0.0, (0.1, -0.2), 1.0, 0.5, sample_rate_hz=100.0),
        SteppedSine(0.0, 0.1, (0.5, 1.0), 3, 1, 0.5),
    ):
        data = spec.to_dict()
        assert data["type"] == spec.kind
        assert spec_from_dict(data) == spec
    with pytest.raises(SpecError):
        spec_from_dict({"type": "jump"})


def test_plan_adds_a_ramp_from_where_the_actuator_is():
    spec = Step(0.0, (10 * DEG,), 1.0, 0.5)
    plan = build_plan(spec, 20 * DEG, approach_speed_rad_s=20 * DEG)
    approach = plan[0]
    assert approach.kind == APPROACH
    assert approach.duration_s == pytest.approx(1.0)
    assert approach.setpoint_rad(0.0) == pytest.approx(20 * DEG)
    assert approach.setpoint_rad(0.5) == pytest.approx(10 * DEG)
    assert approach.setpoint_rad(5.0) == pytest.approx(0.0)
    assert plan[1:] == spec.segments()
    assert build_plan(spec, 0.0, 20 * DEG) == spec.segments()  # already there: no ramp
