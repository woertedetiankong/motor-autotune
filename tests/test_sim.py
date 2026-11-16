"""The simulated servo: qualitative behaviour, determinism, speed, bench settings."""

import dataclasses
import itertools
import math
import re
import statistics
import time

import pytest

from servobench.adapters.sim import TURN_RAD, SimConfig, SimServo
from servobench.clock import VirtualClock
from servobench.contract import (
    CURRENT,
    POSITION,
    TEMPERATURE,
    VELOCITY,
    VOLTAGE,
    PersistentWriteConfirmation,
)
from servobench.limits import TEMPLATE, SimSettings, parse_bench
from servobench.model import DEFAULT_PARAMS, PendulumLoad

LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)
EXACT = SimConfig(noise=False, jitter_s=0.0, position_steps_per_turn=None)


def step_response(servo, amplitude_rad, duration_s=1.0):
    """Enable at rest, step by ``amplitude_rad``, read at the full bus rate."""
    servo.enable()
    servo.clock.wait_s(0.1)
    servo.command(POSITION, amplitude_rad)
    samples = []
    end_s = servo.clock.now_s() + duration_s
    while servo.clock.now_s() < end_s:
        samples.append(servo.read())
    return samples


def overshoot(samples, amplitude_rad):
    peak = max(sample[POSITION] for sample in samples)
    final = statistics.fmean(sample[POSITION] for sample in samples[-20:])
    return (peak - final) / amplitude_rad


def peak_acceleration_rad_s2(samples):
    return max(
        abs(b[VELOCITY] - a[VELOCITY]) / (b.t_s - a.t_s) for a, b in itertools.pairwise(samples)
    )


# -- the "Done when" checks of plan step 3 ---------------------------------------


def test_higher_proportional_gain_gives_more_overshoot():
    amplitude_rad = math.radians(10.0)
    overshoots = []
    for kp in (1.8, 3.6, 7.2, 14.4):
        servo = SimServo(
            EXACT, params=dataclasses.replace(DEFAULT_PARAMS, kp_n_m_rad=kp), load=LOAD
        )
        overshoots.append(overshoot(step_response(servo, amplitude_rad), amplitude_rad))
    assert overshoots == sorted(overshoots)
    assert overshoots[-1] > overshoots[0] + 0.2


def test_torque_limit_caps_acceleration():
    def peak(amplitude_deg, torque_max_n_m=DEFAULT_PARAMS.torque_max_n_m):
        params = dataclasses.replace(DEFAULT_PARAMS, torque_max_n_m=torque_max_n_m)
        servo = SimServo(EXACT, params=params, load=LOAD)
        return peak_acceleration_rad_s2(step_response(servo, math.radians(amplitude_deg), 0.3))

    inertia_kg_m2 = DEFAULT_PARAMS.inertia_kg_m2 + LOAD.inertia_kg_m2
    cap_rad_s2 = DEFAULT_PARAMS.torque_max_n_m / inertia_kg_m2
    # Small steps: below the limit, acceleration grows with the step.
    assert peak(10.0) == pytest.approx(2 * peak(5.0), rel=0.1)
    assert peak(10.0) < 0.7 * cap_rad_s2
    # Large steps: the torque limit holds acceleration at one value however large
    # the step, a little under torque_max / inertia because friction also acts.
    capped = [peak(amplitude_deg) for amplitude_deg in (60.0, 90.0, 120.0)]
    assert max(capped) == pytest.approx(min(capped), rel=0.01)
    assert 0.85 * cap_rad_s2 < capped[0] <= cap_rad_s2
    # Halve the limit and the cap halves.
    halved = peak(90.0, torque_max_n_m=0.5)
    assert 0.85 * cap_rad_s2 / 2 < halved <= cap_rad_s2 / 2


def test_temperature_rises_under_sustained_effort_and_falls_at_rest():
    # A heavy arm held out to the side: sustained torque against gravity.
    heavy = PendulumLoad(mass_kg=0.5, com_distance_m=0.1)
    servo = SimServo(SimConfig(noise=False), load=heavy)
    start_degc = servo.read()[TEMPERATURE]
    servo.enable()
    servo.command(POSITION, math.radians(90.0))
    temperatures = []
    for _ in range(12):
        servo.clock.wait_s(10.0)
        sample = servo.read()
        temperatures.append(sample[TEMPERATURE])
    assert sample[CURRENT] > 0.15  # it really is working against gravity
    assert temperatures == sorted(temperatures)
    assert temperatures[-1] > start_degc + 10.0

    servo.enter_safe_state()
    resting = []
    for _ in range(12):
        servo.clock.wait_s(30.0)
        resting.append(servo.read()[TEMPERATURE])
    assert resting == sorted(resting, reverse=True)
    assert resting[-1] < temperatures[-1] - 10.0
    assert resting[-1] == pytest.approx(start_degc, abs=1.0)
    assert servo.read_faults() == ()


