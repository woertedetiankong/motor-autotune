"""The adapter contract: the only interface between the tool and an actuator.

An adapter is the one piece of code that knows a particular actuator. Everything
else in the tool (the runner, the safety monitor, the analysis, the commands) is
written against the ``Adapter`` class below and the data types it returns.

Rules every adapter follows:

Units. Every value crossing this interface is a float in the SI unit of its
dimension (see ``servobench.units``; temperatures are in degC). Every name of a
signal, command or parameter ends in a suffix that says its unit, for example
``position_rad`` or ``kp_n_m_rad``; ``UNIT_SUFFIXES`` lists the suffixes and
``check_description`` rejects a name whose suffix disagrees with its declared
dimension. Positions are measured from the actuator's zero; positive is the
direction the device itself calls positive.

Time. Every time is in seconds on the adapter's own clock (``Adapter.clock``).
A sample's ``t_s`` is the adapter's best estimate of the instant the values were
measured, and ``Description.timestamp`` says how that estimate was made. A time
returned by ``command`` is the instant the adapter sent the command; any delay
between that and the actuator acting on it is part of the actuator's behaviour,
to be measured, not hidden. Callers wait only through ``Adapter.clock``, never
with ``time.sleep``, so a simulated adapter can run on virtual time.

Bounded calls. Every call that may reach the device (``read``, ``command``,
``enable``, ``enter_safe_state``, ``read_faults``, ``read_parameter``,
``write_parameter``) returns or raises within ``Description.call_time_max_s``,
measured on the adapter's clock from the moment it is called, whether the
device answers or not. An adapter for a real bus guarantees this by setting
its own timeouts (for a serial port, the read and write timeouts, plus any
retries it makes) and declares the resulting worst case; it never waits on the
device without a timeout. The bound matters for safety: the runner is
single-threaded and can act only between calls, so a call that blocks is time
in which nothing watches the actuator and no stop request can be sent. A call
that takes longer than declared breaks the contract, and the runner stops the
run when it sees one.

Honesty. ``Description.capabilities`` states which protections the hardware
really has. ``Support.YES`` is a claim the adapter's author has verified on the
device or read in its primary documentation; anything else is ``NO`` or
``UNKNOWN``. The tool treats ``UNKNOWN`` like ``NO``.

Faults. ``read_faults`` reports them. There is deliberately no way to clear a
fault through this interface: recovery from a fault is a human decision.

Persistent writes. A parameter marked ``persistent`` survives a power cycle.
``write_parameter`` refuses to write one unless it is given a
``PersistentWriteConfirmation``, which only a command run by a human may create.

State. An adapter starts in whatever state the device is in and changes nothing
by being created or closed: closing releases the connection, it does not turn
torque off. (On an assembled robot, turning torque off drops it.) The safe state
is entered only by ``enter_safe_state``, and the runner calls it on every exit.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from servobench.clock import Clock
from servobench.units import Dimension

# ---------------------------------------------------------------------------
# Names and units


#: The unit suffix that every signal, command and parameter name of a given
#: dimension must end with, after an underscore: ``position_rad``,
#: ``velocity_rad_s``, ``kd_n_m_s_rad``.
UNIT_SUFFIXES: dict[Dimension, str] = {
    Dimension.ANGLE: "rad",
    Dimension.ANGULAR_VELOCITY: "rad_s",
    Dimension.FREQUENCY: "hz",
    Dimension.TIME: "s",
    Dimension.VOLTAGE: "v",
    Dimension.CURRENT: "a",
    Dimension.TEMPERATURE: "degc",
    Dimension.MASS: "kg",
    Dimension.LENGTH: "m",
    Dimension.TORQUE: "n_m",
    Dimension.ROTATIONAL_STIFFNESS: "n_m_rad",
    Dimension.ROTATIONAL_DAMPING: "n_m_s_rad",
}


def dimension_of_name(name: str) -> Dimension | None:
    """The dimension a name's unit suffix stands for, or None if it has no known suffix.

    The longest matching suffix wins, so ``velocity_rad_s`` is an angular
    velocity, not a time, and ``torque_n_m`` is a torque, not a length.
    """
    best: tuple[int, Dimension] | None = None
    for dimension, suffix in UNIT_SUFFIXES.items():
        tail = "_" + suffix
        if name.endswith(tail) and len(name) > len(tail):
            if best is None or len(tail) > best[0]:
                best = (len(tail), dimension)
    return None if best is None else best[1]


# Standard signal names. The runner and the safety monitor look for these.
# An adapter must provide POSITION and may provide others of its own, named
# with a unit suffix.
POSITION = "position_rad"  # output shaft angle from the actuator's zero
VELOCITY = "velocity_rad_s"  # output shaft angular velocity
CURRENT = "current_a"  # current drawn by the actuator
VOLTAGE = "voltage_v"  # supply voltage at the actuator
TEMPERATURE = "temperature_degc"  # the hottest temperature the device reports

REQUIRED_SIGNALS = (POSITION,)

# Standard fault codes. An adapter uses one of these when it fits; otherwise it
# uses its own code written "<adapter>:<code>", for example "hd1910:bit5".
STANDARD_FAULT_CODES: dict[str, str] = {
    "overheat": "the device is over its temperature limit",
    "overload": "the device is over its torque or current limit",
    "overvoltage": "the supply voltage is above the device's limit",
    "undervoltage": "the supply voltage is below the device's limit",
    "position_limit": "the device hit or was commanded past its own travel limit",
    "sensor": "the device reports a position or other sensor error",
}


# ---------------------------------------------------------------------------
# The description


class Support(Enum):
    """Whether a device really has a protection or capability."""

    YES = "yes"  # verified on the device or read in its primary documentation
    NO = "no"  # the device does not have it
    UNKNOWN = "unknown"  # not established; the tool treats this like NO


class TimestampSource(Enum):
    """How an adapter estimates the instant a sample was measured."""

    DEVICE = "device"  # the device stamped it; converted to the adapter's clock
    MIDPOINT = "midpoint"  # host: halfway between sending the request and receiving the reply
    REPLY = "reply"  # host: the moment the reply was received


@dataclass(frozen=True)
class SignalInfo:
    """A value the adapter can read."""

    name: str  # with its unit suffix, for example "position_rad"
    dimension: Dimension
    description: str


@dataclass(frozen=True)
class CommandInfo:
    """A setpoint the adapter can be commanded to, and the range it accepts."""

    name: str  # with its unit suffix, for example "position_rad"
    dimension: Dimension
    minimum: float  # in the SI unit of ``dimension``
    maximum: float  # in the SI unit of ``dimension``
    description: str


@dataclass(frozen=True)
class ParameterInfo:
    """A setting on the device that can be read, and perhaps written."""

    name: str  # with its unit suffix, for example "kp_n_m_rad"
    dimension: Dimension
    writable: bool
    persistent: bool  # a written value survives a power cycle
    description: str
    minimum: float | None = None  # accepted range for a write, in SI; None if unbounded
    maximum: float | None = None


@dataclass(frozen=True)
class Identity:
    adapter: str  # the adapter name used in bench.toml, for example "sim"
    model: str  # the device model, as the device reports it where it can
    device_id: str | None  # bus ID or serial number; None if the device has none
    firmware: str | None  # firmware version as reported; None if unknown
    simulated: bool  # True for a simulated device


@dataclass(frozen=True)
class Capabilities:
    """Which protections and features the hardware really has.

    Each protection acts on the device itself, without waiting for the host.
    """

    position_limit: Support  # refuses or clamps travel outside its configured range
    speed_limit: Support  # limits output speed by itself
    torque_limit: Support  # limits output torque or motor current by itself
    temperature_cutoff: Support  # stops driving when too hot
    voltage_cutoff: Support  # stops driving when the supply is out of range
    safe_state_on_comm_loss: Support  # enters its safe state by itself when the host goes quiet
    fault_latching: Support  # a fault stays until a human clears it, for example by power cycle
    onboard_capture: Support  # can record signals on the device at a high rate
    onboard_test_signals: Support  # can generate test signals on the device
    # How long without communication before the device enters its safe state.
    # Set only when safe_state_on_comm_loss is YES.
    comm_loss_timeout_s: float | None = None


@dataclass(frozen=True)
class Description:
    """Everything the tool needs to know about an actuator before using it."""

    identity: Identity
    signals: tuple[SignalInfo, ...]
    commands: tuple[CommandInfo, ...]  # empty for a read-only adapter
    parameters: tuple[ParameterInfo, ...]
    # Fastest rate at which read() of every declared signal can be called back
    # to back and still reply reliably.
    max_sample_rate_hz: float
    timestamp: TimestampSource
    # The longest any one call that may reach the device takes to return or
    # raise, on the adapter's clock, whether or not the device answers (see
    # "Bounded calls" above). Finite and above zero. A real adapter sets its
    # timeouts so that this holds and declares the result.
    call_time_max_s: float
    capabilities: Capabilities
    # What the safe state is on this device, in plain words, including what
    # the load does in it: for example "torque off: the output turns freely
    # and a pendulum load falls to hanging".
    safe_state: str

    @property
    def signal_names(self) -> tuple[str, ...]:
        return tuple(signal.name for signal in self.signals)

    def signal(self, name: str) -> SignalInfo:
        return _find(self.signals, name, "signal")

    def command(self, name: str) -> CommandInfo:
        return _find(self.commands, name, "command")

    def parameter(self, name: str) -> ParameterInfo:
        return _find(self.parameters, name, "parameter")


def _find(items, name: str, kind: str):
    for item in items:
        if item.name == name:
            return item
    known = ", ".join(item.name for item in items) or "none"
    raise ContractError(f"unknown {kind} {name!r}; this adapter has: {known}")


# ---------------------------------------------------------------------------
# What reading and commanding return


@dataclass(frozen=True)
class Sample:
    """Values read together, with one timestamp on the adapter's clock."""

    t_s: float
    values: Mapping[str, float] = field(default_factory=dict)  # name -> SI value

    def __getitem__(self, name: str) -> float:
        return self.values[name]


