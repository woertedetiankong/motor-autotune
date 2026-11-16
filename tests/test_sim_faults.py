"""Each injected fault is observable through the adapter contract alone."""

import math

import pytest

from servobench.adapters.sim import SimConfig, SimServo
from servobench.contract import (
    POSITION,
    TEMPERATURE,
    VELOCITY,
    CommandRejected,
    DeviceFaulted,
    NoReply,
)
from servobench.limits import SIM_FAULTS, TEMPLATE, BenchFileError, parse_bench
from servobench.model import PendulumLoad

LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)


def servo_with(*faults, **settings):
    return SimServo(SimConfig(faults=frozenset(faults), **settings), load=LOAD)


def hold(servo, duration_s):
    """Read at the bus rate for ``duration_s``; return the samples."""
    samples = []
    end_s = servo.clock.now_s() + duration_s
    while servo.clock.now_s() < end_s:
        samples.append(servo.read())
    return samples


FIVE_DEG_RAD = math.radians(5.0)


def small_move(servo, move_rad=FIVE_DEG_RAD):
    """Enable, move by ``move_rad``, and return how far the reading changed."""
    servo.enable()
    hold(servo, 0.2)
    start_rad = servo.read([POSITION])[POSITION]
    servo.command(POSITION, start_rad + move_rad)
    samples = hold(servo, 0.5)
    return samples[-1][POSITION] - start_rad, samples


def test_healthy_servo_shows_none_of_the_faults():
    servo = servo_with()
    assert abs(servo.read()[POSITION]) < math.radians(0.5)
    moved_rad, samples = small_move(servo)
    assert moved_rad == pytest.approx(math.radians(5.0), rel=0.2)
    assert max(sample[TEMPERATURE] for sample in samples) < 26.0
    assert servo.read_faults() == ()


def test_reversed_direction_moves_against_the_command():
    moved_rad, _ = small_move(servo_with("reversed_direction"))
    assert moved_rad == pytest.approx(-math.radians(5.0), rel=0.2)


def test_wrong_zero_reads_far_from_zero_at_rest():
    servo = servo_with("wrong_zero")
    # At rest with torque off the arm hangs straight down, which should read 0.
    assert servo.read()[POSITION] == pytest.approx(math.radians(20.0), abs=math.radians(0.5))
    # Moves still go the commanded way and by the commanded amount.
    moved_rad, _ = small_move(servo)
    assert moved_rad == pytest.approx(math.radians(5.0), rel=0.2)


def test_overheating_heats_fast_then_latches_a_fault_and_turns_torque_off():
    servo = servo_with("overheating")
    start_degc = servo.read()[TEMPERATURE]
    servo.enable()
    servo.command(POSITION, math.radians(30.0))
    servo.clock.wait_s(5.0)
    assert servo.read()[TEMPERATURE] > start_degc + 10.0  # a healthy servo barely warms
    assert servo.read_faults() == ()

    servo.clock.wait_s(30.0)  # past the device's own 70 degC cutoff
    faults = servo.read_faults()
    assert [fault.code for fault in faults] == ["overheat"]
    # The device turned torque off by itself: the arm sinks back towards hanging,
    # stopping where gravity no longer beats friction (about 6 deg here).
    assert abs(servo.read()[POSITION]) < math.radians(10.0)
    # The fault stays latched, and the servo refuses to enable, even once cool.
    servo.enter_safe_state()
    servo.clock.wait_s(600.0)
    assert servo.read()[TEMPERATURE] < 30.0
    assert [fault.code for fault in servo.read_faults()] == ["overheat"]
    with pytest.raises(DeviceFaulted):
        servo.enable()
    with pytest.raises(CommandRejected):
        servo.command(POSITION, 0.0)


def test_overheating_shows_at_rest_when_the_servo_starts_hot():
    servo = servo_with("overheating", initial_temperature_degc=75.0)
    assert servo.read()[TEMPERATURE] == pytest.approx(75.0, abs=0.5)
    assert [fault.code for fault in servo.read_faults()] == ["overheat"]


def test_dropped_replies_raise_no_reply_on_about_a_quarter_of_reads():
    servo = servo_with("dropped_replies")
    dropped = 0
    longest_run = run = 0
    for _ in range(1000):
        before_s = servo.clock.now_s()
        try:
            servo.read()
            run = 0
        except NoReply:
            dropped += 1
            run += 1
            longest_run = max(longest_run, run)
            # Waiting for a reply that never comes takes the reply timeout.
            assert servo.clock.now_s() - before_s == pytest.approx(servo.config.reply_timeout_s)
    assert 200 < dropped < 300
    assert longest_run >= 3  # consecutive misses happen, for the monitor to count
    # Commands, enable and the safe state still get through.
    servo.enable()
    servo.command(POSITION, 0.1)
    servo.enter_safe_state()


def test_stuck_position_never_changes_while_the_servo_moves():
    servo = servo_with("stuck_position")
    moved_rad, samples = small_move(servo, math.radians(10.0))
    assert moved_rad == 0.0
    assert len({sample[POSITION] for sample in samples}) == 1
    assert max(abs(sample[VELOCITY]) for sample in samples) > 0.5  # yet the shaft turned


def test_runaway_drives_away_and_ignores_commands_but_torque_off_stops_it():
    servo = servo_with("runaway")
    start_rad = servo.read()[POSITION]
    servo.enable()
    servo.command(POSITION, start_rad)  # accepted, and ignored
    samples = hold(servo, 0.3)
    assert samples[-1][POSITION] - start_rad > math.radians(90.0)
    assert max(sample[VELOCITY] for sample in samples) > math.radians(360.0)
    # Torque off still works: the arm coasts to a stop and sinks to hanging,
    # some whole number of turns from where it started.
    servo.enter_safe_state()
    hold(servo, 5.0)
    sample = servo.read()
    assert abs(sample[VELOCITY]) < math.radians(5.0)
    turns = (sample[POSITION] - start_rad) / (2 * math.pi)
    assert turns >= 1 and abs(turns - round(turns)) < 10.0 / 360.0


def test_faults_are_selected_from_the_bench_file():
    for fault in SIM_FAULTS:
        text = TEMPLATE.replace('# faults = ["wrong_zero"]', f'faults = ["{fault}"]')
        assert parse_bench(text.encode()).sim.faults == (fault,)
        assert SimServo.from_bench(parse_bench(text.encode())).config.faults == {fault}
    text = TEMPLATE.replace('# faults = ["wrong_zero"]', 'faults = ["wrong_zero", "runaway"]')
    assert set(parse_bench(text.encode()).sim.faults) == {"wrong_zero", "runaway"}


def test_unknown_fault_is_rejected():
    with pytest.raises(ValueError):
        SimConfig(faults=frozenset({"gremlins"}))
    text = TEMPLATE.replace('# faults = ["wrong_zero"]', 'faults = ["gremlins"]')
    with pytest.raises(BenchFileError, match="unknown fault 'gremlins'"):
        parse_bench(text.encode())
