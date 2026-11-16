"""The bench file: load, validate, fingerprint.

``bench.toml`` belongs to the owner. The tool reads it and never writes it; the
one exception is ``write_template``, which creates a commented template and
refuses to overwrite an existing file.

The fingerprint is the SHA-256 of the file's exact bytes, so any edit to the
file, even a comment, shows up in the run records. It makes a change visible;
it does not prevent one. Real enforcement is the actuator's own protections and
the bench power supply's current limit.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from servobench.units import Dimension, UnitError, parse

BENCH_FILENAME = "bench.toml"


class BenchFileError(Exception):
    """The bench file is missing or invalid. Each problem names the field and what was expected."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__("\n".join(f"{source}: {problem}" for problem in problems))


@dataclass(frozen=True)
class Target:
    """Which actuator, and how to reach it."""

    adapter: str
    port: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Limits:
    """The limits every experiment must stay inside, in SI units."""

    position_min_rad: float
    position_max_rad: float
    speed_max_rad_s: float
    current_max_a: float
    temperature_max_degc: float
    voltage_min_v: float
    voltage_max_v: float
    experiment_max_s: float
    cooldown_s: float
    # No experiment starts while the actuator is hotter than this.
    start_temperature_max_degc: float
    # Every commanded position stays at least this far inside the travel range.
    command_margin_rad: float
    # The actuator must follow its command: the position reading may be at most
    # follow_error_max_rad outside the range of positions commanded during the
    # last follow_time_max_s, and must change within follow_time_max_s of a
    # commanded move larger than follow_error_max_rad.
    follow_error_max_rad: float
    follow_time_max_s: float


WAIT = "wait"
REFUSE = "refuse"
WHEN_TOO_WARM = (WAIT, REFUSE)


@dataclass(frozen=True)
class RunnerSettings:
    """How the experiment runner behaves, from the [runner] section."""

    # A run is stopped when more than this many reads in a row, or this many
    # fault polls in a row, get no reply.
    missed_replies_max: int
    # Speed of the ramp from where the actuator is to where the experiment starts.
    approach_speed_rad_s: float
    # When an experiment is due to start inside the cool-down, or while the
    # actuator is above start_temperature_max: "wait" (at rest, in the safe
    # state) or "refuse".
    when_too_warm: str
    # The longest the runner waits for the actuator to cool before refusing.
    cool_wait_max_s: float
    # The longest an enabled actuator may go without a good reading of the
    # signals the safety monitor checks, however the time was lost (missed
    # replies, slow replies, a stalled call). Also how far a run may overrun
    # its plan. An experiment must sample at least four times within it.
    sample_gap_max_s: float


@dataclass(frozen=True)
class CheckSettings:
    """How the check command judges the actuator, from the [check] section."""

    # Size of check's small move in the positive direction.
    move_rad: float
    # How far from zero the actuator may read at rest. The model, and so the
    # bench limits, assume the arm hangs straight down at position zero.
    zero_tolerance_rad: float
    # The most the temperature may rise during check's few seconds of small moves.
    temperature_rise_max_degc: float
    # The slowest reading rate that counts as sustaining what the other commands need.
    sample_rate_min_hz: float


@dataclass(frozen=True)
class Load:
    """What is attached to the actuator's output. For the bench rig, the pendulum arm."""

    mass_kg: float
    length_m: float


@dataclass(frozen=True)
class SimSettings:
    """Settings for the simulated servo. None means the simulator's own default.

    Fault injection is allowed only when the target is the simulated servo, so
    a fault meant for testing can never end up in a real run's rehearsal.
    """

    faults: tuple[str, ...] = ()
    seed: int | None = None
    noise: bool | None = None
    sample_rate_hz: float | None = None
    jitter_s: float | None = None
    supply_voltage_v: float | None = None
    ambient_temperature_degc: float | None = None
    initial_temperature_degc: float | None = None


@dataclass(frozen=True)
class Bench:
    target: Target
    limits: Limits
    runner: RunnerSettings
    check: CheckSettings
    load: Load | None
    fingerprint: str
    source: str
    sim: SimSettings = SimSettings()