@dataclass(frozen=True)
class Fault:
    code: str  # a key of STANDARD_FAULT_CODES, or "<adapter>:<code>"
    description: str


@dataclass(frozen=True)
class PersistentWriteConfirmation:
    """A human's explicit agreement to one persistent write.

    Only a command that a human runs and confirms may create one. No code path
    in the tool creates one on its own, and an agent must never create one.
    """

    parameter: str
    value: float
    confirmed_by: str  # who confirmed, and how, in plain words


# ---------------------------------------------------------------------------
# Errors


class AdapterError(Exception):
    """Base class of every error an adapter raises on purpose."""


class ContractError(AdapterError, ValueError):
    """The caller broke the contract: unknown name, wrong state, bad value."""


class CommandRejected(ContractError):
    """A command was refused before it reached the actuator."""


class PersistentWriteRefused(ContractError):
    """A persistent parameter write was refused for lack of a matching confirmation."""


class CommunicationError(AdapterError):
    """The device could not be reached, or its reply was unusable."""


class NoReply(CommunicationError):
    """The device did not reply in time."""


class DeviceFaulted(AdapterError):
    """The device refused because it has a fault that only a human can clear."""


# ---------------------------------------------------------------------------
# The interface


class Adapter(ABC):
    """One actuator, behind the contract described at the top of this module."""

    @property
    @abstractmethod
    def clock(self) -> Clock:
        """The clock that every timestamp from this adapter is on."""

    @abstractmethod
    def describe(self) -> Description:
        """What this actuator is and what it can do. The same on every call."""

    @abstractmethod
    def read(self, signals: Iterable[str] | None = None) -> Sample:
        """Read some declared signals (all of them when ``signals`` is None).

        Returns exactly the requested names, SI values. Each call's ``t_s`` is
        later than the previous call's. Works in every state, the safe state
        included. Raises ``ContractError`` for an undeclared name and
        ``CommunicationError`` (for example ``NoReply``) when the device does not
        answer, within ``Description.call_time_max_s`` like every other call.
        """

    @abstractmethod
    def command(self, name: str, value: float) -> float:
        """Send a setpoint, for example ``command("position_rad", 0.1)``.

        Returns the time, on the adapter's clock, at which it was sent. Raises
        ``CommandRejected``, without sending anything, if the name is not a
        declared command, the value is not finite or is outside the declared
        range, or the adapter has not been enabled since it was created or last
        put in the safe state. An adapter never clamps a value silently.
        """

    @abstractmethod
    def enable(self) -> None:
        """Start driving the output, holding the position it is at now.

        Enabling must not cause motion by itself: the adapter makes the device
        hold its present position, not an old setpoint. Raises ``DeviceFaulted``
        if a latched fault prevents it.
        """

    @abstractmethod
    def enter_safe_state(self) -> None:
        """Put the actuator in its safe state (``Description.safe_state``).

        Works from any state, may be called any number of times, and returns
        only once the adapter has done everything it can to confirm the device
        is there, within ``Description.call_time_max_s``. Reading still works
        afterwards. Commands are refused until ``enable`` is called again.
        Raises ``CommunicationError`` if the device cannot be reached; the
        caller should retry and then tell a human.
        """

    @abstractmethod
    def read_faults(self) -> tuple[Fault, ...]:
        """The faults the device reports now; empty if none. There is no clear call."""

    @abstractmethod
    def read_parameter(self, name: str) -> float:
        """Read a declared parameter, SI value."""

    @abstractmethod
    def write_parameter(
        self,
        name: str,
        value: float,
        *,
        confirmation: PersistentWriteConfirmation | None = None,
    ) -> None:
        """Write a declared writable parameter, SI value.

        A persistent parameter is written only with a ``confirmation`` whose
        parameter and value match; otherwise ``PersistentWriteRefused`` is
        raised and nothing is sent. An out-of-range or non-finite value, or a
        read-only parameter, raises ``ContractError``.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the connection. Does not change the actuator's state.

        Afterwards every other call raises ``CommunicationError``.
        """

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Checks shared by adapters, the runner and the conformance suite


