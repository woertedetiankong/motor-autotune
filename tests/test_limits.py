import hashlib
import math
import re

import pytest

from servobench.limits import (
    LIMIT_FIELDS,
    LOAD_FIELDS,
    TEMPLATE,
    BenchFileError,
    Limits,
    Load,
    Target,
    load_bench,
    parse_bench,
    write_template,
)


def edit(text: str, key: str, new_line: str) -> str:
    """Replace the line that sets ``key``; fail loudly if there is not exactly one."""
    edited, count = re.subn(rf"^{key} = .*$", new_line, text, flags=re.MULTILINE)
    assert count == 1, f"{key} not found exactly once"
    return edited


def problems_for(text: str) -> str:
    with pytest.raises(BenchFileError) as error:
        parse_bench(text.encode())
    return str(error.value)


def test_template_loads_with_si_values():
    bench = parse_bench(TEMPLATE.encode())
    assert bench.target == Target(adapter="sim")
    assert bench.limits == Limits(
        position_min_rad=pytest.approx(-math.pi / 2),
        position_max_rad=pytest.approx(math.pi / 2),
        speed_max_rad_s=pytest.approx(math.pi),
        current_max_a=0.5,
        temperature_max_degc=60.0,
        voltage_min_v=6.0,
        voltage_max_v=8.4,
        experiment_max_s=30.0,
        cooldown_s=60.0,
    )
    assert bench.load == Load(mass_kg=pytest.approx(0.05), length_m=pytest.approx(0.1))
    assert bench.fingerprint == "sha256:" + hashlib.sha256(TEMPLATE.encode()).hexdigest()


def test_template_sets_every_field():
    for key, _, _ in LIMIT_FIELDS + LOAD_FIELDS:
        assert re.search(rf"^{key} = ", TEMPLATE, flags=re.MULTILINE), key


def test_degrees_and_radians_both_accepted_in_bench_file():
    in_degrees = parse_bench(edit(TEMPLATE, "position_max", 'position_max = "90 deg"').encode())
    in_radians = parse_bench(
        edit(TEMPLATE, "position_max", f'position_max = "{math.pi / 2} rad"').encode()
    )
    assert in_degrees.limits.position_max_rad == pytest.approx(in_radians.limits.position_max_rad)


def test_bare_number_rejected_naming_the_field():
    message = problems_for(edit(TEMPLATE, "position_max", "position_max = 90"))
    assert "[limits] position_max" in message and "bare number" in message


def test_voltage_where_angle_expected_rejected_naming_the_field():
    message = problems_for(edit(TEMPLATE, "position_max", 'position_max = "6 V"'))
    assert "[limits] position_max" in message
    assert "is a voltage" in message and "expected an angle" in message


@pytest.mark.parametrize("key", [key for key, _, _ in LIMIT_FIELDS])
def test_missing_limit_rejected_naming_it(key):
    message = problems_for(edit(TEMPLATE, key, ""))
    assert f"[limits] {key} is missing" in message


@pytest.mark.parametrize("key", [key for key, _, _ in LOAD_FIELDS])
def test_missing_load_field_rejected_naming_it(key):
    message = problems_for(edit(TEMPLATE, key, ""))
    assert f"[load] {key} is missing" in message


def test_missing_sections_rejected():
    assert "section [limits] is missing" in problems_for('[target]\nadapter = "sim"\n')
    assert "section [target] is missing" in problems_for(TEMPLATE.replace("[target]", "[other]"))


def test_load_section_is_optional():
    without_load = TEMPLATE[: TEMPLATE.index("[load]")]
    assert parse_bench(without_load.encode()).load is None


def test_every_problem_is_reported_at_once():
    text = edit(TEMPLATE, "speed_max", "")
    text = edit(text, "cooldown", "cooldown = 60")
    message = problems_for(text)
    assert "speed_max is missing" in message and "cooldown" in message