# Each quantity field: key in bench.toml, attribute in the dataclass, dimension.
LIMIT_FIELDS: tuple[tuple[str, str, Dimension], ...] = (
    ("position_min", "position_min_rad", Dimension.ANGLE),
    ("position_max", "position_max_rad", Dimension.ANGLE),
    ("speed_max", "speed_max_rad_s", Dimension.ANGULAR_VELOCITY),
    ("current_max", "current_max_a", Dimension.CURRENT),
    ("temperature_max", "temperature_max_degc", Dimension.TEMPERATURE),
    ("voltage_min", "voltage_min_v", Dimension.VOLTAGE),
    ("voltage_max", "voltage_max_v", Dimension.VOLTAGE),
    ("experiment_max", "experiment_max_s", Dimension.TIME),
    ("cooldown", "cooldown_s", Dimension.TIME),
    ("start_temperature_max", "start_temperature_max_degc", Dimension.TEMPERATURE),
    ("command_margin", "command_margin_rad", Dimension.ANGLE),
    ("follow_error_max", "follow_error_max_rad", Dimension.ANGLE),
    ("follow_time_max", "follow_time_max_s", Dimension.TIME),
)

# The quantities of the [runner] section. Its other keys are missed_replies_max
# (a whole number of replies) and when_too_warm (a word).
RUNNER_FIELDS: tuple[tuple[str, str, Dimension], ...] = (
    ("approach_speed", "approach_speed_rad_s", Dimension.ANGULAR_VELOCITY),
    ("cool_wait_max", "cool_wait_max_s", Dimension.TIME),
    ("sample_gap_max", "sample_gap_max_s", Dimension.TIME),
)

CHECK_FIELDS: tuple[tuple[str, str, Dimension], ...] = (
    ("move", "move_rad", Dimension.ANGLE),
    ("zero_tolerance", "zero_tolerance_rad", Dimension.ANGLE),
    # A temperature difference: "3 degC" is a rise of three degrees.
    ("temperature_rise_max", "temperature_rise_max_degc", Dimension.TEMPERATURE),
    ("sample_rate_min", "sample_rate_min_hz", Dimension.FREQUENCY),
)

LOAD_FIELDS: tuple[tuple[str, str, Dimension], ...] = (
    ("mass", "mass_kg", Dimension.MASS),
    ("length", "length_m", Dimension.LENGTH),
)

SIM_FIELDS: tuple[tuple[str, str, Dimension], ...] = (
    ("sample_rate", "sample_rate_hz", Dimension.FREQUENCY),
    ("jitter", "jitter_s", Dimension.TIME),
    ("supply_voltage", "supply_voltage_v", Dimension.VOLTAGE),
    ("ambient_temperature", "ambient_temperature_degc", Dimension.TEMPERATURE),
    ("initial_temperature", "initial_temperature_degc", Dimension.TEMPERATURE),
)

# The faults the simulated servo can be told to have, and what each one does.
# The simulator (servobench.adapters.sim) implements exactly these.
SIM_FAULTS: dict[str, str] = {
    "reversed_direction": "the servo turns the opposite way to the position it is commanded to",
    "wrong_zero": "the servo's zero is 20 deg away from where the arm hangs at rest",
    "overheating": "the servo heats far faster than normal whenever its torque is on",
    "dropped_replies": "about one read in four gets no reply",
    "stuck_position": "the position reading never changes, although the servo moves",
    "runaway": "once enabled, the servo drives away at full torque and ignores position"
    " commands; turning torque off still works",
}

SIM_ADAPTER = "sim"
_TARGET_KEYS = ("adapter", "port", "id")
_SIM_KEYS = ("faults", "seed", "noise") + tuple(key for key, _, _ in SIM_FIELDS)
_RUNNER_KEYS = ("missed_replies_max", "when_too_warm") + tuple(key for key, _, _ in RUNNER_FIELDS)
_SECTIONS = ("target", "limits", "runner", "check", "load", "sim")


def fingerprint(data: bytes) -> str:
    """The fingerprint of a bench file's exact bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_bench(path: str | Path = BENCH_FILENAME) -> Bench:
    """Read, validate and fingerprint the bench file. Raises ``BenchFileError``."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise BenchFileError(
            str(path), ["not found; run the init command here to create a template, then edit it"]
        ) from None
    except OSError as error:
        raise BenchFileError(str(path), [f"cannot be read: {error.strerror}"]) from None
    return parse_bench(data, source=str(path))


