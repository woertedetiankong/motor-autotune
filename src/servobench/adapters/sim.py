"""The simulated servo: the shared servo model behind the adapter contract.

It is treated as a real target: it has a bus with a finite transaction rate and
timing jitter, a quantised and noisy position reading, a supply voltage that
sags with current, a temperature that rises with effort, protections of its
own, and faults that can be injected from the bench file. It runs on a
``VirtualClock``, so it never sleeps and never reads the computer's clock: a
simulated experiment runs as fast as the computer allows, and the same seed
gives the same samples.

How time passes. Each bus transaction (a read, a command, enable, safe state,
a parameter read or write) takes one bus period, ``1 / sample_rate_hz``, plus a
random extra delay drawn from a half-normal distribution of scale ``jitter_s``.
The device acts on a request partway through the transaction: half a period
plus a random share of the extra delay after the request was sent. A sample is
stamped at the midpoint of its transaction (``TimestampSource.MIDPOINT``), so
with jitter on, the stamp is off from the true sampling instant by up to half
the extra delay, as it is on a real bus. A command takes effect at the time it
was sent plus the model's ``delay_s``. Between calls the model is not stepped;
it catches up to the clock at the next call, so waiting is free.

The bound on one call. No transaction lasts longer than ``reply_timeout_s``
(four bus periods unless set): a reply later than that counts as no reply
(``NoReply``, raised at the timeout), and a request that expects no reply is
given up at the timeout too, as a real adapter's serial timeout would. Every
call is one transaction, so the servo declares ``reply_timeout_s`` as its
``call_time_max_s``.

Every constant marked PLACEHOLDER below is an invented value chosen to make the
simulator behave plausibly. None of them is a fact about the HD1910 or any other
real servo.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from dataclasses import dataclass, replace

from servobench import __version__
from servobench.clock import Clock, VirtualClock
from servobench.contract import (
    CURRENT,
    POSITION,
    TEMPERATURE,
    VELOCITY,
    VOLTAGE,
    Adapter,
    Capabilities,
    CommandInfo,
    CommandRejected,
    CommunicationError,
    Description,
    DeviceFaulted,
    Fault,
    Identity,
    NoReply,
    ParameterInfo,
    PersistentWriteConfirmation,
    Sample,
    SignalInfo,
    Support,
    TimestampSource,
    check_command,
    check_parameter_write,
    check_signal_names,
)
from servobench.limits import SIM_ADAPTER, SIM_FAULTS, Bench, SimSettings
from servobench.model import (
    DEFAULT_PARAMS,
    NO_LOAD,
    Integrator,
    PendulumLoad,
    ServoModel,
    ServoParams,
)
from servobench.units import Dimension

TURN_RAD = 2.0 * math.pi

REVERSED_DIRECTION = "reversed_direction"
WRONG_ZERO = "wrong_zero"
OVERHEATING = "overheating"
DROPPED_REPLIES = "dropped_replies"
STUCK_POSITION = "stuck_position"
RUNAWAY = "runaway"
_IMPLEMENTED_FAULTS = frozenset(
    {REVERSED_DIRECTION, WRONG_ZERO, OVERHEATING, DROPPED_REPLIES, STUCK_POSITION, RUNAWAY}
)
if _IMPLEMENTED_FAULTS != frozenset(SIM_FAULTS):  # the bench-file schema and this file must agree
    raise ImportError("servobench.limits.SIM_FAULTS and the simulator's faults differ")

KP = "kp_n_m_rad"
KD = "kd_n_m_s_rad"
TEMPERATURE_CUTOFF = "temperature_cutoff_degc"


@dataclass(frozen=True)
class SimConfig:
    """Everything about the simulated servo except the model's own parameters."""

    seed: int = 0
    faults: frozenset[str] = frozenset()
    noise: bool = True
    sample_rate_hz: float = 200.0  # PLACEHOLDER bus transactions per second
    jitter_s: float = 0.3e-3  # PLACEHOLDER scale of the extra time per transaction
    supply_voltage_v: float = 7.4  # PLACEHOLDER supply with no load
    ambient_temperature_degc: float = 25.0
    initial_temperature_degc: float | None = None  # None: ambient
    initial_position_rad: float = 0.0  # where the arm starts; 0 is hanging straight down
    position_steps_per_turn: int | None = 4096  # reading resolution; None: not quantised
    # Backlash, an effect the servo model does not have, for testing the model
    # fit (plan step 8): play of this total width between the motor side, which
    # the internal controller moves and holds, and the output, which the
    # position reading measures. The output stays put until the motor side has
    # taken up the play, then follows it. Not settable from the bench file;
    # 0 (the default) leaves the simulator exactly as without it.
    backlash_rad: float = 0.0

    # Measurement noise, one standard deviation, when noise is on. PLACEHOLDERS.
    position_noise_rad: float = 0.3 * TURN_RAD / 4096
    velocity_noise_rad_s: float = 0.02
    current_noise_a: float = 0.005
    voltage_noise_v: float = 0.02
    temperature_noise_degc: float = 0.1

    # Electrical and thermal behaviour. PLACEHOLDERS.
    torque_constant_n_m_a: float = 2.5  # output torque per ampere of motor current
    idle_current_a: float = 0.04  # drawn by the electronics at all times
    supply_resistance_ohm: float = 0.3  # supply and wiring; the voltage sags by I * R
    thermal_time_constant_s: float = 60.0
    heating_k_per_n_m_squared: float = 150.0  # steady temperature rise per (N m)^2 of torque
    temperature_cutoff_degc: float = 70.0  # the device's own protection; latches a fault

    # The device's own limits and timing. PLACEHOLDERS.
    command_limit_rad: float = math.pi  # accepts position commands in [-limit, +limit]
    # How long a read waits for a reply that never comes. None: four bus periods.
    reply_timeout_s: float | None = None

    # Size of each injected fault. PLACEHOLDERS.
    wrong_zero_offset_rad: float = math.radians(20.0)
    dropped_reply_probability: float = 0.25
    overheating_rise_k: float = 200.0  # extra steady temperature rise while torque is on
    runaway_offset_rad: float = 10.0  # how far past the present position a runaway drives

    def __post_init__(self) -> None:
        unknown = set(self.faults) - _IMPLEMENTED_FAULTS
        if unknown:
            raise ValueError(f"unknown faults {sorted(unknown)}; known: {sorted(SIM_FAULTS)}")
        object.__setattr__(self, "faults", frozenset(self.faults))
        if not (math.isfinite(self.sample_rate_hz) and self.sample_rate_hz > 0):
            raise ValueError("sample_rate_hz must be above zero")
        if not (math.isfinite(self.jitter_s) and self.jitter_s >= 0):
            raise ValueError("jitter_s must be zero or more")
        if self.reply_timeout_s is None:
            object.__setattr__(self, "reply_timeout_s", 4.0 / self.sample_rate_hz)
        if not self.reply_timeout_s > 1.0 / self.sample_rate_hz:
            raise ValueError("reply_timeout_s must be longer than one bus period")
        if self.position_steps_per_turn is not None and self.position_steps_per_turn < 1:
            raise ValueError("position_steps_per_turn must be at least 1")
        if not 0 <= self.dropped_reply_probability < 1:
            raise ValueError("dropped_reply_probability must be in [0, 1)")
        if not (math.isfinite(self.backlash_rad) and self.backlash_rad >= 0):
            raise ValueError("backlash_rad must be zero or more")

    @classmethod
    def from_bench(cls, settings: SimSettings) -> SimConfig:
        """The [sim] section of the bench file, with the defaults for what it leaves out."""
        chosen = {
            "seed": settings.seed,
            "noise": settings.noise,
            "sample_rate_hz": settings.sample_rate_hz,
            "jitter_s": settings.jitter_s,
            "supply_voltage_v": settings.supply_voltage_v,
            "ambient_temperature_degc": settings.ambient_temperature_degc,
            "initial_temperature_degc": settings.initial_temperature_degc,
        }
        return cls(
            faults=frozenset(settings.faults),
            **{key: value for key, value in chosen.items() if value is not None},
        )


