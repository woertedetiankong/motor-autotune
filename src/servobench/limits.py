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


@dataclass(frozen=True)
class Load:
    """What is attached to the actuator's output. For the bench rig, the pendulum arm."""

    mass_kg: float
    length_m: float


@dataclass(frozen=True)
class Bench:
    target: Target
    limits: Limits
    load: Load | None
    fingerprint: str
    source: str


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
)

LOAD_FIELDS: tuple[tuple[str, str, Dimension], ...] = (
    ("mass", "mass_kg", Dimension.MASS),
    ("length", "length_m", Dimension.LENGTH),
)

_TARGET_KEYS = ("adapter", "port", "id")
_SECTIONS = ("target", "limits", "load")


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
    load_table = _section(document, "load", problems, required=False)
    load = _read_quantities(load_table, "load", LOAD_FIELDS, problems)

    if limits is not None:
        _check_limits(limits, problems)
    if load is not None:
        _check_positive(load, "load", ("mass", "length"), problems)

    if problems or target is None or limits is None:
        raise BenchFileError(source, problems)
    return Bench(
        target=target,
        limits=Limits(**_by_attribute(limits, LIMIT_FIELDS)),
        load=None if load is None else Load(**_by_attribute(load, LOAD_FIELDS)),
        fingerprint=fingerprint(data),
        source=source,
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
) -> dict[str, float] | None:
    if table is None:
        return None
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


def _check_unknown(table: dict, section: str, known: tuple[str, ...], problems: list[str]) -> None:
    for key in table:
        if key not in known:
            problems.append(f"[{section}] unknown key {key!r}; known keys: {_names(known)}")


def _check_limits(limits: dict[str, float], problems: list[str]) -> None:
    if limits["position_min"] >= limits["position_max"]:
        problems.append("[limits] position_min must be less than position_max")
    if limits["voltage_min"] >= limits["voltage_max"]:
        problems.append("[limits] voltage_min must be less than voltage_max")
    _check_positive(limits, "limits", ("speed_max", "current_max", "experiment_max"), problems)
    if limits["cooldown"] < 0:
        problems.append("[limits] cooldown must not be negative")


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
# A bare number is an error.
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
# Longest single experiment.
experiment_max = "30 s"
# Rest between one experiment and the next.
cooldown = "60 s"

[load]
# What is attached to the output. For the bench rig, the pendulum arm:
# its total mass, and the distance from the shaft axis to its centre of mass.
# Delete this section if nothing is attached.
mass = "50 g"
length = "100 mm"
"""
