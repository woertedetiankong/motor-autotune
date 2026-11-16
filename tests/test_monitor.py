"""The safety monitor's checks, one at a time, on made-up samples."""

import math

import pytest

from servobench.contract import (
    CURRENT,
    POSITION,
    TEMPERATURE,
    VELOCITY,
    VOLTAGE,
    Fault,
    Sample,
)
from servobench.experiments import monitor as checks
from servobench.experiments.monitor import Monitor
from servobench.experiments.spec import command_band_rad
from servobench.limits import TEMPLATE, parse_bench

BENCH = parse_bench(TEMPLATE.encode())
DEG = math.radians(1.0)
PERIOD_S = 0.005


def sample(t_s, position_rad=0.0, **values):
    readings = {
        POSITION: position_rad,
        VELOCITY: 0.0,
        CURRENT: 0.05,
        VOLTAGE: 7.4,
        TEMPERATURE: 30.0,
    }
    readings.update(values)
    return Sample(t_s=t_s, values=readings)


def started_monitor(hold_rad=0.0):
    monitor = Monitor(BENCH.limits, BENCH.runner)
    assert monitor.on_sample(sample(0.0, hold_rad)) is None
    monitor.hold(0.0, hold_rad)
    monitor.start(0.0)
    return monitor


def test_healthy_readings_pass():
    monitor = started_monitor()
    for index in range(1, 400):
        assert monitor.on_sample(sample(index * PERIOD_S, 0.0)) is None
    assert monitor.check_time(2.0) is None


@pytest.mark.parametrize(
    ("values", "kind"),
    [
        ({POSITION: 91 * DEG}, checks.POSITION_LIMIT),
        ({POSITION: -91 * DEG}, checks.POSITION_LIMIT),
        ({VELOCITY: 181 * DEG}, checks.SPEED_LIMIT),
        ({VELOCITY: -181 * DEG}, checks.SPEED_LIMIT),
        ({CURRENT: 0.51}, checks.CURRENT_LIMIT),
        ({CURRENT: -0.51}, checks.CURRENT_LIMIT),
        ({TEMPERATURE: 60.1}, checks.TEMPERATURE_LIMIT),
        ({VOLTAGE: 5.9}, checks.VOLTAGE_LIMIT),
        ({VOLTAGE: 8.5}, checks.VOLTAGE_LIMIT),
        ({TEMPERATURE: math.nan}, checks.BAD_READING),
        ({VELOCITY: math.inf}, checks.BAD_READING),
    ],
)
def test_each_limit_is_checked_on_every_sample(values, kind):
    monitor = Monitor(BENCH.limits, BENCH.runner)
    position_rad = values.pop(POSITION, 0.0)
    violation = monitor.on_sample(sample(0.0, position_rad, **values))
    assert violation is not None and violation.kind == kind
    assert violation.message


def test_values_at_the_limit_pass():
    monitor = Monitor(BENCH.limits, BENCH.runner)
    limits = BENCH.limits
    at_limits = sample(
        0.0,
        limits.position_max_rad,
        **{
            VELOCITY: limits.speed_max_rad_s,
            CURRENT: limits.current_max_a,
            TEMPERATURE: limits.temperature_max_degc,
            VOLTAGE: limits.voltage_min_v,
        },
    )
    assert monitor.on_sample(at_limits) is None


def test_a_missing_signal_is_a_bad_reading():
    monitor = Monitor(BENCH.limits, BENCH.runner)
    violation = monitor.on_sample(Sample(t_s=0.0, values={POSITION: 0.0}))
    assert violation.kind == checks.BAD_READING


def test_speed_is_also_worked_out_from_position():
    monitor = started_monitor()
    # The velocity reading says 0, but the position moves at 200 deg/s.
    for index in range(1, 20):
        t_s = index * PERIOD_S
        violation = monitor.on_sample(sample(t_s, 200 * DEG * t_s))
        if violation is not None:
            break
    assert violation.kind == checks.SPEED_LIMIT
    assert "change in position" in violation.message
    assert violation.value == pytest.approx(200 * DEG)