def ten_second_experiment(servo):
    """A 2 Hz sine of 10 deg for 10 s, commanding and reading every cycle: never at rest."""
    servo.enable()
    start_s = servo.clock.now_s()
    samples = []
    while servo.clock.now_s() - start_s < 10.0:
        t_s = servo.clock.now_s() - start_s
        servo.command(POSITION, math.radians(10.0) * math.sin(2 * math.pi * 2.0 * t_s))
        samples.append(servo.read())
    servo.enter_safe_state()
    return samples


def test_ten_second_experiment_takes_well_under_a_second():
    servo = SimServo(load=LOAD)  # noise and jitter on
    cpu_start_s = time.process_time()  # CPU time used by this process; not a clock the sim reads
    samples = ten_second_experiment(servo)
    cpu_s = time.process_time() - cpu_start_s
    assert samples[-1].t_s - samples[0].t_s >= 9.9
    assert len(samples) > 900
    assert cpu_s < 0.5


def test_the_simulator_never_sleeps_or_reads_the_real_clock(monkeypatch):
    def forbidden(*args):
        raise AssertionError("the simulator touched the real clock")

    for name in (
        "sleep",
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
    ):
        monkeypatch.setattr(time, name, forbidden)
    servo = SimServo(load=LOAD)
    ten_second_experiment(servo)
    servo.clock.wait_s(60.0)
    servo.read()


def test_same_seed_gives_the_same_samples_and_another_seed_does_not():
    def samples(seed):
        return ten_second_experiment(SimServo(SimConfig(seed=seed), load=LOAD))

    assert samples(7) == samples(7)
    assert samples(7) != samples(8)


# -- sampling, noise and the other signals -------------------------------------------


def test_transactions_take_one_bus_period_plus_jitter():
    servo = SimServo(SimConfig(sample_rate_hz=500.0, jitter_s=0.2e-3))
    times = [servo.read().t_s for _ in range(2000)]
    intervals = [b - a for a, b in itertools.pairwise(times)]
    assert min(intervals) > 0.0
    assert statistics.fmean(intervals) == pytest.approx(
        2e-3 + 0.2e-3 * math.sqrt(2 / math.pi), rel=0.02
    )
    # Half-normal extra time, and each interval spans half of two transactions.
    expected_stdev_s = 0.2e-3 * math.sqrt((1 - 2 / math.pi) / 2)
    assert statistics.stdev(intervals) == pytest.approx(expected_stdev_s, rel=0.1)

    steady = SimServo(SimConfig(sample_rate_hz=500.0, jitter_s=0.0))
    times = [steady.read().t_s for _ in range(100)]
    intervals = [b - a for a, b in itertools.pairwise(times)]
    assert max(intervals) - min(intervals) < 1e-12
    assert intervals[0] == pytest.approx(2e-3)


def test_position_is_quantised_to_4096_steps_per_turn():
    servo = SimServo(SimConfig(initial_position_rad=0.3))
    step_rad = TURN_RAD / 4096
    for _ in range(50):
        steps = servo.read()[POSITION] / step_rad
        assert steps == pytest.approx(round(steps), abs=1e-9)


def test_noise_can_be_switched_off():
    quiet = SimServo(SimConfig(noise=False))
    assert len({tuple(quiet.read().values.values()) for _ in range(20)}) == 1
    noisy = SimServo(SimConfig(noise=True))
    assert len({noisy.read()[VOLTAGE] for _ in range(20)}) > 1


def test_current_rises_and_voltage_sags_with_effort():
    heavy = PendulumLoad(mass_kg=0.5, com_distance_m=0.1)
    servo = SimServo(SimConfig(noise=False), load=heavy)
    at_rest = servo.read()
    servo.enable()
    servo.command(POSITION, math.radians(90.0))
    servo.clock.wait_s(1.0)
    working = servo.read()
    assert working[CURRENT] > at_rest[CURRENT] + 0.1
    assert working[VOLTAGE] < at_rest[VOLTAGE]
    assert at_rest[VOLTAGE] == pytest.approx(7.4, abs=0.05)


