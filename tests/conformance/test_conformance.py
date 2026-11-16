"""Tests that every adapter must pass. They use only the contract.

Tests marked ``motion`` change the actuator's state: they enable it, move it by
``target.move_rad``, or write a parameter back with the value it already has.
The rest only read.
"""

from __future__ import annotations

import math

import pytest

from servobench.contract import (
    POSITION,
    STANDARD_FAULT_CODES,
    Adapter,
    CommandRejected,
    CommunicationError,
    ContractError,
    Fault,
    PersistentWriteRefused,
    Sample,
    check_description,
)
from servobench.units import Dimension

# A plausible range for a reading in the SI unit of each dimension. A value
# outside it almost always means a unit mistake: millivolts sent as volts,
# kelvin as degC, encoder counts or degrees as radians.
PLAUSIBLE = {
    Dimension.ANGLE: (-4 * math.pi, 4 * math.pi),
    Dimension.ANGULAR_VELOCITY: (-200.0, 200.0),
    Dimension.VOLTAGE: (0.0, 80.0),
    Dimension.CURRENT: (-100.0, 100.0),
    Dimension.TEMPERATURE: (-40.0, 150.0),
    Dimension.TORQUE: (-500.0, 500.0),
}


def position(adapter: Adapter) -> float:
    return adapter.read([POSITION])[POSITION]


def settle(adapter: Adapter, duration_s: float) -> None:
    """Wait on the adapter's clock while reading, as the runner will."""
    end_s = adapter.clock.now_s() + duration_s
    while adapter.clock.now_s() < end_s:
        adapter.read([POSITION])


def small_move_target(adapter: Adapter, start_rad: float, move_rad: float) -> float:
    """A target ``move_rad`` away from ``start_rad``, inside the command range."""
    info = adapter.describe().command(POSITION)
    if start_rad + move_rad <= info.maximum:
        return start_rad + move_rad
    return start_rad - move_rad


# -- describe ------------------------------------------------------------------


def test_description_is_complete_and_consistent(adapter):
    assert check_description(adapter.describe()) == []


def test_description_is_the_same_on_every_call(adapter):
    assert adapter.describe() == adapter.describe()


# -- reading -------------------------------------------------------------------


def test_every_signal_reads_with_a_plausible_value_in_its_unit(adapter):
    description = adapter.describe()
    sample = adapter.read()
    assert isinstance(sample, Sample)
    assert set(sample.values) == set(description.signal_names)
    for signal in description.signals:
        value = sample[signal.name]
        assert isinstance(value, float) and math.isfinite(value), signal.name
        low, high = PLAUSIBLE.get(signal.dimension, (-math.inf, math.inf))
        assert low <= value <= high, f"{signal.name} = {value} {signal.dimension.si_unit}"


def test_reading_a_subset_returns_exactly_those_signals(adapter):
    for name in adapter.describe().signal_names:
        assert set(adapter.read([name]).values) == {name}


def test_unknown_signal_is_rejected(adapter):
    with pytest.raises(ContractError):
        adapter.read(["no_such_signal_rad"])
    with pytest.raises(ContractError):
        adapter.read(POSITION)  # a bare string is not a collection of names


def test_timestamps_increase_and_are_on_the_adapter_clock(adapter):
    previous_s = -math.inf
    for _ in range(20):
        before_s = adapter.clock.now_s()
        sample = adapter.read()
        after_s = adapter.clock.now_s()
        assert before_s <= sample.t_s <= after_s
        assert sample.t_s > previous_s
        previous_s = sample.t_s


def test_reads_keep_up_with_the_declared_rate(adapter):
    rate_hz = adapter.describe().max_sample_rate_hz
    count = 50
    start_s = adapter.clock.now_s()
    for _ in range(count):
        adapter.read()
    elapsed_s = adapter.clock.now_s() - start_s
    # The declared rate is the fastest reliable one; allow for jitter on top of it.
    assert elapsed_s <= 1.5 * count / rate_hz


def test_clock_is_monotonic_and_waits(adapter):
    clock = adapter.clock
    first_s = clock.now_s()
    clock.wait_s(0.05)
    second_s = clock.now_s()
    assert second_s >= first_s + 0.05
    clock.wait_until_s(first_s)  # a time in the past returns at once
    assert clock.now_s() >= second_s


# -- faults and parameters -------------------------------------------------------


def test_faults_are_readable_and_a_healthy_device_has_none(adapter):
    faults = adapter.read_faults()
    assert isinstance(faults, tuple)
    for fault in faults:
        assert isinstance(fault, Fault)
        assert fault.code in STANDARD_FAULT_CODES or ":" in fault.code
    assert faults == ()


def test_every_parameter_reads_as_a_finite_value(adapter):
    for parameter in adapter.describe().parameters:
        value = adapter.read_parameter(parameter.name)
        assert isinstance(value, float) and math.isfinite(value), parameter.name


@pytest.mark.motion
def test_volatile_parameter_accepts_its_own_value(adapter):
    for parameter in adapter.describe().parameters:
        if parameter.writable and not parameter.persistent:
            value = adapter.read_parameter(parameter.name)
            adapter.write_parameter(parameter.name, value)
            assert adapter.read_parameter(parameter.name) == pytest.approx(value)