def parse_bench(data: bytes, source: str = BENCH_FILENAME) -> Bench:
    """Validate bench-file contents. Every problem found is reported, not just the first."""
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise BenchFileError(source, [f"is not valid TOML: {error}"]) from None

    problems: list[str] = []
    for name in document:
        if name not in _SECTIONS:
            problems.append(f"unknown section [{name}]; known sections: {_names(_SECTIONS)}")

    target = _read_target(_section(document, "target", problems, required=True), problems)
    limits_table = _section(document, "limits", problems, required=True)
    limits = _read_quantities(limits_table, "limits", LIMIT_FIELDS, problems)
    runner = _read_runner(_section(document, "runner", problems, required=True), problems)
    check_table = _section(document, "check", problems, required=True)
    check = _read_quantities(check_table, "check", CHECK_FIELDS, problems)
    load_table = _section(document, "load", problems, required=False)
    load = _read_quantities(load_table, "load", LOAD_FIELDS, problems)

    sim_table = _section(document, "sim", problems, required=False)
    sim = _read_sim(sim_table, problems)
    if sim_table and target is not None and target.adapter != SIM_ADAPTER:
        problems.append(
            f"[sim] settings apply only to the simulated servo, but [target] adapter is"
            f' "{target.adapter}"; remove them, or set adapter = "{SIM_ADAPTER}"'
        )

    if limits is not None:
        _check_limits(limits, problems)
    if limits is not None and runner is not None:
        if runner.approach_speed_rad_s > limits["speed_max"]:
            problems.append("[runner] approach_speed must not be above [limits] speed_max")
    if check is not None:
        _check_check(check, limits, problems)
    if load is not None:
        _check_positive(load, "load", ("mass", "length"), problems)

    if (
        problems
        or target is None
        or limits is None
        or runner is None
        or check is None
        or sim is None
    ):
        raise BenchFileError(source, problems)
    return Bench(
        target=target,
        limits=Limits(**_by_attribute(limits, LIMIT_FIELDS)),
        runner=runner,
        check=CheckSettings(**_by_attribute(check, CHECK_FIELDS)),
        load=None if load is None else Load(**_by_attribute(load, LOAD_FIELDS)),
        fingerprint=fingerprint(data),
        source=source,
        sim=sim,
    )


def write_template(path: str | Path = BENCH_FILENAME) -> None:
    """Create a commented bench file. An existing file is left alone: ``FileExistsError``."""
    with open(path, "x", encoding="utf-8") as file:
        file.write(TEMPLATE)


def _section(document: dict, name: str, problems: list[str], *, required: bool) -> dict | None:
    table = document.get(name)
    if table is None:
        if required:
            problems.append(f"section [{name}] is missing")
        return None
    if not isinstance(table, dict):
        problems.append(f"[{name}] must be a section, not a single value")
        return None
    return table


def _read_target(table: dict | None, problems: list[str]) -> Target | None:
    if table is None:
        return None
    _check_unknown(table, "target", _TARGET_KEYS, problems)
    ok = True
    adapter = table.get("adapter")
    if adapter is None:
        problems.append('[target] adapter is missing; expected an adapter name, for example "sim"')
        ok = False
    elif not isinstance(adapter, str) or not adapter.strip():
        problems.append(f'[target] adapter must be an adapter name such as "sim", got {adapter!r}')
        ok = False
    port = table.get("port")
    if port is not None and not isinstance(port, str):
        problems.append(f'[target] port must be a string such as "/dev/ttyUSB0", got {port!r}')
        ok = False
    device_id = table.get("id")
    if device_id is not None and (
        isinstance(device_id, bool) or not isinstance(device_id, int) or device_id < 0
    ):
        problems.append(f"[target] id must be a whole number of 0 or more, got {device_id!r}")
        ok = False
    return Target(adapter=adapter, port=port, id=device_id) if ok else None


def _read_quantities(
    table: dict | None,
    section: str,
    fields: tuple[tuple[str, str, Dimension], ...],
    problems: list[str],
    *,
    check_unknown: bool = True,
) -> dict[str, float] | None:
    if table is None:
        return None
    if check_unknown:
        _check_unknown(table, section, tuple(key for key, _, _ in fields), problems)
    values: dict[str, float] = {}
    for key, _, dimension in fields:
        if key not in table:
            problems.append(
                f"[{section}] {key} is missing; expected {dimension.description},"
                f' for example "{dimension.example}"'
            )
            continue
        try:
            values[key] = parse(table[key], dimension, field=key)
        except UnitError as error:
            problems.append(f"[{section}] {error}")
    return values if len(values) == len(fields) else None


