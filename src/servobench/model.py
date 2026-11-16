"""The servo model shared by the simulated servo and the model fit.

A position servo driving a pendulum load, about the output shaft:

    J * dv/dt = tau_m + tau_g - b * v - tau_c * sign(v)
    dtheta/dt = v

    J     = inertia_kg_m2 (the servo's own, reflected to the output)
            + mass_kg * com_distance_m**2 (the load, taken as a point mass)
    tau_m = clamp(kp * (r(t - delay) - theta) - kd * v, -torque_max, +torque_max)
            when torque is on, 0 when it is off
    tau_g = -mass_kg * g * com_distance_m * sin(theta)
    tau_c * sign(v) is Coulomb friction; at v = 0 it holds anything up to tau_c
            (stiction), so the shaft can stop and stay stopped.

``theta`` is the output angle, with zero where the pendulum arm hangs straight
down. ``r`` is the commanded position, held between commands, and ``delay`` is
the time from the adapter sending a command to the servo's controller acting on
it, transport included. The derivative acts on the measured velocity, not on the
error, so a step command gives no derivative kick.

The servo's parameters (``ServoParams``) are the model's unknowns, which the
fit estimates. The load (``PendulumLoad``) comes from the bench file.

Integration. A fixed grid of ``dt_s``, default 0.5 ms (2 kHz): ten times the
simulated servo's default bus rate, eight steps inside the default command
delay, and about 1/470 of the natural period of the default servo with the
template's 50 g, 100 mm arm (natural frequency about 27 rad/s). Each step is
semi-implicit Euler. The controller and friction terms are implicit: the new
velocity is solved for with the derivative gain, viscous and Coulomb friction
taken at the new velocity and the proportional term at the new position
(backward Euler for the linear part), and the position is then advanced with
the new velocity. Gravity is taken at the start of the
step, which makes the free pendulum symplectic Euler (no energy drift). The
implicit equation is piecewise linear and strictly monotonic in the new
velocity because of the torque limit, so it is solved exactly in closed form,
with no iteration. The controller part is therefore stable for any gains,
inertia and time step, so the fit can try any parameters without the
integration blowing up; the explicit gravity term only needs dt_s below about
2 / sqrt(g / com_distance_m), which is 0.2 s for a 100 mm arm. Coulomb friction
is treated as a set-valued force, which gives true sticking at rest instead of
the chatter an explicit sign(v) produces. The command delay is not rounded to
the grid: a step during which the delayed command changes uses the
time-average of the command over the step, so the response varies smoothly
with the delay and with command times. Values between grid points are
interpolated linearly. The scheme is first-order accurate: halving dt_s changes
a 10 deg step response by less than 0.05 deg.

``Integrator`` is the one stepping engine. The simulated servo uses it live,
feeding commands as they arrive; ``simulate`` replays a recorded command
history through it with any parameters, which is what the fit does.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields

import numpy as np

from servobench.limits import Load

GRAVITY_M_S2 = 9.80665  # standard gravity
DEFAULT_DT_S = 0.0005


@dataclass(frozen=True)
class ServoParams:
    """The servo's own parameters: the unknowns of the model."""

    kp_n_m_rad: float  # proportional gain of the internal position controller
    kd_n_m_s_rad: float  # derivative gain, acting on velocity
    torque_max_n_m: float  # the controller's torque limit
    inertia_kg_m2: float  # the servo's own inertia, reflected to the output shaft
    viscous_n_m_s_rad: float  # viscous friction, including any back-EMF damping
    coulomb_n_m: float  # Coulomb (dry) friction
    delay_s: float  # from the adapter sending a command to the controller acting on it

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{item.name} must be a finite number of zero or more")
        if self.inertia_kg_m2 <= 0:
            raise ValueError("inertia_kg_m2 must be greater than zero")
        if self.torque_max_n_m <= 0:
            raise ValueError("torque_max_n_m must be greater than zero")