def check_description(description: Description) -> list[str]:
    """Every way a description is incomplete or inconsistent; empty if it is fine."""
    problems: list[str] = []
    identity = description.identity
    if not identity.adapter.strip():
        problems.append("identity.adapter is empty")
    if not identity.model.strip():
        problems.append("identity.model is empty")

    for kind, items in (
        ("signal", description.signals),
        ("command", description.commands),
        ("parameter", description.parameters),
    ):
        seen: set[str] = set()
        for item in items:
            if item.name in seen:
                problems.append(f"{kind} {item.name!r} is declared twice")
            seen.add(item.name)
            if not isinstance(item.dimension, Dimension):
                problems.append(f"{kind} {item.name!r} has no dimension")
                continue
            named = dimension_of_name(item.name)
            if named is not item.dimension:
                problems.append(
                    f"{kind} {item.name!r} is declared as {item.dimension.description} but its"
                    f' name must end in "_{UNIT_SUFFIXES[item.dimension]}"'
                )
            if not item.description.strip():
                problems.append(f"{kind} {item.name!r} has no description")

    for name in REQUIRED_SIGNALS:
        if name not in description.signal_names:
            problems.append(f"required signal {name!r} is not declared")

    for command in description.commands:
        if not (_finite(command.minimum) and _finite(command.maximum)):
            problems.append(f"command {command.name!r} range must be finite")
        elif command.minimum >= command.maximum:
            problems.append(f"command {command.name!r} minimum must be below its maximum")

    for parameter in description.parameters:
        for bound in (parameter.minimum, parameter.maximum):
            if bound is not None and not _finite(bound):
                problems.append(f"parameter {parameter.name!r} bounds must be finite")
        if (
            parameter.minimum is not None
            and parameter.maximum is not None
            and parameter.minimum > parameter.maximum
        ):
            problems.append(f"parameter {parameter.name!r} minimum is above its maximum")

    if not (_finite(description.max_sample_rate_hz) and description.max_sample_rate_hz > 0):
        problems.append("max_sample_rate_hz must be a finite rate above zero")
    if not (_finite(description.call_time_max_s) and description.call_time_max_s > 0):
        problems.append(
            "call_time_max_s must be a finite time above zero: the longest any one call takes"
            " to return or raise, whether or not the device answers"
        )
    if not isinstance(description.timestamp, TimestampSource):
        problems.append("timestamp must be a TimestampSource")

    capabilities = description.capabilities
    for name, value in vars(capabilities).items():
        if name != "comm_loss_timeout_s" and not isinstance(value, Support):
            problems.append(f"capabilities.{name} must be a Support value")
    timeout_s = capabilities.comm_loss_timeout_s
    if capabilities.safe_state_on_comm_loss is Support.YES:
        if timeout_s is None or not _finite(timeout_s) or timeout_s <= 0:
            problems.append(
                "capabilities.comm_loss_timeout_s must be set when safe_state_on_comm_loss is YES"
            )
    elif timeout_s is not None:
        problems.append(
            "capabilities.comm_loss_timeout_s must be None unless safe_state_on_comm_loss is YES"
        )

    if not description.safe_state.strip():
        problems.append("safe_state is not described")
    return problems


