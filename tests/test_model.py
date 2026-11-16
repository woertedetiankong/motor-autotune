import dataclasses
import math

import numpy as np
import pytest

from servobench.adapters.sim import SimConfig, SimServo
from servobench.limits import Load
from servobench.model import (
    DEFAULT_PARAMS,
    GRAVITY_M_S2,
    Integrator,
    PendulumLoad,
    ServoModel,
    ServoParams,
    simulate,
)

LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)


def params(**changes) -> ServoParams:
    return dataclasses.replace(DEFAULT_PARAMS, **changes)


def run(model, start_rad, duration_s, *, torque_on, setpoint_rad=None, dt_s=0.0005):
    integrator = Integrator(
        model, 0.0, start_rad, setpoint_rad=setpoint_rad, torque_on=torque_on, dt_s=dt_s
    )
    times = np.arange(0.0, duration_s, dt_s)
    return times, np.array([integrator.observe(t)[0] for t in times])


def test_load_from_bench():
    assert PendulumLoad.from_bench(None) == PendulumLoad()
    load = PendulumLoad.from_bench(Load(mass_kg=0.05, length_m=0.1))
    assert load.inertia_kg_m2 == pytest.approx(0.05 * 0.1**2)
    assert load.gravity_torque_max_n_m == pytest.approx(0.05 * GRAVITY_M_S2 * 0.1)


@pytest.mark.parametrize(
    "changes",
    [{"inertia_kg_m2": 0.0}, {"torque_max_n_m": 0.0}, {"coulomb_n_m": -1.0}, {"delay_s": math.nan}],
)
def test_params_are_validated(changes):
    with pytest.raises(ValueError):
        params(**changes)