def _read_sim(table: dict | None, problems: list[str]) -> SimSettings | None:
    if table is None:
        return SimSettings()
    _check_unknown(table, "sim", _SIM_KEYS, problems)
    ok = True
    values: dict[str, object] = {}

    faults = table.get("faults", [])
    known = tuple(SIM_FAULTS)
    if not isinstance(faults, list) or not all(isinstance(fault, str) for fault in faults):
        problems.append(
            f'[sim] faults must be a list of fault names such as ["wrong_zero"],'
            f" got {faults!r}; known faults: {_names(known)}"
        )
        ok = False
    else:
        for fault in faults:
            if fault not in SIM_FAULTS:
                problems.append(f"[sim] unknown fault {fault!r}; known faults: {_names(known)}")
                ok = False
        if len(set(faults)) != len(faults):
            problems.append("[sim] faults lists the same fault more than once")
            ok = False
        values["faults"] = tuple(faults)

    seed = table.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
        problems.append(f"[sim] seed must be a whole number of 0 or more, got {seed!r}")
        ok = False
    values["seed"] = seed

    noise = table.get("noise")
    if noise is not None and not isinstance(noise, bool):
        problems.append(f"[sim] noise must be true or false, got {noise!r}")
        ok = False
    values["noise"] = noise

    for key, attribute, dimension in SIM_FIELDS:
        if key not in table:
            values[attribute] = None
            continue
        try:
            values[attribute] = parse(table[key], dimension, field=key)
        except UnitError as error:
            problems.append(f"[sim] {error}")
            ok = False
    for key, attribute, must_be_positive in (
        ("sample_rate", "sample_rate_hz", True),
        ("jitter", "jitter_s", False),
        ("supply_voltage", "supply_voltage_v", True),
    ):
        value = values.get(attribute)
        if isinstance(value, float):
            if must_be_positive and value <= 0:
                problems.append(f"[sim] {key} must be greater than zero")
                ok = False
            elif value < 0:
                problems.append(f"[sim] {key} must not be negative")
                ok = False
    return SimSettings(**values) if ok else None


def _read_runner(table: dict | None, problems: list[str]) -> RunnerSettings | None:
    if table is None:
        return None
    _check_unknown(table, "runner", _RUNNER_KEYS, problems)
    ok = True

    missed = table.get("missed_replies_max")
    if missed is None:
        problems.append(
            "[runner] missed_replies_max is missing; expected a whole number of replies, for"
            " example 3"
        )
        ok = False
    elif isinstance(missed, bool) or not isinstance(missed, int) or missed < 0:
        problems.append(
            f"[runner] missed_replies_max must be a whole number of replies, 0 or more,"
            f" got {missed!r}"
        )
        ok = False

    when = table.get("when_too_warm")
    if when is None:
        problems.append(
            f'[runner] when_too_warm is missing; expected "{WAIT}" or "{REFUSE}"'
        )
        ok = False
    elif when not in WHEN_TOO_WARM:
        problems.append(f'[runner] when_too_warm must be "{WAIT}" or "{REFUSE}", got {when!r}')
        ok = False

    quantities = _read_quantities(table, "runner", RUNNER_FIELDS, problems, check_unknown=False)
    if quantities is None:
        return None
    if quantities["approach_speed"] <= 0:
        problems.append("[runner] approach_speed must be greater than zero")
        ok = False
    if quantities["cool_wait_max"] < 0:
        problems.append("[runner] cool_wait_max must not be negative")
        ok = False
    if quantities["sample_gap_max"] <= 0:
        problems.append("[runner] sample_gap_max must be greater than zero")
        ok = False
    if not ok:
        return None
    return RunnerSettings(
        missed_replies_max=missed,
        when_too_warm=when,
        **_by_attribute(quantities, RUNNER_FIELDS),
    )


def _check_unknown(table: dict, section: str, known: tuple[str, ...], problems: list[str]) -> None:
    for key in table:
        if key not in known:
            problems.append(f"[{section}] unknown key {key!r}; known keys: {_names(known)}")