def check_signal_names(description: Description, signals: Iterable[str] | None) -> tuple[str, ...]:
    """The signal names a read should return; raises ``ContractError`` for an unknown one."""
    if signals is None:
        return description.signal_names
    if isinstance(signals, str):
        raise ContractError(f"signals must be a collection of names, not the string {signals!r}")
    names = tuple(signals)
    for name in names:
        description.signal(name)
    return names


def check_command(description: Description, name: str, value: float) -> None:
    """Raise ``CommandRejected`` unless ``value`` is a finite, in-range setpoint for ``name``."""
    try:
        info = description.command(name)
    except ContractError as error:
        raise CommandRejected(str(error)) from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise CommandRejected(f"{name} must be a finite number in SI units, got {value!r}")
    if not info.minimum <= value <= info.maximum:
        raise CommandRejected(
            f"{name} = {value!r} is outside the accepted range"
            f" {info.minimum!r} to {info.maximum!r} {info.dimension.si_unit}"
        )


def check_parameter_write(
    description: Description,
    name: str,
    value: float,
    confirmation: PersistentWriteConfirmation | None,
) -> ParameterInfo:
    """Raise unless writing ``value`` to ``name`` is allowed; return the parameter's info."""
    info = description.parameter(name)
    if not info.writable:
        raise ContractError(f"parameter {name!r} is read-only")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
        raise ContractError(f"{name} must be a finite number in SI units, got {value!r}")
    if (info.minimum is not None and value < info.minimum) or (
        info.maximum is not None and value > info.maximum
    ):
        raise ContractError(
            f"{name} = {value!r} is outside the accepted range"
            f" {info.minimum!r} to {info.maximum!r} {info.dimension.si_unit}"
        )
    if info.persistent and (
        confirmation is None or confirmation.parameter != name or confirmation.value != value
    ):
        raise PersistentWriteRefused(
            f"{name} is stored persistently on the device; writing it needs a human's"
            " confirmation of this exact parameter and value"
        )
    return info


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
