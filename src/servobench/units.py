"""Parse and format quantities. Inside the library every value is an SI float.

A quantity enters the tool as a string with a unit, for example ``"45 deg"``,
and is converted once, at the boundary, to a float in the SI unit of its
dimension. From then on the variable name carries the unit, for example
``position_rad`` or ``timeout_s``. A bare number is never accepted: in the
workshop that motivated this project, degrees sent where radians were expected
turned a robot about 60 times too far.

Temperatures are kept in degrees Celsius rather than kelvin, because every
device and every limit states them that way and Celsius is an SI derived unit.

To support a new unit, add one line to ``_UNITS``. To support a new dimension,
add a member to ``Dimension`` and its units to ``_UNITS``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum


class UnitError(ValueError):
    """A value has no unit, an unknown unit, or a unit of the wrong dimension."""


class Dimension(Enum):
    """What a quantity measures, with the unit it is stored in inside the library."""

    # name = (description, unit stored inside the library, example for messages)
    ANGLE = ("an angle", "rad", "45 deg")
    ANGULAR_VELOCITY = ("an angular velocity", "rad/s", "90 deg/s")
    FREQUENCY = ("a frequency", "Hz", "2 Hz")
    TIME = ("a time", "s", "300 ms")
    VOLTAGE = ("a voltage", "V", "6 V")
    CURRENT = ("a current", "A", "0.8 A")
    TEMPERATURE = ("a temperature", "degC", "60 degC")
    MASS = ("a mass", "kg", "50 g")
    LENGTH = ("a length", "m", "100 mm")
    TORQUE = ("a torque", "N m", "0.5 N m")

    def __init__(self, description: str, si_unit: str, example: str) -> None:
        self.description = description
        self.si_unit = si_unit
        self.example = example


_D = Dimension

# Each entry: accepted spellings, dimension, size of one unit in the SI unit.
# The first spelling is the one shown in messages. Spellings are case-sensitive
# (mA is not MA); spaces, "*" and "·" inside a unit are ignored, so "N m",
# "N*m" and "N·m" are the same unit.
_UNITS: list[tuple[tuple[str, ...], Dimension, float]] = [
    (("rad",), _D.ANGLE, 1.0),
    (("mrad",), _D.ANGLE, 1e-3),
    (("deg", "°"), _D.ANGLE, math.pi / 180.0),
    (("rad/s",), _D.ANGULAR_VELOCITY, 1.0),
    (("deg/s", "°/s"), _D.ANGULAR_VELOCITY, math.pi / 180.0),
    (("rpm",), _D.ANGULAR_VELOCITY, 2.0 * math.pi / 60.0),
    (("Hz",), _D.FREQUENCY, 1.0),
    (("kHz",), _D.FREQUENCY, 1e3),
    (("s",), _D.TIME, 1.0),
    (("ms",), _D.TIME, 1e-3),
    (("us", "µs", "μs"), _D.TIME, 1e-6),
    (("min",), _D.TIME, 60.0),
    (("V",), _D.VOLTAGE, 1.0),
    (("mV",), _D.VOLTAGE, 1e-3),
    (("A",), _D.CURRENT, 1.0),
    (("mA",), _D.CURRENT, 1e-3),
    (("degC", "°C"), _D.TEMPERATURE, 1.0),
    (("kg",), _D.MASS, 1.0),
    (("g",), _D.MASS, 1e-3),
    (("m",), _D.LENGTH, 1.0),
    (("cm",), _D.LENGTH, 1e-2),
    (("mm",), _D.LENGTH, 1e-3),
    (("N m", "Nm"), _D.TORQUE, 1.0),
    (("mN m", "mNm"), _D.TORQUE, 1e-3),
]


def _normalise(unit: str) -> str:
    return re.sub(r"[\s*·]", "", unit)


_LOOKUP: dict[str, tuple[Dimension, float]] = {
    _normalise(spelling): (dimension, scale)
    for spellings, dimension, scale in _UNITS
    for spelling in spellings
}

_QUANTITY = re.compile(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*")


@dataclass(frozen=True)
class Quantity:
    """A value in the SI unit of its dimension (see ``Dimension.si_unit``)."""

    value: float
    dimension: Dimension

    def __str__(self) -> str:
        return format_quantity(self.value, self.dimension)


def units_for(dimension: Dimension) -> list[str]:
    """The units accepted for a dimension, one spelling each."""
    return [spellings[0] for spellings, dim, _ in _UNITS if dim is dimension]


def parse_quantity(text: object, *, field: str | None = None) -> Quantity:
    """Parse a string such as ``"45 deg"`` into an SI value tagged with its dimension.

    ``field`` names the setting being parsed, so that the error says where the
    bad value came from.
    """
    return _parse(text, field, expected=None)


def parse(text: object, expected: Dimension, *, field: str | None = None) -> float:
    """Parse a string with a unit and return its value in the SI unit of ``expected``.

    Rejects a bare number, an unknown unit, and a unit of another dimension.
    """
    quantity = _parse(text, field, expected)
    if quantity.dimension is not expected:
        raise UnitError(
            f'{_prefix(field)}"{text}" is {quantity.dimension.description}; {_accepted(expected)}'
        )
    return quantity.value


def format_quantity(
    value: float, dimension: Dimension, unit: str | None = None, *, digits: int = 6
) -> str:
    """Format an SI value in ``unit`` (default: the SI unit), to ``digits`` significant figures.

    The default suits people. Records should keep SI floats, not formatted text;
    if text must parse back exactly, pass ``digits=17``.
    """
    unit = unit or dimension.si_unit
    known = _LOOKUP.get(_normalise(unit))
    if known is None or known[0] is not dimension:
        raise UnitError(f'cannot show {dimension.description} in "{unit}"; {_accepted(dimension)}')
    return f"{value / known[1]:.{digits}g} {unit}"


def _parse(text: object, field: str | None, expected: Dimension | None) -> Quantity:
    # Messages never attach a unit to the caller's own number: guessing "90 rad"
    # for a bare 90 is exactly the mistake this module exists to prevent.
    accepted = _accepted(expected)
    if isinstance(text, bool) or not isinstance(text, str):
        what = "a bare number with no unit" if isinstance(text, (int, float)) else "not a string"
        raise UnitError(f"{_prefix(field)}{text!r} is {what}; {accepted}")
    match = _QUANTITY.fullmatch(text)
    if match is None:
        raise UnitError(f'{_prefix(field)}"{text}" is not a number and a unit; {accepted}')
    number, unit = match.groups()
    if not unit:
        raise UnitError(f'{_prefix(field)}"{text}" is a bare number with no unit; {accepted}')
    known = _LOOKUP.get(_normalise(unit))
    if known is None:
        raise UnitError(f'{_prefix(field)}unknown unit "{unit}" in "{text}"; {accepted}')
    dimension, scale = known
    return Quantity(float(number) * scale, dimension)


def _prefix(field: str | None) -> str:
    return f"{field}: " if field else ""


def _accepted(dimension: Dimension | None) -> str:
    if dimension is None:
        units = ", ".join(spellings[0] for spellings, _, _ in _UNITS)
        return f'for example "45 deg" or "300 ms" (known units: {units})'
    units = ", ".join(units_for(dimension))
    return f'expected {dimension.description}, for example "{dimension.example}" (units: {units})'