def test_safe_state_lets_the_arm_swing_down():
    servo = SimServo(EXACT, load=LOAD)
    step_response(servo, math.radians(45.0), 0.5)
    servo.enter_safe_state()
    servo.clock.wait_s(5.0)
    assert abs(servo.read()[POSITION]) < math.radians(15.0)


def test_shared_virtual_clock():
    clock = VirtualClock()
    first, second = SimServo(clock=clock), SimServo(clock=clock)
    assert first.clock is second.clock is clock
    a = first.read()
    b = second.read()
    assert b.t_s > a.t_s


# -- parameters ---------------------------------------------------------------------


def test_writing_the_gain_changes_the_response():
    amplitude_rad = math.radians(10.0)
    servo = SimServo(EXACT, load=LOAD)
    default = overshoot(step_response(servo, amplitude_rad), amplitude_rad)
    servo = SimServo(EXACT, load=LOAD)
    servo.write_parameter("kp_n_m_rad", 7.2)
    assert servo.read_parameter("kp_n_m_rad") == 7.2
    assert overshoot(step_response(servo, amplitude_rad), amplitude_rad) > default + 0.1


def test_persistent_write_needs_the_matching_confirmation():
    servo = SimServo()
    name = "temperature_cutoff_degc"
    servo.write_parameter(name, 60.0, confirmation=PersistentWriteConfirmation(name, 60.0, "test"))
    assert servo.read_parameter(name) == 60.0


# -- the bench file -------------------------------------------------------------------


def test_from_bench_uses_the_load_and_sim_settings():
    text = TEMPLATE.replace("# faults = [", "faults = [").replace("# seed = 0", "seed = 4")
    text = text.replace('# sample_rate = "200 Hz"', 'sample_rate = "100 Hz"')
    text = text.replace("# noise = true", "noise = false")
    bench = parse_bench(text.encode())
    servo = SimServo.from_bench(bench)
    assert servo.config.faults == {"wrong_zero"}
    assert servo.config.seed == 4
    assert servo.config.noise is False
    assert servo.config.sample_rate_hz == pytest.approx(100.0)
    assert servo.describe().max_sample_rate_hz == pytest.approx(100.0)
    assert servo._load.mass_kg == pytest.approx(0.05)
    assert servo._load.com_distance_m == pytest.approx(0.1)


def test_default_bench_settings_give_the_default_simulator():
    assert SimConfig.from_bench(SimSettings()) == SimConfig()
    bench = parse_bench(TEMPLATE.encode())
    assert SimServo.from_bench(bench).config == SimConfig()


def test_template_lists_every_fault():
    from servobench.limits import SIM_FAULTS

    for fault in SIM_FAULTS:
        assert re.search(rf'^#\s+"{fault}"', TEMPLATE, flags=re.MULTILINE), fault


# == Added in plan step 8: backlash, for testing the model fit ======================


def test_without_backlash_the_simulator_is_unchanged():
    plain = step_response(SimServo(EXACT, load=LOAD), math.radians(10))
    zero = step_response(
        SimServo(dataclasses.replace(EXACT, backlash_rad=0.0), load=LOAD), math.radians(10)
    )
    assert [s.values for s in plain] == [s.values for s in zero]


def test_backlash_makes_the_reading_lag_a_reversal_by_the_play():
    play_rad = math.radians(1.0)
    config = dataclasses.replace(EXACT, backlash_rad=play_rad)
    servo = SimServo(config, load=PendulumLoad())  # no gravity to hold the output to one side
    exact = SimServo(EXACT, load=PendulumLoad())
    readings = []
    for each in (servo, exact):
        each.enable()
        for target_deg in (5.0, 0.0):
            each.command(POSITION, math.radians(target_deg))
            each.clock.wait_s(1.0)
        readings.append(each.read()[POSITION])
    # Up to 5 deg and back to 0: the motor side returns to 0, the output is
    # left behind by half the play on the way up and the whole play on the way
    # back, so it reads half the play above zero.
    assert readings[0] - readings[1] == pytest.approx(0.5 * play_rad, abs=math.radians(0.02))


def test_backlash_cannot_be_negative():
    with pytest.raises(ValueError, match="backlash_rad"):
        SimConfig(backlash_rad=-0.1)
