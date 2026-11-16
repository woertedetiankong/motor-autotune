import math

import pytest

from servobench.units import (
    _UNITS,
    Dimension,
    Quantity,
    UnitError,
    format_quantity,
    parse,
    parse_quantity,
    units_for,
)


@pytest.mark.parametrize(
    ("text", "dimension", "expected_si"),
    [
        ("45 deg", Dimension.ANGLE, math.pi / 4),
        ("0.5 rad", Dimension.ANGLE, 0.5),
        ("2 Hz", Dimension.FREQUENCY, 2.0),
        ("300 ms", Dimension.TIME, 0.3),
        ("6 V", Dimension.VOLTAGE, 6.0),
        ("0.8 A", Dimension.CURRENT, 0.8),
        ("60 degC", Dimension.TEMPERATURE, 60.0),
    ],
)
def test_plan_examples_parse_to_si(text, dimension, expected_si):
    assert parse(text, dimension) == pytest.approx(expected_si)
    assert parse_quantity(text) == Quantity(pytest.approx(expected_si), dimension)


def test_degrees_and_radians_both_accepted_for_an_angle():
    assert parse("180 deg", Dimension.ANGLE) == pytest.approx(math.pi)
    assert parse("-90 deg", Dimension.ANGLE) == pytest.approx(-math.pi / 2)
    assert parse("90 °", Dimension.ANGLE) == pytest.approx(math.pi / 2)
    assert parse("3.14159 rad", Dimension.ANGLE) == pytest.approx(3.14159)


def test_angular_velocity_units():
    assert parse("180 deg/s", Dimension.ANGULAR_VELOCITY) == pytest.approx(math.pi)
    assert parse("60 rpm", Dimension.ANGULAR_VELOCITY) == pytest.approx(2 * math.pi)


@pytest.mark.parametrize("text", ["45deg", "  45   deg  ", "+45 deg", "4.5e1 deg", ".45e2 deg"])
def test_spacing_sign_and_exponent(text):
    assert parse(text, Dimension.ANGLE) == pytest.approx(math.pi / 4)


@pytest.mark.parametrize("text", ["0.5 N m", "0.5 Nm", "0.5 N*m", "0.5 N·m", "500 mN m"])
def test_compound_unit_spellings(text):
    assert parse(text, Dimension.TORQUE) == pytest.approx(0.5)


@pytest.mark.parametrize("bare", [45, 0.5, "45", "  0.5 "])
def test_bare_number_rejected(bare):
    with pytest.raises(UnitError, match="bare number"):
        parse(bare, Dimension.ANGLE, field="position_max")
    with pytest.raises(UnitError, match="bare number"):
        parse_quantity(bare)


def test_bare_number_message_does_not_guess_a_unit():
    # Suggesting "90 rad" for a bare 90 would invite the very mistake units exist to stop.
    with pytest.raises(UnitError) as error:
        parse(90, Dimension.ANGLE, field="position_max")
    message = str(error.value)
    assert "position_max" in message
    assert "90 rad" not in message and "90 deg" not in message


@pytest.mark.parametrize("value", [True, None, ["45 deg"]])
def test_non_string_rejected(value):
    with pytest.raises(UnitError):
        parse(value, Dimension.ANGLE)


def test_voltage_where_angle_expected_rejected():
    with pytest.raises(UnitError) as error:
        parse("6 V", Dimension.ANGLE, field="position_max")
    message = str(error.value)
    assert "position_max" in message
    assert "is a voltage" in message and "expected an angle" in message


def test_frequency_is_not_an_angular_velocity():
    with pytest.raises(UnitError, match="a frequency"):
        parse("2 Hz", Dimension.ANGULAR_VELOCITY)


@pytest.mark.parametrize("text", ["45 degrees", "6 v", "800 MA", "300 K", "45 deg deg"])
def test_unknown_unit_rejected(text):
    with pytest.raises(UnitError, match="unknown unit"):
        parse(text, Dimension.ANGLE)


@pytest.mark.parametrize("text", ["", "deg", "abc", "nan deg", "inf deg", "1,5 deg", "--1 deg"])
def test_not_a_quantity_rejected(text):
    with pytest.raises(UnitError):
        parse(text, Dimension.ANGLE)


def test_error_lists_the_expected_units():
    with pytest.raises(UnitError) as error:
        parse("45 degrees", Dimension.ANGLE)
    assert "deg" in str(error.value) and "rad" in str(error.value)


def test_every_unit_round_trips_through_format_at_full_precision():
    for spellings, dimension, _ in _UNITS:
        for unit in spellings:
            text = format_quantity(1.25, dimension, unit, digits=17)
            assert parse(text, dimension) == pytest.approx(1.25, rel=1e-15), text


def test_format_quantity():
    assert format_quantity(math.pi / 4, Dimension.ANGLE, "deg") == "45 deg"
    assert format_quantity(1.25, Dimension.ANGULAR_VELOCITY, "rpm") == "11.9366 rpm"
    assert format_quantity(1.25, Dimension.ANGULAR_VELOCITY, "rpm", digits=3) == "11.9 rpm"
    assert format_quantity(0.3, Dimension.TIME) == "0.3 s"
    assert str(Quantity(0.3, Dimension.TIME)) == "0.3 s"
    with pytest.raises(UnitError):
        format_quantity(1.0, Dimension.ANGLE, "V")


def test_every_dimension_has_units_and_a_valid_example():
    for dimension in Dimension:
        assert dimension.si_unit in units_for(dimension)
        parse(dimension.example, dimension)