class SimServo(Adapter):
    """A simulated position servo with a pendulum load, behind the adapter contract."""

    # The model is stepped at least this often during long waits at rest, so
    # that temperature and the device's cutoff are tracked closely.
    _MAX_SKIP_S = 0.05

    def __init__(
        self,
        config: SimConfig = SimConfig(),
        *,
        params: ServoParams = DEFAULT_PARAMS,
        load: PendulumLoad = NO_LOAD,
        clock: VirtualClock | None = None,
    ) -> None:
        self.config = config
        self._clock = clock if clock is not None else VirtualClock()
        self._params = params
        self._load = load
        faults = config.faults
        self._direction = -1.0 if REVERSED_DIRECTION in faults else 1.0
        self._zero_offset_rad = config.wrong_zero_offset_rad if WRONG_ZERO in faults else 0.0
        self._period_s = 1.0 / config.sample_rate_hz
        self._half_play_rad = 0.5 * config.backlash_rad
        self._output_rad = config.initial_position_rad  # the output side of any backlash

        seed = config.seed
        self._timing = random.Random(f"{seed}:timing")
        self._noise = random.Random(f"{seed}:noise")
        self._drops = random.Random(f"{seed}:drops")

        self._temperature_degc = (
            config.ambient_temperature_degc
            if config.initial_temperature_degc is None
            else config.initial_temperature_degc
        )
        self._latched: dict[str, Fault] = {}
        self._enabled = False  # whether the host side has enabled since the last safe state
        self._closed = False
        self._integrator = Integrator(
            ServoModel(params, load),
            self._clock.now_s(),
            config.initial_position_rad,
            setpoint_rad=config.initial_position_rad,
            torque_on=False,
            on_step=self._device_step,
            max_skip_s=self._MAX_SKIP_S,
        )
        self._stuck_reading_rad = self._quantise(
            config.initial_position_rad + self._zero_offset_rad
        )
        self._description = self._build_description()

    @classmethod
    def from_bench(
        cls,
        bench: Bench,
        *,
        params: ServoParams = DEFAULT_PARAMS,
        clock: VirtualClock | None = None,
    ) -> SimServo:
        """The simulated servo with the bench file's load and [sim] settings."""
        return cls(
            SimConfig.from_bench(bench.sim),
            params=params,
            load=PendulumLoad.from_bench(bench.load),
            clock=clock,
        )

    # -- the contract -------------------------------------------------------

    @property
    def clock(self) -> Clock:
        return self._clock

    def describe(self) -> Description:
        return self._description

    def read(self, signals: Iterable[str] | None = None) -> Sample:
        names = check_signal_names(self._description, signals)
        t_sent_s, t_device_s, t_done_s = self._transaction(reply=True)
        position, velocity, torque = self._integrator.observe(t_device_s)
        values = {name: self._measure(name, position, velocity, torque) for name in names}
        return Sample(t_s=0.5 * (t_sent_s + t_done_s), values=values)

    def command(self, name: str, value: float) -> float:
        self._check_open()
        check_command(self._description, name, value)
        if not self._enabled:
            raise CommandRejected("not enabled: call enable() before sending commands")
        t_sent_s, _, _ = self._transaction(reply=False)
        if RUNAWAY not in self.config.faults:
            target_rad = self._direction * (float(value) - self._zero_offset_rad)
            self._integrator.set_setpoint(t_sent_s + self._params.delay_s, target_rad)
        return t_sent_s

    def enable(self) -> None:
        self._check_open()
        _, t_device_s, _ = self._transaction(reply=False)
        if self._latched:
            codes = ", ".join(sorted(self._latched))
            raise DeviceFaulted(
                f"the simulated servo has a latched fault ({codes}) and will not enable;"
                " a human must power-cycle it (create a new simulated servo)"
            )
        position, _, _ = self._integrator.observe(t_device_s)
        hold_rad = position + (
            self.config.runaway_offset_rad if RUNAWAY in self.config.faults else 0
        )
        self._integrator.cancel_setpoints_after(t_device_s)
        self._integrator.set_setpoint(t_device_s, hold_rad)
        self._integrator.set_torque_on(t_device_s, True)
        self._enabled = True

    def enter_safe_state(self) -> None:
        self._check_open()
        _, t_device_s, _ = self._transaction(reply=False)
        self._integrator.set_torque_on(t_device_s, False)
        self._integrator.cancel_setpoints_after(t_device_s)
        self._enabled = False

    def read_faults(self) -> tuple[Fault, ...]:
        _, t_device_s, _ = self._transaction(reply=True)
        self._integrator.advance_to(t_device_s)
        return tuple(self._latched[code] for code in sorted(self._latched))

    def read_parameter(self, name: str) -> float:
        self._check_open()
        self._description.parameter(name)
        self._transaction(reply=True)
        if name == KP:
            return self._params.kp_n_m_rad
        if name == KD:
            return self._params.kd_n_m_s_rad
        return self.config.temperature_cutoff_degc

    def write_parameter(
        self,
        name: str,
        value: float,
        *,
        confirmation: PersistentWriteConfirmation | None = None,
    ) -> None:
        self._check_open()
        check_parameter_write(self._description, name, value, confirmation)
        _, t_device_s, _ = self._transaction(reply=False)
        self._integrator.advance_to(t_device_s)
        if name == KP:
            self._set_params(replace(self._params, kp_n_m_rad=float(value)))
        elif name == KD:
            self._set_params(replace(self._params, kd_n_m_s_rad=float(value)))
        else:
            self.config = replace(self.config, temperature_cutoff_degc=float(value))

    def close(self) -> None:
        self._closed = True

    # -- the bus ------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise CommunicationError("the simulated servo's connection is closed")

    def _transaction(self, *, reply: bool) -> tuple[float, float, float]:
        """One bus transaction. Returns (sent, device acts, done) times; may raise NoReply."""
        self._check_open()
        t_sent_s = self._clock.now_s()
        extra_s = (
            abs(self._timing.gauss(0.0, self.config.jitter_s)) if self.config.jitter_s else 0.0
        )
        t_device_s = t_sent_s + 0.5 * self._period_s + extra_s * self._timing.random()
        # A reply later than the timeout counts as no reply, as it would on a real bus.
        timeout_s = self.config.reply_timeout_s
        t_done_s = t_sent_s + self._period_s + extra_s
        late = t_done_s > t_sent_s + timeout_s
        dropped = (
            DROPPED_REPLIES in self.config.faults
            and self._drops.random() < self.config.dropped_reply_probability
        )
        if reply and (dropped or late):
            self._integrator.advance_to(t_device_s)
            self._clock.wait_until_s(t_sent_s + timeout_s)
            raise NoReply(f"the simulated servo did not reply within {timeout_s} s")
        if late:
            # The request went out; the host stops waiting for its acknowledgement
            # at the timeout, so no call lasts longer than call_time_max_s.
            t_done_s = t_sent_s + timeout_s
            t_device_s = min(t_device_s, t_done_s)
        self._clock.wait_until_s(t_done_s)
        return t_sent_s, t_device_s, t_done_s

    # -- the device ---------------------------------------------------------

    def _set_params(self, params: ServoParams) -> None:
        self._params = params
        self._integrator.model = ServoModel(params, self._load)

    def _device_step(
        self, start_s: float, duration_s: float, steps: int, torque_n_m: float, torque_on: bool
    ) -> None:
        """The device's own temperature model and cutoff, run for every committed model step."""
        config = self.config
        if self._half_play_rad:
            self._output_rad = self._play(self._integrator.position_rad)
        rise_k = config.heating_k_per_n_m_squared * torque_n_m * torque_n_m
        if torque_on and OVERHEATING in config.faults:
            rise_k += config.overheating_rise_k
        target_degc = config.ambient_temperature_degc + rise_k
        share = -math.expm1(-duration_s / config.thermal_time_constant_s)
        self._temperature_degc += (target_degc - self._temperature_degc) * share
        if self._temperature_degc >= config.temperature_cutoff_degc and "overheat" not in (
            self._latched
        ):
            self._latched["overheat"] = Fault(
                "overheat",
                f"temperature reached the device cutoff of {config.temperature_cutoff_degc} degC;"
                " torque is off until a human power-cycles the servo",
            )
            self._integrator.torque_on = False

    def _measure(
        self, name: str, position_rad: float, velocity_rad_s: float, torque: float
    ) -> float:
        config = self.config
        if name == POSITION:
            if STUCK_POSITION in config.faults:
                return self._stuck_reading_rad
            if self._half_play_rad:
                position_rad = self._play(position_rad)
            return self._quantise(
                position_rad + self._zero_offset_rad + self._gauss(config.position_noise_rad)
            )
        if name == VELOCITY:
            return velocity_rad_s + self._gauss(config.velocity_noise_rad_s)
        current_a = config.idle_current_a + abs(torque) / config.torque_constant_n_m_a
        if name == CURRENT:
            return max(0.0, current_a + self._gauss(config.current_noise_a))
        if name == VOLTAGE:
            sag_v = current_a * config.supply_resistance_ohm
            return config.supply_voltage_v - sag_v + self._gauss(config.voltage_noise_v)
        if name == TEMPERATURE:
            return self._temperature_degc + self._gauss(config.temperature_noise_degc)
        raise AssertionError(f"undeclared signal {name!r}")  # check_signal_names prevents this

    def _play(self, motor_rad: float) -> float:
        """Where the output is, behind the backlash, with the motor side at ``motor_rad``."""
        half = self._half_play_rad
        return min(max(self._output_rad, motor_rad - half), motor_rad + half)

    def _gauss(self, sigma: float) -> float:
        return self._noise.gauss(0.0, sigma) if self.config.noise and sigma > 0 else 0.0

    def _quantise(self, position_rad: float) -> float:
        steps = self.config.position_steps_per_turn
        if steps is None:
            return position_rad
        step_rad = TURN_RAD / steps
        return round(position_rad / step_rad) * step_rad

    def _build_description(self) -> Description:
        config = self.config
        steps = config.position_steps_per_turn
        resolution = f", in steps of 1/{steps} turn" if steps else ""
        return Description(
            identity=Identity(
                adapter=SIM_ADAPTER,
                model="servobench simulated position servo",
                device_id="1",
                firmware=__version__,
                simulated=True,
            ),
            signals=(
                SignalInfo(POSITION, Dimension.ANGLE, f"output angle from zero{resolution}"),
                SignalInfo(VELOCITY, Dimension.ANGULAR_VELOCITY, "output angular velocity"),
                SignalInfo(CURRENT, Dimension.CURRENT, "current drawn from the supply, 0 or more"),
                SignalInfo(VOLTAGE, Dimension.VOLTAGE, "supply voltage at the servo"),
                SignalInfo(TEMPERATURE, Dimension.TEMPERATURE, "motor temperature"),
            ),
            commands=(
                CommandInfo(
                    POSITION,
                    Dimension.ANGLE,
                    -config.command_limit_rad,
                    config.command_limit_rad,
                    "target output angle",
                ),
            ),
            parameters=(
                ParameterInfo(
                    KP,
                    Dimension.ROTATIONAL_STIFFNESS,
                    writable=True,
                    persistent=False,
                    description="proportional gain of the position controller",
                    minimum=0.0,
                    maximum=100.0,
                ),
                ParameterInfo(
                    KD,
                    Dimension.ROTATIONAL_DAMPING,
                    writable=True,
                    persistent=False,
                    description="derivative gain of the position controller",
                    minimum=0.0,
                    maximum=10.0,
                ),
                ParameterInfo(
                    TEMPERATURE_CUTOFF,
                    Dimension.TEMPERATURE,
                    writable=True,
                    persistent=True,
                    description="temperature at which the servo turns torque off and latches",
                    minimum=0.0,
                    maximum=100.0,
                ),
            ),
            max_sample_rate_hz=config.sample_rate_hz,
            timestamp=TimestampSource.MIDPOINT,
            call_time_max_s=config.reply_timeout_s,
            capabilities=Capabilities(
                position_limit=Support.YES,
                speed_limit=Support.NO,
                torque_limit=Support.YES,
                temperature_cutoff=Support.YES,
                voltage_cutoff=Support.NO,
                safe_state_on_comm_loss=Support.NO,
                fault_latching=Support.YES,
                onboard_capture=Support.NO,
                onboard_test_signals=Support.NO,
            ),
            safe_state="torque off: the output turns freely and a pendulum load swings down"
            " to hanging",
        )
