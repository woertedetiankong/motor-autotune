import dataclasses
import math

import pytest

from servobench.adapters.sim import SimServo
from servobench.contract import (
    POSITION,
    UNIT_SUFFIXES,
    CommandInfo,
    CommandRejected,
    ContractError,
    ParameterInfo,
    PersistentWriteConfirmation,
    PersistentWriteRefused,
    SignalInfo,
    Support,
    check_command,
    check_description,
    check_parameter_write,
    check_signal_names,
    dimension_of_name,
)
from servobench.units import Dimension

GOOD = SimServo().describe()


def with_changes(**changes):
    return dataclasses.replace(GOOD, **changes)


def test_every_dimension_has_a_unit_suffix():
    assert set(UNIT_SUFFIXES) == set(Dimension)


@pytest.mark.parametrize(
    ("name", "dimension"),
    [
        ("position_rad", Dimension.ANGLE),
        ("velocity_rad_s", Dimension.ANGULAR_VELOCITY),
        ("delay_s", Dimension.TIME),
        ("torque_max_n_m", Dimension.TORQUE),
        ("arm_length_m", Dimension.LENGTH),
        ("kp_n_m_rad", Dimension.ROTATIONAL_STIFFNESS),
        ("kd_n_m_s_rad", Dimension.ROTATIONAL_DAMPING),
        ("temperature_degc", Dimension.TEMPERATURE),
        ("current_a", Dimension.CURRENT),
        ("voltage_v", Dimension.VOLTAGE),
        ("position", None),
        ("position_deg", None),
        ("_rad", None),
    ],
)
def test_dimension_of_name_uses_the_longest_suffix(name, dimension):
    assert dimension_of_name(name) is dimension


def test_the_simulated_servo_description_passes():
    assert check_description(GOOD) == []


def test_a_name_whose_suffix_disagrees_with_its_dimension_is_caught():
    signals = (*GOOD.signals, SignalInfo("angle_deg", Dimension.ANGLE, "an angle"))
    assert any(
        "angle_deg" in problem for problem in check_description(with_changes(signals=signals))
    )
    signals = (*GOOD.signals, SignalInfo("supply_v", Dimension.CURRENT, "wrong dimension"))
    assert any(
        "supply_v" in problem for problem in check_description(with_changes(signals=signals))
    )


def test_missing_position_signal_is_caught():
    signals = tuple(signal for signal in GOOD.signals if signal.name != POSITION)
    problems = check_description(with_changes(signals=signals))
    assert any("position_rad" in problem for problem in problems)


def test_duplicate_names_and_bad_ranges_are_caught():
    command = CommandInfo(POSITION, Dimension.ANGLE, 1.0, -1.0, "backwards range")
    problems = check_description(with_changes(commands=(command,)))
    assert any("minimum must be below" in problem for problem in problems)
    problems = check_description(with_changes(signals=GOOD.signals + GOOD.signals[:1]))
    assert any("declared twice" in problem for problem in problems)
    parameter = ParameterInfo(
        "gain_n_m_rad", Dimension.ROTATIONAL_STIFFNESS, True, False, "x", minimum=2.0, maximum=1.0
    )
    problems = check_description(with_changes(parameters=(parameter,)))
    assert any("minimum is above" in problem for problem in problems)


@pytest.mark.parametrize("rate", [0.0, -5.0, math.inf, math.nan])
def test_bad_sample_rate_is_caught(rate):
    assert check_description(with_changes(max_sample_rate_hz=rate))


def test_comm_loss_timeout_must_match_the_capability():
    yes_without_timeout = dataclasses.replace(
        GOOD.capabilities, safe_state_on_comm_loss=Support.YES
    )
    assert check_description(with_changes(capabilities=yes_without_timeout))
    yes_with_timeout = dataclasses.replace(yes_without_timeout, comm_loss_timeout_s=0.5)
    assert check_description(with_changes(capabilities=yes_with_timeout)) == []
    no_with_timeout = dataclasses.replace(GOOD.capabilities, comm_loss_timeout_s=0.5)
    assert check_description(with_changes(capabilities=no_with_timeout))


def test_capabilities_must_be_support_values():
    bool_instead = dataclasses.replace(GOOD.capabilities, torque_limit=True)
    assert any(
        "torque_limit" in problem
        for problem in check_description(with_changes(capabilities=bool_instead))
    )


def test_blank_texts_are_caught():
    assert check_description(with_changes(safe_state=" "))
    identity = dataclasses.replace(GOOD.identity, model="")
    assert check_description(with_changes(identity=identity))


def test_check_command():
    check_command(GOOD, POSITION, 0.5)
    for name, value in (
        (POSITION, 10.0),
        (POSITION, math.nan),
        (POSITION, "0.5"),
        (POSITION, False),
        ("speed_rad_s", 0.0),
    ):
        with pytest.raises(CommandRejected):
            check_command(GOOD, name, value)


def test_check_signal_names():
    assert check_signal_names(GOOD, None) == GOOD.signal_names
    assert check_signal_names(GOOD, [POSITION]) == (POSITION,)
    with pytest.raises(ContractError):
        check_signal_names(GOOD, ["position_deg"])


def test_persistent_write_needs_a_confirmation_of_that_exact_write():
    name = "temperature_cutoff_degc"
    assert GOOD.parameter(name).persistent
    with pytest.raises(PersistentWriteRefused):
        check_parameter_write(GOOD, name, 60.0, None)
    wrong_value = PersistentWriteConfirmation(name, 65.0, "the owner, at the bench")
    with pytest.raises(PersistentWriteRefused):
        check_parameter_write(GOOD, name, 60.0, wrong_value)
    wrong_name = PersistentWriteConfirmation("kp_n_m_rad", 60.0, "the owner, at the bench")
    with pytest.raises(PersistentWriteRefused):
        check_parameter_write(GOOD, name, 60.0, wrong_name)
    right = PersistentWriteConfirmation(name, 60.0, "the owner, at the bench")
    assert check_parameter_write(GOOD, name, 60.0, right).name == name


def test_parameter_write_range_is_checked():
    with pytest.raises(ContractError):
        check_parameter_write(GOOD, "kp_n_m_rad", -1.0, None)
    with pytest.raises(ContractError):
        check_parameter_write(GOOD, "kp_n_m_rad", math.nan, None)


# == Regressions from the second outside review, 2026-11-16 ============================


@pytest.mark.parametrize("bound", [None, 0.0, -0.01, math.inf, math.nan, True, "20 ms"])
def test_the_bound_on_one_call_is_required_finite_and_positive(bound):
    problems = check_description(with_changes(call_time_max_s=bound))
    assert any("call_time_max_s" in problem for problem in problems), problems


def test_the_simulated_servo_declares_its_reply_timeout_as_its_call_bound():
    servo = SimServo()
    assert GOOD.call_time_max_s == servo.config.reply_timeout_s == 0.02