# PLACEHOLDER VALUES. These are not measurements of the HD1910 or of any other
# real servo, and nothing about a real servo may be concluded from them. They
# were chosen so that the simulated servo behaves like a small hobby-class
# position servo and suits the template bench file (50 g arm at 100 mm): a
# 10 deg step rises in about 80 ms, overshoots about 3 % and settles in about
# 0.2 s, staying under the template's 180 deg/s and 0.5 A; a step larger than
# about 32 deg (kp * error > torque_max) saturates the torque limit. The torque
# limit is of the order of the roughly 1 N m (10 kg cm) stall torque that
# retailers report for the HD1910 (docs/targets.md, "reported"), and the rest
# follow from the response above. Real values come from `characterize` on the
# real servo (plan step 12).
DEFAULT_PARAMS = ServoParams(
    kp_n_m_rad=1.8,
    kd_n_m_s_rad=0.06,
    torque_max_n_m=1.0,
    inertia_kg_m2=2.0e-3,
    viscous_n_m_s_rad=0.03,
    coulomb_n_m=0.005,
    delay_s=0.004,
)


@dataclass(frozen=True)
class PendulumLoad:
    """What is attached to the output: an arm hanging straight down at position zero.

    The arm is taken as a point mass at its centre of mass, so its inertia about
    the shaft is ``mass_kg * com_distance_m**2``. A real arm has a little more.
    """

    mass_kg: float = 0.0
    com_distance_m: float = 0.0

    def __post_init__(self) -> None:
        for name in ("mass_kg", "com_distance_m"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite number of zero or more")

    @property
    def inertia_kg_m2(self) -> float:
        return self.mass_kg * self.com_distance_m**2

    @property
    def gravity_torque_max_n_m(self) -> float:
        """The gravity torque with the arm horizontal."""
        return self.mass_kg * GRAVITY_M_S2 * self.com_distance_m

    @classmethod
    def from_bench(cls, load: Load | None) -> PendulumLoad:
        """The load section of the bench file; no section means nothing attached."""
        if load is None:
            return cls()
        return cls(mass_kg=load.mass_kg, com_distance_m=load.length_m)


NO_LOAD = PendulumLoad()


class ServoModel:
    """The equations of motion, and one integration step of them."""

    def __init__(self, params: ServoParams, load: PendulumLoad = NO_LOAD) -> None:
        self.params = params
        self.load = load
        self.inertia_kg_m2 = params.inertia_kg_m2 + load.inertia_kg_m2
        self._gravity_n_m = load.gravity_torque_max_n_m

    def gravity_torque_n_m(self, position_rad: float) -> float:
        return -self._gravity_n_m * math.sin(position_rad)

    def step(
        self,
        position_rad: float,
        velocity_rad_s: float,
        setpoint_rad: float,
        torque_on: bool,
        dt_s: float,
    ) -> tuple[float, float, float]:
        """Advance one step of ``dt_s``. Returns (position_rad, velocity_rad_s, torque_n_m).

        ``setpoint_rad`` is the delayed command averaged over the step. The
        returned torque is the motor torque applied during the step.
        """
        p = self.params
        inertia_s = self.inertia_kg_m2 / dt_s
        a = inertia_s + p.viscous_n_m_s_rad
        b = inertia_s * velocity_rad_s - self._gravity_n_m * math.sin(position_rad)
        if torque_on:
            # tau_m(v) = clamp(prop - damp * v): kp * (r - theta_new) - kd * v_new.
            prop = p.kp_n_m_rad * (setpoint_rad - position_rad)
            damp = p.kd_n_m_s_rad + p.kp_n_m_rad * dt_s
            limit = p.torque_max_n_m
        else:
            prop = damp = limit = 0.0
        friction = p.coulomb_n_m

        # Solve a*v - b - tau_m(v) + friction*sign(v) = 0 for the new velocity v.
        # The left side is strictly increasing in v, so there is one solution.
        at_rest = -b - _clamp(prop, limit)
        if -friction <= at_rest <= friction:
            new_velocity = 0.0  # friction holds the shaft
        else:
            push = b - (friction if at_rest < 0 else -friction)
            new_velocity = (push + prop) / (a + damp)
            unclamped = prop - damp * new_velocity
            if unclamped > limit:
                new_velocity = (push + limit) / a
            elif unclamped < -limit:
                new_velocity = (push - limit) / a
        torque = _clamp(prop - damp * new_velocity, limit)
        return position_rad + dt_s * new_velocity, new_velocity, torque


def _clamp(value: float, limit: float) -> float:
    return limit if value > limit else -limit if value < -limit else value


class _Schedule:
    """The setpoint as a function of time: piecewise constant, queried forwards in time."""

    def __init__(self, value: float) -> None:
        self._value = value  # in effect from the last popped change on
        self._changes: list[tuple[float, float]] = []  # (t_s, value), sorted by time

    def add(self, t_s: float, value: float) -> None:
        bisect.insort(self._changes, (t_s, value), key=lambda change: change[0])

    def cancel_after(self, t_s: float) -> None:
        self._changes = [change for change in self._changes if change[0] <= t_s]

    def next_change_s(self) -> float:
        return self._changes[0][0] if self._changes else math.inf

    def average(self, t0_s: float, t1_s: float) -> tuple[float, bool]:
        """The mean setpoint over [t0_s, t1_s], and whether it changes inside the interval."""
        changes = self._changes
        while changes and changes[0][0] <= t0_s:
            self._value = changes.pop(0)[1]
        if not changes or changes[0][0] >= t1_s:
            return self._value, False
        total = 0.0
        t_s, value = t0_s, self._value
        for change_t_s, change_value in changes:
            if change_t_s >= t1_s:
                break
            total += value * (change_t_s - t_s)
            t_s, value = change_t_s, change_value
        total += value * (t1_s - t_s)
        return total / (t1_s - t0_s), True


# Called for every committed run of steps:
# (start time s, duration s, number of steps, motor torque N m, torque on).
StepCallback = Callable[[float, float, int, float, bool], None]


class Integrator:
    """Steps a ``ServoModel`` on a fixed time grid and answers for any time.

    Steps are committed in order up to the latest time asked about; a time
    between grid points is answered by interpolating a tentative step, which is
    recomputed if a command arrives before the next grid point. A step whose
    result equals its start (the shaft held by friction, a constant setpoint)
    is a fixed point, so the integrator jumps over the steps until the next
    setpoint change: exact, and fast during long waits at rest. ``max_skip_s``
    bounds one jump, so that ``on_step`` (which may model something that does
    change, such as temperature) is called at least that often.
    """

    def __init__(
        self,
        model: ServoModel,
        t0_s: float,
        position_rad: float,
        velocity_rad_s: float = 0.0,
        *,
        setpoint_rad: float | None = None,
        torque_on: bool = True,
        dt_s: float = DEFAULT_DT_S,
        on_step: StepCallback | None = None,
        max_skip_s: float = math.inf,
    ) -> None:
        if not (math.isfinite(dt_s) and dt_s > 0):
            raise ValueError("dt_s must be a finite time above zero")
        if not max_skip_s > 0:
            raise ValueError("max_skip_s must be above zero")
        self._max_skip_s = max_skip_s
        self.model = model
        self.dt_s = dt_s
        self._t0_s = t0_s
        self._steps = 0
        self.position_rad = position_rad
        self.velocity_rad_s = velocity_rad_s
        self.torque_n_m = 0.0
        self.torque_on = torque_on
        self._schedule = _Schedule(position_rad if setpoint_rad is None else setpoint_rad)
        self._on_step = on_step

    @property
    def t_s(self) -> float:
        """The time of the last committed grid point."""
        return self._t0_s + self._steps * self.dt_s

    def set_setpoint(self, t_s: float, setpoint_rad: float) -> None:
        """The setpoint becomes ``setpoint_rad`` from ``t_s`` on (no earlier than the grid)."""
        self._schedule.add(max(t_s, self.t_s), setpoint_rad)

    def cancel_setpoints_after(self, t_s: float) -> None:
        """Forget setpoint changes scheduled after ``t_s``."""
        self._schedule.cancel_after(t_s)

    def set_torque_on(self, t_s: float, torque_on: bool) -> None:
        """Switch torque on or off from the first grid point at or after ``t_s``."""
        self.advance_to(t_s)
        if self.t_s < t_s:
            self._commit_step()
        self.torque_on = torque_on

    def advance_to(self, t_s: float) -> None:
        """Commit every step that ends at or before ``t_s``."""
        dt_s = self.dt_s
        while self.t_s + dt_s <= t_s:
            start_position, start_velocity = self.position_rad, self.velocity_rad_s
            changed = self._commit_step()
            if (
                not changed
                and start_velocity == 0.0
                and self.velocity_rad_s == 0.0
                and self.position_rad == start_position
            ):
                self._skip_fixed_point(t_s)

    def observe(self, t_s: float) -> tuple[float, float, float]:
        """(position_rad, velocity_rad_s, torque_n_m) at ``t_s``, which must not go backwards."""
        if t_s < self.t_s:
            raise ValueError(f"cannot observe at {t_s!r} s, before the grid time {self.t_s!r} s")
        self.advance_to(t_s)
        if t_s == self.t_s:
            return self.position_rad, self.velocity_rad_s, self.torque_n_m
        start_s = self.t_s
        setpoint, _ = self._schedule.average(start_s, start_s + self.dt_s)
        position, velocity, torque = self.model.step(
            self.position_rad, self.velocity_rad_s, setpoint, self.torque_on, self.dt_s
        )
        fraction = (t_s - start_s) / self.dt_s
        return (
            self.position_rad + fraction * (position - self.position_rad),
            self.velocity_rad_s + fraction * (velocity - self.velocity_rad_s),
            torque,
        )

    def _commit_step(self) -> bool:
        start_s = self.t_s
        setpoint, changed = self._schedule.average(start_s, start_s + self.dt_s)
        self.position_rad, self.velocity_rad_s, self.torque_n_m = self.model.step(
            self.position_rad, self.velocity_rad_s, setpoint, self.torque_on, self.dt_s
        )
        self._steps += 1
        if self._on_step is not None:
            self._on_step(start_s, self.dt_s, 1, self.torque_n_m, self.torque_on)
        return changed

    def _skip_fixed_point(self, t_s: float) -> None:
        end_s = min(t_s, self._schedule.next_change_s(), self.t_s + self._max_skip_s)
        count = math.floor((end_s - self.t_s) / self.dt_s)
        # Guard against rounding: never pass a step that ends after end_s.
        while count > 0 and self._t0_s + (self._steps + count) * self.dt_s > end_s:
            count -= 1
        if count <= 0:
            return
        start_s = self.t_s
        self._steps += count
        if self._on_step is not None:
            self._on_step(start_s, count * self.dt_s, count, self.torque_n_m, self.torque_on)


@dataclass(frozen=True)
class Response:
    """The model's response at the requested times."""

    t_s: np.ndarray
    position_rad: np.ndarray
    velocity_rad_s: np.ndarray
    torque_n_m: np.ndarray


def simulate(
    params: ServoParams,
    load: PendulumLoad,
    command_times_s: Sequence[float],
    command_positions_rad: Sequence[float],
    sample_times_s: Sequence[float],
    *,
    initial_position_rad: float,
    initial_velocity_rad_s: float = 0.0,
    initial_setpoint_rad: float | None = None,
    dt_s: float = DEFAULT_DT_S,
) -> Response:
    """Replay recorded position commands through the model, torque on throughout.

    ``command_times_s`` are the times the commands were sent, on the same clock
    as ``sample_times_s``; each takes effect ``params.delay_s`` later. The
    model starts at the first sample time in the given state, holding
    ``initial_setpoint_rad`` (default: the initial position, as on enabling).
    Both time sequences must be in order.
    """
    times = np.asarray(sample_times_s, dtype=float)
    if times.ndim != 1 or times.size == 0:
        raise ValueError("sample_times_s must be a non-empty sequence of times")
    if np.any(np.diff(times) < 0):
        raise ValueError("sample_times_s must be in order")
    if len(command_times_s) != len(command_positions_rad):
        raise ValueError("command_times_s and command_positions_rad differ in length")
    if any(b < a for a, b in zip(command_times_s, command_times_s[1:], strict=False)):
        raise ValueError("command_times_s must be in order")

    integrator = Integrator(
        ServoModel(params, load),
        float(times[0]),
        initial_position_rad,
        initial_velocity_rad_s,
        setpoint_rad=initial_setpoint_rad,
        dt_s=dt_s,
    )
    for t_s, position_rad in zip(command_times_s, command_positions_rad, strict=True):
        integrator.set_setpoint(float(t_s) + params.delay_s, float(position_rad))

    out = np.empty((3, times.size))
    for index, t_s in enumerate(times):
        out[:, index] = integrator.observe(float(t_s))
    return Response(times, out[0], out[1], out[2])