def test_quantisation_steps_do_not_look_like_speed():
    monitor = started_monitor()
    step_rad = 2 * math.pi / 4096
    for index in range(1, 400):  # a reading flickering by one step every sample
        assert monitor.on_sample(sample(index * PERIOD_S, step_rad * (index % 2))) is None


def test_a_servo_lagging_behind_a_step_is_following():
    monitor = started_monitor()
    monitor.on_command(0.1, 30 * DEG)
    # It takes 0.8 s to get there, linearly, then overshoots by 3 deg.
    for index in range(1, 300):
        t_s = 0.1 + index * PERIOD_S
        position_rad = min(30 * DEG * (t_s - 0.1) / 0.8, 30 * DEG) + (3 * DEG if t_s > 0.9 else 0)
        assert monitor.on_sample(sample(t_s, position_rad, **{VELOCITY: 0.6})) is None


def test_a_servo_moving_the_wrong_way_is_not_following():
    monitor = started_monitor()
    monitor.on_command(0.1, 10 * DEG)
    violation = None
    for index in range(1, 400):
        t_s = 0.1 + index * PERIOD_S
        violation = monitor.on_sample(sample(t_s, -0.1 * (t_s - 0.1), **{VELOCITY: -0.1}))
        if violation is not None:
            break
    assert violation.kind == checks.NOT_FOLLOWING
    assert violation.value > 5 * DEG


def test_a_servo_that_never_arrives_is_not_following_after_follow_time_max():
    monitor = started_monitor()
    monitor.on_command(0.1, 10 * DEG)
    # Stuck 6 deg short: inside the window the old command (0) still counts.
    t_s = 0.1
    while True:
        t_s += PERIOD_S
        violation = monitor.on_sample(sample(t_s, 4 * DEG + 0.001 * (t_s % 0.01)))
        if violation is not None:
            break
    assert violation.kind == checks.NOT_FOLLOWING
    assert t_s == pytest.approx(1.1, abs=2 * PERIOD_S)


def test_a_frozen_reading_is_caught_while_commands_sweep_around_it():
    monitor = started_monitor()
    t_s = 0.0
    violation = None
    while violation is None and t_s < 5.0:
        t_s += PERIOD_S
        monitor.on_command(t_s, 10 * DEG * math.sin(2 * math.pi * 2.0 * t_s))
        violation = monitor.on_sample(sample(t_s, 0.0))  # exactly the same, every time
    assert violation.kind == checks.FROZEN_READING
    # The commands passed 5 deg away from the start about 0.04 s in.
    assert t_s == pytest.approx(1.0 + 0.04, abs=0.02)


def test_a_reading_that_changes_is_not_frozen():
    monitor = started_monitor()
    t_s = 0.0
    while t_s < 5.0:
        t_s += PERIOD_S
        command_rad = 10 * DEG * math.sin(2 * math.pi * 2.0 * t_s)
        monitor.on_command(t_s, command_rad)
        # Heavily attenuated but still moving: one quantisation step at a time.
        reading_rad = round(0.1 * command_rad / (2 * math.pi / 4096)) * (2 * math.pi / 4096)
        assert monitor.on_sample(sample(t_s, reading_rad)) is None


def test_missed_replies_are_counted_in_a_row():
    monitor = started_monitor()
    for _ in range(3):
        assert monitor.on_missed(0.1, "NoReply") is None
    assert monitor.on_sample(sample(0.2)) is None  # a good reply resets the count
    for _ in range(3):
        assert monitor.on_missed(0.3, "NoReply") is None
    violation = monitor.on_missed(0.4, "NoReply")
    assert violation.kind == checks.MISSED_REPLIES
    assert "4 reads in a row" in violation.message


def readings_until(monitor, end_s, start_s=PERIOD_S):
    """Healthy readings every period up to ``end_s``, so the monitor is never blind."""
    for index in range(round(start_s / PERIOD_S), round(end_s / PERIOD_S) + 1):
        assert monitor.on_sample(sample(index * PERIOD_S)) is None