@pytest.mark.motion  # if the adapter's guard were broken, this would write
def test_persistent_parameter_write_is_refused_without_confirmation(adapter):
    for parameter in adapter.describe().parameters:
        if parameter.writable and parameter.persistent:
            value = adapter.read_parameter(parameter.name)
            with pytest.raises(PersistentWriteRefused):
                adapter.write_parameter(parameter.name, value)
            assert adapter.read_parameter(parameter.name) == value


@pytest.mark.motion
def test_read_only_and_unknown_parameters_cannot_be_written(adapter):
    for parameter in adapter.describe().parameters:
        if not parameter.writable:
            with pytest.raises(ContractError):
                adapter.write_parameter(parameter.name, adapter.read_parameter(parameter.name))
    with pytest.raises(ContractError):
        adapter.read_parameter("no_such_parameter_s")
    with pytest.raises(ContractError):
        adapter.write_parameter("no_such_parameter_s", 1.0)


def test_close_releases_the_connection(target):
    with target.make() as device:
        device.read()
    with pytest.raises(CommunicationError):
        device.read()


# -- commanding (moves the actuator) -----------------------------------------------


@pytest.mark.motion
def test_command_is_refused_before_enable(adapter, target):
    start_rad = position(adapter)
    with pytest.raises(CommandRejected):
        adapter.command(POSITION, small_move_target(adapter, start_rad, target.move_rad))
    settle(adapter, target.settle_s)
    assert position(adapter) == pytest.approx(start_rad, abs=target.move_rad / 4)


@pytest.mark.motion
def test_bad_commands_are_refused_without_moving(adapter, target):
    adapter.enable()
    settle(adapter, target.settle_s)
    start_rad = position(adapter)
    info = adapter.describe().command(POSITION)
    for name, value in (
        (POSITION, info.maximum + target.move_rad),
        (POSITION, info.minimum - target.move_rad),
        (POSITION, math.nan),
        (POSITION, math.inf),
        (POSITION, True),
        ("no_such_command_rad", start_rad),
    ):
        with pytest.raises(CommandRejected):
            adapter.command(name, value)
    settle(adapter, target.settle_s)
    assert position(adapter) == pytest.approx(start_rad, abs=target.move_rad / 4)


@pytest.mark.motion
def test_enable_holds_the_present_position(adapter, target):
    start_rad = position(adapter)
    adapter.enable()
    settle(adapter, target.settle_s)
    assert position(adapter) == pytest.approx(start_rad, abs=target.move_rad / 4)


@pytest.mark.motion
def test_command_returns_the_time_it_was_sent(adapter, target):
    adapter.enable()
    start_rad = position(adapter)
    before_s = adapter.clock.now_s()
    sent_s = adapter.command(POSITION, small_move_target(adapter, start_rad, target.move_rad))
    after_s = adapter.clock.now_s()
    assert before_s <= sent_s <= after_s


@pytest.mark.motion
@pytest.mark.parametrize("sign", [1.0, -1.0], ids=["positive", "negative"])
def test_small_move_goes_the_commanded_way_by_about_the_commanded_amount(adapter, target, sign):
    adapter.enable()
    settle(adapter, target.settle_s)
    start_rad = position(adapter)
    goal_rad = small_move_target(adapter, start_rad, sign * target.move_rad)
    adapter.command(POSITION, goal_rad)
    settle(adapter, target.settle_s)
    moved_rad = position(adapter) - start_rad
    wanted_rad = goal_rad - start_rad
    assert moved_rad * wanted_rad > 0, "moved the wrong way"
    # Within a factor of two catches a unit mistake (degrees are 57 times radians)
    # without demanding precision from a servo under load.
    assert 0.5 * abs(wanted_rad) <= abs(moved_rad) <= 2.0 * abs(wanted_rad)


def _fresh(adapter, target):
    """As created."""


def _enabled(adapter, target):
    adapter.enable()


def _moving(adapter, target):
    adapter.enable()
    start_rad = position(adapter)
    adapter.command(POSITION, small_move_target(adapter, start_rad, target.move_rad))
    adapter.read()


def _already_safe(adapter, target):
    adapter.enable()
    adapter.enter_safe_state()


def _re_enabled(adapter, target):
    adapter.enable()
    adapter.enter_safe_state()
    adapter.enable()


@pytest.mark.motion
@pytest.mark.parametrize(
    "reach_state",
    [_fresh, _enabled, _moving, _already_safe, _re_enabled],
    ids=["fresh", "enabled", "moving", "already-safe", "re-enabled"],
)
def test_safe_state_from_any_state(adapter, target, reach_state):
    reach_state(adapter, target)
    adapter.enter_safe_state()
    sample = adapter.read()
    assert set(sample.values) == set(adapter.describe().signal_names)
    with pytest.raises(CommandRejected):
        adapter.command(POSITION, small_move_target(adapter, position(adapter), target.move_rad))
    assert adapter.read_faults() == ()