def test_free_pendulum_swings_at_its_natural_period_without_losing_energy():
    # Torque off, no friction: a pendulum. Small-angle period is 2 pi sqrt(J / (m g L)).
    model = ServoModel(params(coulomb_n_m=0.0, viscous_n_m_s_rad=0.0), LOAD)
    expected_period_s = 2 * math.pi * math.sqrt(model.inertia_kg_m2 / LOAD.gravity_torque_max_n_m)
    start_rad = math.radians(3.0)
    times, positions = run(model, start_rad, 10 * expected_period_s, torque_on=False)
    upward = np.flatnonzero((positions[:-1] < 0) & (positions[1:] >= 0))
    period_s = np.mean(np.diff(times[upward]))
    assert period_s == pytest.approx(expected_period_s, rel=0.005)
    # Semi-implicit Euler is symplectic for this conservative case: no drift in amplitude.
    assert np.max(np.abs(positions[-len(positions) // 10 :])) == pytest.approx(start_rad, rel=0.01)


def test_friction_holds_the_arm_when_gravity_is_weaker_and_not_when_stronger():
    model = ServoModel(params(coulomb_n_m=0.01), LOAD)
    holding_rad = math.asin(0.5 * 0.01 / LOAD.gravity_torque_max_n_m)  # gravity torque 0.005 N m
    _, positions = run(model, holding_rad, 1.0, torque_on=False)
    assert np.all(positions == holding_rad)
    slipping_rad = math.asin(2.0 * 0.01 / LOAD.gravity_torque_max_n_m)
    _, positions = run(model, slipping_rad, 10.0, torque_on=False)
    # With this much viscous friction the arm creeps, overdamped, down to where
    # gravity no longer beats the Coulomb friction, and stays there.
    assert positions[-1] < slipping_rad - 0.1
    final_gravity_n_m = LOAD.gravity_torque_max_n_m * math.sin(positions[-1])
    assert final_gravity_n_m == pytest.approx(0.01, rel=0.01)


def test_torque_never_exceeds_the_limit():
    model = ServoModel(params(torque_max_n_m=0.3), LOAD)
    integrator = Integrator(model, 0.0, 0.0, setpoint_rad=math.radians(90.0))
    torques = [integrator.observe(t)[2] for t in np.arange(0.0, 1.0, 0.001)]
    assert max(np.abs(torques)) == pytest.approx(0.3)


@pytest.mark.parametrize(
    "changes",
    [
        {"inertia_kg_m2": 1e-8, "kd_n_m_s_rad": 5.0, "kp_n_m_rad": 500.0},
        {"inertia_kg_m2": 1e-8, "viscous_n_m_s_rad": 10.0, "coulomb_n_m": 1.0},
        {"kp_n_m_rad": 1000.0, "kd_n_m_s_rad": 0.0},
    ],
)
@pytest.mark.parametrize("dt_s", [0.0005, 0.01])
def test_stiff_parameters_stay_finite_and_bounded(changes, dt_s):
    # What the fit may try. An explicit scheme blows up on these.
    model = ServoModel(params(**changes), LOAD)
    _, positions = run(model, 0.0, 2.0, torque_on=True, setpoint_rad=0.2, dt_s=dt_s)
    assert np.all(np.isfinite(positions))
    assert np.max(np.abs(positions)) < 1.0


def test_pd_servo_settles_near_the_setpoint():
    model = ServoModel(DEFAULT_PARAMS, LOAD)
    _, positions = run(model, 0.0, 1.0, torque_on=True, setpoint_rad=math.radians(10.0))
    gravity_sag_rad = LOAD.gravity_torque_max_n_m * math.sin(math.radians(10)) / 1.8
    deadband_rad = DEFAULT_PARAMS.coulomb_n_m / DEFAULT_PARAMS.kp_n_m_rad
    error_rad = positions[-1] - math.radians(10.0)
    assert -gravity_sag_rad - deadband_rad - 1e-9 <= error_rad <= deadband_rad - gravity_sag_rad


def test_halving_the_time_step_changes_a_step_response_very_little():
    commands = ([0.01], [math.radians(10.0)])
    times = np.arange(0.0, 0.5, 0.002)
    coarse = simulate(DEFAULT_PARAMS, LOAD, *commands, times, initial_position_rad=0.0, dt_s=0.001)
    fine = simulate(DEFAULT_PARAMS, LOAD, *commands, times, initial_position_rad=0.0, dt_s=0.0005)
    assert np.max(np.abs(coarse.position_rad - fine.position_rad)) < math.radians(0.05)


def test_response_varies_smoothly_with_delay_below_the_grid_step():
    times = np.arange(0.0, 0.3, 0.005)
    responses = [
        simulate(params(delay_s=delay_s), LOAD, [0.01], [0.2], times, initial_position_rad=0.0)
        for delay_s in (0.0040, 0.0041, 0.0042)
    ]
    first = responses[1].position_rad - responses[0].position_rad
    second = responses[2].position_rad - responses[1].position_rad
    assert np.max(np.abs(first)) > 1e-5  # a 0.1 ms change is visible, not rounded to the grid
    assert np.max(np.abs(first - second)) < 0.1 * np.max(np.abs(first))


def test_command_takes_effect_after_the_delay():
    times = np.arange(0.0, 0.05, 0.0005)
    response = simulate(params(delay_s=0.01), LOAD, [0.005], [0.2], times, initial_position_rad=0.0)
    assert np.all(response.position_rad[times <= 0.015] == 0.0)
    assert response.position_rad[-1] > 0.0


@pytest.mark.parametrize("rate_hz", [200.0, 173.0])  # 173 Hz: samples off the model's grid
def test_replay_reproduces_the_simulated_servo(rate_hz):
    # The fit relies on this: the simulator and the replay are the same model.
    # Without jitter the sample stamps are the true sampling instants; what is
    # left is the replay's grid starting at a different time from the servo's.
    config = SimConfig(
        noise=False, jitter_s=0.0, position_steps_per_turn=None, sample_rate_hz=rate_hz
    )
    servo = SimServo(config, load=LOAD)
    servo.enable()
    commands, samples = [], []
    for target_deg in (10.0, -5.0, 30.0, 0.0):
        commands.append((servo.command("position_rad", math.radians(target_deg)), target_deg))
        end_s = servo.clock.now_s() + 0.5
        while servo.clock.now_s() < end_s:
            samples.append(servo.read())
    times = [sample.t_s for sample in samples]
    replay = simulate(
        DEFAULT_PARAMS,
        LOAD,
        [t_s for t_s, _ in commands],
        [math.radians(deg) for _, deg in commands],
        times,
        initial_position_rad=samples[0]["position_rad"],
        initial_velocity_rad_s=samples[0]["velocity_rad_s"],
        initial_setpoint_rad=0.0,
    )
    measured = np.array([sample["position_rad"] for sample in samples])
    assert np.max(np.abs(replay.position_rad - measured)) < math.radians(0.02)


def test_simulate_checks_its_inputs():
    with pytest.raises(ValueError):
        simulate(DEFAULT_PARAMS, LOAD, [], [], [], initial_position_rad=0.0)
    with pytest.raises(ValueError):
        simulate(DEFAULT_PARAMS, LOAD, [], [], [1.0, 0.5], initial_position_rad=0.0)
    with pytest.raises(ValueError):
        simulate(DEFAULT_PARAMS, LOAD, [0.2, 0.1], [0.0, 0.0], [0.0], initial_position_rad=0.0)
    with pytest.raises(ValueError):
        simulate(DEFAULT_PARAMS, LOAD, [0.1], [], [0.0], initial_position_rad=0.0)


# == Added in plan step 8: the integrator is second order ========================


LIGHTLY_DAMPED = params(
    kp_n_m_rad=1.8,
    kd_n_m_s_rad=0.02,
    inertia_kg_m2=0.002,
    viscous_n_m_s_rad=0.0,
    coulomb_n_m=0.0,
    delay_s=0.0,
    torque_max_n_m=100.0,
)


def test_a_lightly_damped_step_overshoots_as_the_closed_form_says():
    # Friction and load removed: J x'' + kd x' + kp x = kp r, damping ratio 1/6,
    # overshoot exp(-pi zeta / sqrt(1 - zeta^2)) = 58.80 %. Backward Euler at
    # 0.5 ms, the scheme up to step 7, gave 57.41 %.
    zeta = 0.02 / (2 * math.sqrt(1.8 * 0.002))
    closed_form = 100 * math.exp(-math.pi * zeta / math.sqrt(1 - zeta * zeta))
    times = np.arange(0.0, 1.0, 0.00001)
    response = simulate(
        LIGHTLY_DAMPED, PendulumLoad(), [0.0], [0.2], times,
        initial_position_rad=0.0, initial_setpoint_rad=0.0,
    )  # fmt: skip
    overshoot = 100 * (response.position_rad.max() / 0.2 - 1)
    assert overshoot == pytest.approx(closed_form, abs=0.05)


def test_halving_the_time_step_quarters_the_error():
    times = np.arange(0.0, 0.5, 0.002)

    def response(dt_s):
        return simulate(
            LIGHTLY_DAMPED, LOAD, [0.01], [0.2], times,
            initial_position_rad=0.0, initial_setpoint_rad=0.0, dt_s=dt_s,
        ).position_rad  # fmt: skip

    reference = response(0.0005 / 32)
    coarse = np.max(np.abs(response(0.001) - reference))
    fine = np.max(np.abs(response(0.0005) - reference))
    assert coarse / fine > 3.5  # first order would give 2
    assert fine < math.radians(0.01)