def test_device_faults_and_duration():
    monitor = started_monitor()
    readings_until(monitor, 0.5)
    assert monitor.on_faults(0.5, ()) is None
    violation = monitor.on_faults(0.5, (Fault("overheat", "too hot"),))
    assert violation.kind == checks.DEVICE_FAULT and "overheat" in violation.message
    readings_until(monitor, 30.0, start_s=0.5 + PERIOD_S)
    assert monitor.check_time(30.0) is None
    assert monitor.check_time(30.01).kind == checks.DURATION


def test_commands_outside_the_band_are_not_allowed_out():
    monitor = started_monitor()
    low_rad, high_rad = command_band_rad(BENCH.limits)  # -80 deg to 80 deg
    assert monitor.check_command(0.0, high_rad) is None
    assert monitor.check_command(0.0, low_rad) is None
    assert monitor.check_command(0.0, high_rad + 0.1 * DEG).kind == checks.COMMAND_OUT_OF_RANGE
    assert monitor.check_command(0.0, low_rad - 0.1 * DEG).kind == checks.COMMAND_OUT_OF_RANGE
    assert monitor.check_command(0.0, math.nan).kind == checks.COMMAND_OUT_OF_RANGE


# == Regressions from the outside review, 2026-11-16 ==================================


def test_a_fault_poll_does_not_reset_the_count_of_missed_reads():
    monitor = started_monitor()
    for t_s in (0.01, 0.02, 0.03):
        assert monitor.on_missed(t_s, "NoReply") is None
    assert monitor.on_faults(0.035, ()) is None  # answered, but it is not a reading
    violation = monitor.on_missed(0.04, "NoReply")
    assert violation is not None and violation.kind == checks.MISSED_REPLIES


def test_a_good_reading_does_not_reset_the_count_of_missed_fault_polls():
    monitor = started_monitor()
    for poll in range(1, 4):
        readings_until(monitor, poll * 0.5, start_s=(poll - 1) * 0.5 + PERIOD_S)
        assert monitor.on_missed_poll(poll * 0.5, "NoReply") is None
    readings_until(monitor, 2.0, start_s=1.5 + PERIOD_S)
    violation = monitor.on_missed_poll(2.0, "NoReply")
    assert violation.kind == checks.MISSED_REPLIES and "4 fault polls in a row" in violation.message


def test_the_time_without_a_good_reading_is_bounded_whatever_a_failed_read_costs():
    monitor = started_monitor()  # the last good reading at 0 s
    gap_s = BENCH.runner.sample_gap_max_s
    # One slow failed read, then an answered fault poll: neither is a reading.
    assert monitor.on_missed(0.6 * gap_s, "NoReply") is None
    assert monitor.on_faults(0.8 * gap_s, ()) is None
    assert monitor.check_time(gap_s) is None
    violation = monitor.check_time(gap_s + 0.001)
    assert violation.kind == checks.SAMPLE_GAP
    assert violation.value == pytest.approx(gap_s + 0.001) and violation.limit == gap_s
    # A slow missed read on its own trips it too, with one miss, far below the count.
    monitor = started_monitor()
    assert monitor.on_missed(gap_s + 0.001, "NoReply").kind == checks.SAMPLE_GAP
    # And a good reading that ends a gap that was already too long.
    monitor = started_monitor()
    assert monitor.on_sample(sample(gap_s + 0.001)).kind == checks.SAMPLE_GAP


def test_before_enable_only_the_count_of_missed_replies_applies():
    monitor = Monitor(BENCH.limits, BENCH.runner)  # a preflight: nothing enabled
    assert monitor.on_missed(10.0, "NoReply") is None
    assert monitor.check_time(20.0) is None


def test_a_plan_that_ends_without_a_reading_after_enable_was_not_watched():
    monitor = started_monitor()
    assert monitor.on_missed(0.02, "NoReply") is None
    assert monitor.check_end(0.05).kind == checks.SAMPLE_GAP
    monitor = started_monitor()
    assert monitor.on_sample(sample(0.01)) is None
    assert monitor.check_end(0.05) is None