def _check_limits(limits: dict[str, float], problems: list[str]) -> None:
    if limits["position_min"] >= limits["position_max"]:
        problems.append("[limits] position_min must be less than position_max")
    if limits["voltage_min"] >= limits["voltage_max"]:
        problems.append("[limits] voltage_min must be less than voltage_max")
    _check_positive(
        limits,
        "limits",
        ("speed_max", "current_max", "experiment_max", "follow_error_max", "follow_time_max"),
        problems,
    )
    if limits["cooldown"] < 0:
        problems.append("[limits] cooldown must not be negative")
    if limits["start_temperature_max"] >= limits["temperature_max"]:
        problems.append("[limits] start_temperature_max must be less than temperature_max")
    margin = limits["command_margin"]
    if margin < 0:
        problems.append("[limits] command_margin must not be negative")
    elif limits["position_min"] + margin >= limits["position_max"] - margin:
        problems.append(
            "[limits] command_margin leaves no room to command: position_min + command_margin"
            " must be less than position_max - command_margin"
        )


def _check_check(
    check: dict[str, float], limits: dict[str, float] | None, problems: list[str]
) -> None:
    _check_positive(check, "check", tuple(key for key, _, _ in CHECK_FIELDS), problems)
    # An actuator that turns the wrong way moves as far below the commanded
    # range as it was asked to move above it. Keeping the move to half of
    # follow_error_max leaves room for its overshoot, so the run completes and
    # check names the reversed direction instead of the monitor aborting.
    if limits is not None and check["move"] > 0.5 * limits["follow_error_max"]:
        problems.append(
            "[check] move must be at most half of [limits] follow_error_max, so that an"
            " actuator turning the wrong way stays inside the follow check and check can name"
            " the problem instead of stopping the run"
        )


def _check_positive(
    values: dict[str, float], section: str, keys: tuple[str, ...], problems: list[str]
) -> None:
    for key in keys:
        if values[key] <= 0:
            problems.append(f"[{section}] {key} must be greater than zero")


def _by_attribute(
    values: dict[str, float], fields: tuple[tuple[str, str, Dimension], ...]
) -> dict[str, float]:
    """Rename bench-file keys to the SI-suffixed attribute names used inside the library."""
    return {attribute: values[key] for key, attribute, _ in fields}


def _names(names: tuple[str, ...]) -> str:
    return ", ".join(names)


