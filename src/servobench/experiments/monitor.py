"""The safety monitor: checks everything that happens during a run against the bench limits.

The monitor makes no device calls and keeps no clock of its own. The runner
tells it about every command it is about to send and has sent, every sample,
every read that got no reply, every fault poll, and the time; the monitor
answers with a ``Violation`` for the first thing that breaks a limit, or None.
The runner then stops the run and enters the safe state.

What it checks:

- On every sample: that every signal it needs is present and finite; position
  inside the travel range; speed, from the velocity reading when the adapter
  has one and always from the change in position, at most ``speed_max``;
  current at most ``current_max``; temperature at most ``temperature_max``;
  voltage inside its range.
- That the actuator follows its command. A reading compared only with the
  limits cannot see a servo that ignores its commands while standing still, or
  a position sensor that has frozen at a harmless value. So the monitor also
  stops the run when:

  * the position reading is more than ``follow_error_max`` outside the range
    of positions commanded during the last ``follow_time_max`` ("not
    following": a servo turning the wrong way, running away, or stuck), or
  * the commands have moved by more than ``follow_error_max`` and the position
    reading has not changed at all for ``follow_time_max`` since ("frozen
    reading": a sensor that stopped updating, even while the commands sweep
    back and forth around it, which the first check cannot see).

  Comparing with the range of recent commands, not the latest command, lets a
  servo lag behind a step or a sine without tripping the check.
- Missed replies, counted separately for reads and for fault polls: more than
  ``missed_replies_max`` reads in a row with no reply, or more than that many
  fault polls in a row. Only a good reading resets the count of reads, and
  only an answered fault poll the count of polls, so neither can hide the
  other.
- Blindness, in time: once the actuator is enabled, no more than
  ``sample_gap_max`` may pass without a good reading of every signal the
  monitor needs, whatever the time was lost to (missed replies, slow replies,
  a stalled call). A count of missed replies alone cannot bound this, because
  it does not know what a failed read costs. It is checked whenever the runner
  reports anything: a sample (the gap before it), a missed reply, a fault
  poll, and the time (after every command, and while the runner waits). And a
  plan that ends without a single good reading after enable (``check_end``)
  has not been watched. ``sample_gap_max`` is when the monitor decides, not
  when the actuator stops: the monitor sees the time only when the runner
  reports it, which is between adapter calls, so the stop request goes out up
  to one adapter call later (``runner.stop_time_max_s``), and later still if a
  call blocks for longer than the adapter declares. See "Stop time" in
  ``servobench.experiments.runner``.
- A fault reported by the device.
- The experiment running longer than ``experiment_max``.
- Before a command is sent, that it is finite and inside the command band (the
  travel range less ``command_margin``). The spec was already checked against
  this; this is the last check before the adapter.

What it cannot check: anything the readings do not show. A frozen reading while
the commands stay still looks like a servo holding still. A frozen velocity
reading is caught only through the position. Host-side checks are a second line
of defence; the device's own protections and the power supply are the first.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from servobench.contract import CURRENT, POSITION, TEMPERATURE, VELOCITY, VOLTAGE, Fault, Sample
from servobench.experiments.spec import command_band_rad
from servobench.limits import Limits, RunnerSettings
from servobench.units import Dimension, format_quantity

# Kinds of violation.
POSITION_LIMIT = "position"
SPEED_LIMIT = "speed"
CURRENT_LIMIT = "current"
TEMPERATURE_LIMIT = "temperature"
VOLTAGE_LIMIT = "voltage"
NOT_FOLLOWING = "not_following"
FROZEN_READING = "frozen_reading"
MISSED_REPLIES = "missed_replies"
SAMPLE_GAP = "sample_gap"
DEVICE_FAULT = "device_fault"
DURATION = "duration"
BAD_READING = "bad_reading"
COMMAND_OUT_OF_RANGE = "command_out_of_range"
VIOLATION_KINDS = (
    POSITION_LIMIT,
    SPEED_LIMIT,
    CURRENT_LIMIT,
    TEMPERATURE_LIMIT,
    VOLTAGE_LIMIT,
    NOT_FOLLOWING,
    FROZEN_READING,
    MISSED_REPLIES,
    SAMPLE_GAP,
    DEVICE_FAULT,
    DURATION,
    BAD_READING,
    COMMAND_OUT_OF_RANGE,
)

# The signals the monitor must be able to read to enforce the bench limits.
# Velocity is optional: speed is always also worked out from position.
MONITORED_SIGNALS = (POSITION, CURRENT, VOLTAGE, TEMPERATURE)

# Speed from position is the change in position over at least this long, so
# that one step of quantisation does not look like a burst of speed. With 4096
# steps per turn it adds at most about 9 deg/s of reading noise.
SPEED_BASELINE_S = 0.02


@dataclass(frozen=True)
class Violation:
    """The first thing that broke a limit. ``value`` and ``limit`` are SI, in ``unit``."""

    kind: str
    message: str
    t_s: float
    value: float | None = None
    limit: float | None = None
    unit: str | None = None


class Monitor:
    """Checks one run. Create a new one for every run, rehearsal included."""

    def __init__(self, limits: Limits, settings: RunnerSettings) -> None:
        self.limits = limits
        self.settings = settings
        self._band_rad = command_band_rad(limits)
        self._start_s: float | None = None
        # Missed replies in a row: reads (reset by a good reading) and fault
        # polls (reset by an answered poll), kept apart so neither hides the other.
        self._missed_reads = 0
        self._missed_polls = 0
        # When the last good reading was taken, and from when the actuator is
        # enabled (hold); the time without a reading is bounded from then on.
        self._last_good_s: float | None = None
        self._enabled_s: float | None = None
        self._last_command_rad: float | None = None
        # (t_s, position_rad) of the commands still inside the follow window,
        # plus the one in effect when the window starts.
        self._commands: deque[tuple[float, float]] = deque()
        # Recent position readings, for speed from position.
        self._positions: deque[tuple[float, float]] = deque()
        # Frozen-reading tracking, since the position reading last changed.
        self._last_reading_rad: float | None = None
        self._moved_low_rad = self._moved_high_rad = 0.0
        self._moved_since_s: float | None = None  # when the commands first moved too far

    # -- what the runner reports --------------------------------------------------

    def hold(self, t_s: float, position_rad: float) -> None:
        """The actuator is about to be enabled, which makes it hold ``position_rad``."""
        self._enabled_s = t_s
        self._commanded(t_s, position_rad)

    def start(self, t_s: float) -> None:
        """The experiment has started; its duration counts from ``t_s``."""
        self._start_s = t_s

    def check_command(self, t_s: float, position_rad: float) -> Violation | None:
        """Whether a position command may be sent. Called before sending it."""
        low_rad, high_rad = self._band_rad
        if not (isinstance(position_rad, float) and math.isfinite(position_rad)):
            return Violation(
                COMMAND_OUT_OF_RANGE, f"the runner computed a bad command {position_rad!r}", t_s
            )
        if not low_rad <= position_rad <= high_rad:
            return Violation(
                COMMAND_OUT_OF_RANGE,
                f"command {_deg(position_rad)} is outside the allowed command range"
                f" {_deg(low_rad)} to {_deg(high_rad)}; it was not sent",
                t_s,
                position_rad,
                high_rad if position_rad > high_rad else low_rad,
                "rad",
            )
        return None

    def on_command(self, t_s: float, position_rad: float) -> None:
        """A position command was sent at ``t_s``."""
        self._commanded(t_s, position_rad)

    def on_sample(self, sample: Sample) -> Violation | None:
        """A reading arrived. Its signals are checked, and so is the time since the last one."""
        self._missed_reads = 0
        t_s = sample.t_s
        values = sample.values
        for name in MONITORED_SIGNALS + ((VELOCITY,) if VELOCITY in values else ()):
            value = values.get(name)
            if value is None or not isinstance(value, float) or not math.isfinite(value):
                return Violation(BAD_READING, f"{name} reading is {value!r}, not a number", t_s)
        # A good reading ends a gap, but a gap that was already too long when
        # it ended still broke the bound: the actuator went unwatched.
        gap = self._check_duration(t_s) or self._check_gap(t_s, "before this reading")
        self._last_good_s = t_s
        return (
            self._check_position(t_s, values[POSITION])
            or self._check_speed(t_s, values)
            or self._check_current(t_s, values[CURRENT])
            or self._check_temperature(t_s, values[TEMPERATURE])
            or self._check_voltage(t_s, values[VOLTAGE])
            or self._check_following(t_s, values[POSITION])
            or self._check_frozen(t_s, values[POSITION])
            or gap
        )

    def on_missed(self, t_s: float, error: str) -> Violation | None:
        """A read got no usable reply."""
        self._missed_reads += 1
        return self._check_missed(t_s, self._missed_reads, "reads", error) or self._check_gap(
            t_s, f"{self._missed_reads} reads in a row got no reply; the last error: {error}"
        )

    def on_missed_poll(self, t_s: float, error: str) -> Violation | None:
        """A fault poll got no usable reply. It does not count as, or reset, a missed read."""
        self._missed_polls += 1
        return self._check_missed(t_s, self._missed_polls, "fault polls", error) or self._check_gap(
            t_s, f"a fault poll also got no reply ({error})"
        )

    def on_faults(self, t_s: float, faults: Iterable[Fault]) -> Violation | None:
        """A fault poll was answered. That is not a reading: it resets only the poll count."""
        self._missed_polls = 0
        faults = tuple(faults)
        if faults:
            listed = "; ".join(f"{fault.code}: {fault.description}" for fault in faults)
            return Violation(DEVICE_FAULT, f"the device reports a fault ({listed})", t_s)
        return self._check_gap(t_s, "fault polls were answered, but they are not readings")

    def check_time(self, t_s: float) -> Violation | None:
        """At ``t_s``: the experiment's duration, then the time since the last good reading."""
        return self._check_duration(t_s) or self._check_gap(t_s, "")

    def check_end(self, t_s: float) -> Violation | None:
        """The plan has ended at ``t_s``; was there a good reading after enable?"""
        start_s = self._start_s if self._start_s is not None else self._enabled_s
        if start_s is None or (self._last_good_s is not None and self._last_good_s > start_s):
            return None
        return Violation(
            SAMPLE_GAP,
            f"the plan ended {_s(t_s - start_s)} after enable without a single good reading"
            f" since ({self._missed_reads} reads in a row got no reply), so the safety monitor"
            " never saw the actuator while it was enabled",
            t_s,
            t_s - start_s,
            self.settings.sample_gap_max_s,
            "s",
        )

    # -- the checks ------------------------------------------------------------------

    def _check_duration(self, t_s: float) -> Violation | None:
        if self._start_s is None:
            return None
        elapsed_s = t_s - self._start_s
        limit_s = self.limits.experiment_max_s
        if elapsed_s > limit_s:
            return Violation(
                DURATION,
                f"the experiment has run for {_s(elapsed_s)}, longer than experiment_max"
                f" {_s(limit_s)}",
                t_s,
                elapsed_s,
                limit_s,
                "s",
            )
        return None

    def _check_missed(self, t_s: float, count: int, what: str, error: str) -> Violation | None:
        allowed = self.settings.missed_replies_max
        if count <= allowed:
            return None
        return Violation(
            MISSED_REPLIES,
            f"{count} {what} in a row got no reply (missed_replies_max {allowed});"
            f" the last error: {error}",
            t_s,
            float(count),
            float(allowed),
            "replies",
        )

    def _check_gap(self, t_s: float, context: str) -> Violation | None:
        """Whether the actuator has been enabled without a good reading for too long."""
        if self._enabled_s is None:
            return None  # not enabled yet: the missed-reply count alone applies
        since_s = self._last_good_s if self._last_good_s is not None else self._enabled_s
        gap_s = t_s - since_s
        limit_s = self.settings.sample_gap_max_s
        if gap_s <= limit_s:
            return None
        return Violation(
            SAMPLE_GAP,
            f"no good reading for {_s(gap_s)}, longer than sample_gap_max {_s(limit_s)}: the"
            " safety monitor could not see the actuator"
            + (f" ({context})" if context else ""),
            t_s,
            gap_s,
            limit_s,
            "s",
        )

    def _check_position(self, t_s: float, position_rad: float) -> Violation | None:
        low_rad, high_rad = self.limits.position_min_rad, self.limits.position_max_rad
        if low_rad <= position_rad <= high_rad:
            return None
        return Violation(
            POSITION_LIMIT,
            f"position {_deg(position_rad)} is outside the travel range {_deg(low_rad)} to"
            f" {_deg(high_rad)}",
            t_s,
            position_rad,
            high_rad if position_rad > high_rad else low_rad,
            "rad",
        )

    def _check_speed(self, t_s: float, values) -> Violation | None:
        limit_rad_s = self.limits.speed_max_rad_s
        if VELOCITY in values and abs(values[VELOCITY]) > limit_rad_s:
            return self._speed_violation(t_s, abs(values[VELOCITY]), "the velocity reading")
        positions = self._positions
        position_rad = values[POSITION]
        while len(positions) >= 2 and positions[1][0] <= t_s - SPEED_BASELINE_S:
            positions.popleft()
        positions.append((t_s, position_rad))
        then_s, then_rad = positions[0]
        if t_s - then_s >= SPEED_BASELINE_S:
            speed_rad_s = abs(position_rad - then_rad) / (t_s - then_s)
            if speed_rad_s > limit_rad_s:
                return self._speed_violation(
                    t_s, speed_rad_s, f"the change in position over {_s(t_s - then_s)}"
                )
        return None

    def _speed_violation(self, t_s: float, speed_rad_s: float, source: str) -> Violation:
        limit_rad_s = self.limits.speed_max_rad_s
        return Violation(
            SPEED_LIMIT,
            f"speed {_deg_s(speed_rad_s)} (from {source}) is above speed_max {_deg_s(limit_rad_s)}",
            t_s,
            speed_rad_s,
            limit_rad_s,
            "rad/s",
        )

    def _check_current(self, t_s: float, current_a: float) -> Violation | None:
        limit_a = self.limits.current_max_a
        if abs(current_a) <= limit_a:
            return None
        return Violation(
            CURRENT_LIMIT,
            f"current {_quantity(abs(current_a), Dimension.CURRENT)} is above current_max"
            f" {_quantity(limit_a, Dimension.CURRENT)}",
            t_s,
            abs(current_a),
            limit_a,
            "A",
        )

    def _check_temperature(self, t_s: float, temperature_degc: float) -> Violation | None:
        limit_degc = self.limits.temperature_max_degc
        if temperature_degc <= limit_degc:
            return None
        return Violation(
            TEMPERATURE_LIMIT,
            f"temperature {_quantity(temperature_degc, Dimension.TEMPERATURE)} is above"
            f" temperature_max {_quantity(limit_degc, Dimension.TEMPERATURE)}",
            t_s,
            temperature_degc,
            limit_degc,
            "degC",
        )

    def _check_voltage(self, t_s: float, voltage_v: float) -> Violation | None:
        low_v, high_v = self.limits.voltage_min_v, self.limits.voltage_max_v
        if low_v <= voltage_v <= high_v:
            return None
        return Violation(
            VOLTAGE_LIMIT,
            f"supply voltage {_quantity(voltage_v, Dimension.VOLTAGE)} is outside the range"
            f" {_quantity(low_v, Dimension.VOLTAGE)} to {_quantity(high_v, Dimension.VOLTAGE)}",
            t_s,
            voltage_v,
            high_v if voltage_v > high_v else low_v,
            "V",
        )

    def _check_following(self, t_s: float, position_rad: float) -> Violation | None:
        commands = self._commands
        if not commands:
            return None
        window_s = self.limits.follow_time_max_s
        while len(commands) >= 2 and commands[1][0] <= t_s - window_s:
            commands.popleft()
        low_rad = min(value for _, value in commands)
        high_rad = max(value for _, value in commands)
        outside_rad = max(low_rad - position_rad, position_rad - high_rad, 0.0)
        allowed_rad = self.limits.follow_error_max_rad
        if outside_rad <= allowed_rad:
            return None
        return Violation(
            NOT_FOLLOWING,
            f"the actuator is not following its commands: the position reading"
            f" {_deg(position_rad)} is {_deg(outside_rad)} outside the positions commanded"
            f" in the last {_s(window_s)} ({_deg(low_rad)} to {_deg(high_rad)}), more than"
            f" follow_error_max {_deg(allowed_rad)}. It may be ignoring its commands, turning"
            f" the wrong way, or its position reading may be wrong",
            t_s,
            outside_rad,
            allowed_rad,
            "rad",
        )

    def _check_frozen(self, t_s: float, position_rad: float) -> Violation | None:
        if self._last_reading_rad is None or position_rad != self._last_reading_rad:
            self._last_reading_rad = position_rad
            # Measure the commands' movement from the one in effect now (before
            # any command, from the reading itself).
            command_rad = self._last_command_rad
            self._moved_low_rad = self._moved_high_rad = (
                position_rad if command_rad is None else command_rad
            )
            self._moved_since_s = None
            return None
        if self._moved_since_s is None:
            return None
        unchanged_s = t_s - self._moved_since_s
        window_s = self.limits.follow_time_max_s
        if unchanged_s <= window_s:
            return None
        moved_rad = self._moved_high_rad - self._moved_low_rad
        return Violation(
            FROZEN_READING,
            f"the position reading has stayed at exactly {_deg(position_rad)} for"
            f" {_s(unchanged_s)} after the commands moved by {_deg(moved_rad)}, longer than"
            f" follow_time_max {_s(window_s)}. The position sensor may have stopped updating,"
            f" or the actuator is not moving",
            t_s,
            unchanged_s,
            window_s,
            "s",
        )

    def _commanded(self, t_s: float, position_rad: float) -> None:
        self._commands.append((t_s, position_rad))
        self._last_command_rad = position_rad
        if self._last_reading_rad is None:
            self._moved_low_rad = self._moved_high_rad = position_rad
            return
        self._moved_low_rad = min(self._moved_low_rad, position_rad)
        self._moved_high_rad = max(self._moved_high_rad, position_rad)
        if (
            self._moved_since_s is None
            and self._moved_high_rad - self._moved_low_rad > self.limits.follow_error_max_rad
        ):
            self._moved_since_s = t_s


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=4)


def _deg_s(value_rad_s: float) -> str:
    return format_quantity(value_rad_s, Dimension.ANGULAR_VELOCITY, "deg/s", digits=4)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=4)


def _quantity(value: float, dimension: Dimension) -> str:
    return format_quantity(value, dimension, digits=4)