def test_unknown_key_rejected():
    text = edit(TEMPLATE, "temperature_max", 'temprature_max = "60 degC"')
    message = problems_for(text)
    assert "unknown key 'temprature_max'" in message
    assert "temperature_max is missing" in message


def test_unknown_section_rejected():
    assert "unknown section [limit]" in problems_for(TEMPLATE + '\n[limit]\nx = "1 s"\n')


@pytest.mark.parametrize(
    ("key", "line", "expected"),
    [
        ("position_min", 'position_min = "90 deg"', "position_min must be less than position_max"),
        ("voltage_min", 'voltage_min = "9 V"', "voltage_min must be less than voltage_max"),
        ("speed_max", 'speed_max = "0 deg/s"', "speed_max must be greater than zero"),
        ("current_max", 'current_max = "-1 A"', "current_max must be greater than zero"),
        ("experiment_max", 'experiment_max = "0 s"', "experiment_max must be greater than zero"),
        ("cooldown", 'cooldown = "-1 s"', "cooldown must not be negative"),
        ("mass", 'mass = "0 g"', "[load] mass must be greater than zero"),
    ],
)
def test_inconsistent_values_rejected(key, line, expected):
    assert expected in problems_for(edit(TEMPLATE, key, line))


@pytest.mark.parametrize(
    ("key", "line", "expected"),
    [
        ("adapter", "", "[target] adapter is missing"),
        ("adapter", "adapter = 3", "[target] adapter must be"),
        ("# port", "port = 5", "[target] port must be a string"),
        ("# id", "id = -1", "[target] id must be a whole number"),
        ("# id", 'id = "1"', "[target] id must be a whole number"),
    ],
)
def test_bad_target_rejected(key, line, expected):
    assert expected in problems_for(edit(TEMPLATE, key, line))


def test_target_with_port_and_id():
    text = edit(TEMPLATE, "# port", 'port = "/dev/ttyUSB0"')
    text = edit(text, "# id", "id = 7")
    assert parse_bench(text.encode()).target == Target(adapter="sim", port="/dev/ttyUSB0", id=7)


def test_invalid_toml_rejected():
    assert "not valid TOML" in problems_for("[limits\n")


@pytest.mark.parametrize("key", [key for key, _, _ in LIMIT_FIELDS])
def test_changing_any_limit_changes_the_fingerprint(key):
    original = parse_bench(TEMPLATE.encode())
    line = re.search(rf"^{key} = .*$", TEMPLATE, flags=re.MULTILINE).group(0)
    # Add a decimal digit: "6 V" becomes "6.1 V" and "8.4 V" becomes "8.41 V".
    # The change is small, keeps the unit, and leaves the file valid.
    changed_line = re.sub(r'"(-?\d+)(\.\d+)? ', lambda m: f'"{m[1]}{m[2] or "."}1 ', line, count=1)
    assert changed_line != line
    changed = parse_bench(edit(TEMPLATE, key, changed_line).encode())
    assert changed.limits != original.limits
    assert changed.fingerprint != original.fingerprint


def test_same_bytes_give_the_same_fingerprint():
    assert parse_bench(TEMPLATE.encode()).fingerprint == parse_bench(TEMPLATE.encode()).fingerprint


def test_load_bench_reads_the_file(tmp_path):
    path = tmp_path / "bench.toml"
    path.write_text(TEMPLATE, encoding="utf-8")
    bench = load_bench(path)
    assert bench.source == str(path)
    assert bench.limits.cooldown_s == 60.0


def test_load_bench_missing_file_says_what_to_do(tmp_path):
    with pytest.raises(BenchFileError, match="not found; run the init command"):
        load_bench(tmp_path / "bench.toml")


def test_write_template_refuses_to_overwrite(tmp_path):
    path = tmp_path / "bench.toml"
    write_template(path)
    path.write_text("# mine\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_template(path)
    assert path.read_text(encoding="utf-8") == "# mine\n"