TEMPLATE = """\
# bench.toml: the bench file for this actuator.
#
# You own this file. The tool reads it and never changes it.
#
# Every run records this file's fingerprint (the SHA-256 of its bytes), so an
# edit shows up in the run records. Nothing stops someone with a shell, a coding
# agent included, from editing it: the fingerprint makes a change visible, it
# does not prevent one. The real protection is the actuator's own limits, a
# bench power supply with its current limit set, and your hand on the power.
#
# Every quantity is a string with a unit, for example "45 deg" or "300 ms".
# A bare number is an error. The only bare numbers are counts and identifiers,
# which have no unit: missed_replies_max, id and seed.
#   angle: deg, rad    speed: deg/s, rad/s, rpm    time: ms, s, min
#   voltage: V, mV     current: A, mA              temperature: degC
#   mass: g, kg        length: mm, cm, m
#
# The values below are placeholders that suit the simulated servo. They are not
# facts about any real servo. Before connecting hardware, set every limit from
# the servo's datasheet, your bench rig and your power supply.

[target]
# Which adapter drives the actuator. "sim" is the built-in simulated servo and
# needs no hardware.
adapter = "sim"
# For a real servo, also give the serial port and the servo's ID, for example:
# port = "/dev/tty.usbserial-0001"
# id = 1

[limits]
# Travel range, measured from the actuator's zero position.
position_min = "-90 deg"
position_max = "90 deg"
# Fastest the output may turn.
speed_max = "180 deg/s"
# Highest current the actuator may draw.
current_max = "0.5 A"
# Highest temperature the actuator may reach.
temperature_max = "60 degC"
# Supply voltage range; outside it nothing runs.
voltage_min = "6 V"
voltage_max = "8.4 V"
# Longest single experiment, from the moment the actuator is enabled.
experiment_max = "30 s"
# Rest between one experiment and the next.
cooldown = "60 s"
# No experiment starts while the actuator is hotter than this. Keep it well
# below temperature_max, so an experiment has room to warm the actuator.
start_temperature_max = "45 degC"
# Every commanded position stays at least this far inside the travel range,
# leaving room for overshoot. With the values above: -80 deg to 80 deg.
command_margin = "10 deg"
# The actuator must follow its commands. A run stops if the position reading
# is more than follow_error_max outside the positions commanded during the
# last follow_time_max, or if the reading does not change at all within
# follow_time_max of a commanded move larger than follow_error_max. This
# catches a servo that ignores its commands, turns the wrong way, or whose
# position reading has frozen. follow_error_max must be larger than the
# overshoot and the sag under the load that you expect.
follow_error_max = "5 deg"
follow_time_max = "1 s"

[runner]
# How the experiment runner behaves.
#
# A run stops when more than this many reads in a row, or this many fault
# polls in a row, get no reply. A whole number of replies, written without
# quotes.
missed_replies_max = 3
# The longest the actuator may be enabled without a good reading of its
# position, current, voltage and temperature. A run stops when this passes,
# however the time was lost: missed replies, slow replies or a call that
# stalled. A run that falls this far behind its plan stops too. An experiment
# must sample at least four times within it, so with "200 ms" at 20 Hz or
# faster. Keep it short: for this long, only the actuator's own protections
# and the power supply act. 200 ms suits the simulated servo (three missed
# replies cost it about 90 ms); set it from your bus's reply timeout.
sample_gap_max = "200 ms"
# Before an experiment, the actuator is moved from where it is to where the
# experiment starts by a ramp at this speed. At most speed_max.
approach_speed = "20 deg/s"
# When an experiment is due to start inside the cool-down, or while the
# actuator is hotter than start_temperature_max: "wait" (at rest, in its safe
# state, for at most cool_wait_max) or "refuse".
when_too_warm = "wait"
cool_wait_max = "10 min"

[check]
# How the check command judges the actuator.
#
# check moves the actuator by this much in the positive direction and back,
# from where it is at rest, to see that a positive command increases the
# position. At most half of follow_error_max, so that an actuator turning the
# wrong way stays inside the follow check and check can say so.
move = "2.5 deg"
# At rest, with torque off, the actuator must read within this of zero. The
# tool assumes the arm hangs straight down at position zero, and the travel
# range above is measured from there. Friction can hold a pendulum arm a few
# degrees from hanging when torque is off; set this to what your rig allows.
zero_tolerance = "8 deg"
# The most the temperature may rise during check's few seconds of small moves.
# A healthy actuator barely warms in that time; set this to at least two of
# the steps in which your actuator reports temperature.
temperature_rise_max = "3 degC"
# The slowest rate at which the actuator must answer back-to-back reads.
# The analyses need at least 8 readings per cycle of a sine and several
# readings across a step's rise; 100 Hz allows sines up to 12.5 Hz.
sample_rate_min = "100 Hz"

[load]
# What is attached to the output. For the bench rig, the pendulum arm:
# its total mass, and the distance from the shaft axis to its centre of mass.
# Delete this section if nothing is attached.
mass = "50 g"
length = "100 mm"

[sim]
# Settings for the simulated servo, used only when adapter = "sim". Every
# setting is optional; remove the "#" to change one from its default.
#
# Faults to inject, for testing the tool's safety behaviour. Any of:
#   "reversed_direction"  the servo turns the opposite way to its commands
#   "wrong_zero"          the servo's zero is 20 deg from where the arm hangs
#   "overheating"         the servo heats far faster than normal when torque is on
#   "dropped_replies"     about one read in four gets no reply
#   "stuck_position"      the position reading never changes
#   "runaway"             once enabled, it drives away and ignores commands
# faults = ["wrong_zero"]
#
# Random seed. The same seed gives the same samples.
# seed = 0
# Measurement noise on every signal, true or false.
# noise = true
# Bus transactions per second, and the spread of the extra time each one takes.
# sample_rate = "200 Hz"
# jitter = "0.3 ms"
# Supply voltage with no load, room temperature, and the servo's temperature
# at the start (default: room temperature).
# supply_voltage = "7.4 V"
# ambient_temperature = "25 degC"
# initial_temperature = "25 degC"
"""
