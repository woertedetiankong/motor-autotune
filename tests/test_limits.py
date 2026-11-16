import hashlib
import math
import re

import pytest

from servobench.limits import (
    CHECK_FIELDS,
    LIMIT_FIELDS,
    LOAD_FIELDS,
    RUNNER_FIELDS,
    TEMPLATE,
    BenchFileError,
    CheckSettings,
    Limits,
    Load,
    RunnerSettings,
    SimSettings,
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
        start_temperature_max_degc=45.0,
        command_margin_rad=pytest.approx(math.radians(10.0)),
        follow_error_max_rad=pytest.approx(math.radians(5.0)),
        follow_time_max_s=1.0,
    )
    assert bench.runner == RunnerSettings(
        missed_replies_max=3,
        approach_speed_rad_s=pytest.approx(math.radians(20.0)),
        when_too_warm="wait",
        cool_wait_max_s=600.0,
    )
    assert bench.check == CheckSettings(
        move_rad=pytest.approx(math.radians(2.5)),
        zero_tolerance_rad=pytest.approx(math.radians(8.0)),
        temperature_rise_max_degc=3.0,
        sample_rate_min_hz=100.0,
    )
    assert bench.load == Load(mass_kg=pytest.approx(0.05), length_m=pytest.approx(0.1))
    assert bench.sim == SimSettings()
    assert bench.fingerprint == "sha256:" + hashlib.sha256(TEMPLATE.encode()).hexdigest()


def test_template_sets_every_field():
    keys = [key for key, _, _ in LIMIT_FIELDS + RUNNER_FIELDS + CHECK_FIELDS + LOAD_FIELDS]
    for key in keys + ["missed_replies_max", "when_too_warm"]:
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


@pytest.mark.parametrize("key", [key for key, _, _ in CHECK_FIELDS])
def test_missing_check_setting_rejected_naming_it(key):
    message = problems_for(edit(TEMPLATE, key, ""))
    assert f"[check] {key} is missing" in message


def test_check_section_is_required_and_checked_for_unknown_keys():
    start, end = TEMPLATE.index("[check]"), TEMPLATE.index("[load]")
    assert "section [check] is missing" in problems_for(TEMPLATE[:start] + TEMPLATE[end:])
    text = TEMPLATE.replace("[check]\n", '[check]\nskip_direction = true\n')
    assert "[check] unknown key 'skip_direction'" in problems_for(text)


@pytest.mark.parametrize(
    ("key", "line", "expected"),
    [
        ("move", 'move = "0 deg"', "[check] move must be greater than zero"),
        ("move", "move = 2.5", "[check] move: 2.5 is a bare number"),
        ("move", 'move = "3 deg"', "[check] move must be at most half of [limits] follow_error"),
        ("zero_tolerance", 'zero_tolerance = "0 deg"', "zero_tolerance must be greater"),
        ("zero_tolerance", 'zero_tolerance = "8 V"', "expected an angle"),
        ("temperature_rise_max", 'temperature_rise_max = "-1 degC"', "must be greater"),
        ("sample_rate_min", 'sample_rate_min = "10 ms"', "expected a frequency"),
    ],
)
def test_bad_check_settings_rejected(key, line, expected):
    assert expected in problems_for(edit(TEMPLATE, key, line))


def test_check_move_follows_follow_error_max():
    text = edit(TEMPLATE, "follow_error_max", 'follow_error_max = "8 deg"')
    assert parse_bench(edit(text, "move", 'move = "4 deg"').encode()).check.move_rad == (
        pytest.approx(math.radians(4.0))
    )


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
        (
            "start_temperature_max",
            'start_temperature_max = "60 degC"',
            "start_temperature_max must be less than temperature_max",
        ),
        ("command_margin", 'command_margin = "-1 deg"', "command_margin must not be negative"),
        ("command_margin", 'command_margin = "90 deg"', "command_margin leaves no room"),
        ("follow_error_max", 'follow_error_max = "0 deg"', "follow_error_max must be greater"),
        ("follow_time_max", 'follow_time_max = "0 s"', "follow_time_max must be greater"),
        ("approach_speed", 'approach_speed = "0 deg/s"', "approach_speed must be greater"),
        (
            "approach_speed",
            'approach_speed = "200 deg/s"',
            "approach_speed must not be above [limits] speed_max",
        ),
        ("cool_wait_max", 'cool_wait_max = "-1 s"', "cool_wait_max must not be negative"),
    ],
)
def test_inconsistent_values_rejected(key, line, expected):
    assert expected in problems_for(edit(TEMPLATE, key, line))


@pytest.mark.parametrize("key", [key for key, _, _ in RUNNER_FIELDS])
def test_missing_runner_quantity_rejected_naming_it(key):
    assert f"[runner] {key} is missing" in problems_for(edit(TEMPLATE, key, ""))


@pytest.mark.parametrize(
    ("key", "line", "expected"),
    [
        ("missed_replies_max", "", "[runner] missed_replies_max is missing"),
        ("missed_replies_max", 'missed_replies_max = "3"', "must be a whole number of replies"),
        ("missed_replies_max", "missed_replies_max = -1", "must be a whole number of replies"),
        ("missed_replies_max", "missed_replies_max = 2.5", "must be a whole number of replies"),
        ("missed_replies_max", "missed_replies_max = true", "must be a whole number of replies"),
        ("when_too_warm", "", "[runner] when_too_warm is missing"),
        ("when_too_warm", 'when_too_warm = "ignore"', 'must be "wait" or "refuse"'),
        ("approach_speed", "approach_speed = 20", "approach_speed: 20 is a bare number"),
        ("cool_wait_max", 'cool_wait_max = "10 deg"', "expected a time"),
    ],
)
def test_bad_runner_settings_rejected(key, line, expected):
    assert expected in problems_for(edit(TEMPLATE, key, line))


def test_runner_section_is_required_and_checked_for_unknown_keys():
    start, end = TEMPLATE.index("[runner]"), TEMPLATE.index("[load]")
    assert "section [runner] is missing" in problems_for(TEMPLATE[:start] + TEMPLATE[end:])
    text = TEMPLATE.replace("[runner]\n", '[runner]\nskip_rehearsal = true\n')
    assert "[runner] unknown key 'skip_rehearsal'" in problems_for(text)


def test_when_too_warm_refuse_is_accepted():
    bench = parse_bench(edit(TEMPLATE, "when_too_warm", 'when_too_warm = "refuse"').encode())
    assert bench.runner.when_too_warm == "refuse"


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


# -- the [sim] section ---------------------------------------------------------


def with_sim(*lines: str) -> str:
    """The template with its [sim] section's settings replaced by ``lines``."""
    return TEMPLATE[: TEMPLATE.index("[sim]")] + "[sim]\n" + "\n".join(lines) + "\n"


def test_template_sim_section_is_all_defaults():
    assert parse_bench(TEMPLATE.encode()).sim == SimSettings()


def test_sim_settings_are_read_with_units():
    text = with_sim(
        'faults = ["wrong_zero", "dropped_replies"]',
        "seed = 3",
        "noise = false",
        'sample_rate = "0.5 kHz"',
        'jitter = "200 us"',
        'supply_voltage = "7400 mV"',
        'ambient_temperature = "30 degC"',
        'initial_temperature = "45 degC"',
    )
    assert parse_bench(text.encode()).sim == SimSettings(
        faults=("wrong_zero", "dropped_replies"),
        seed=3,
        noise=False,
        sample_rate_hz=500.0,
        jitter_s=pytest.approx(200e-6),
        supply_voltage_v=pytest.approx(7.4),
        ambient_temperature_degc=30.0,
        initial_temperature_degc=45.0,
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("sample_rate = 200", "[sim] sample_rate: 200 is a bare number"),
        ('sample_rate = "200 ms"', "expected a frequency"),
        ('sample_rate = "0 Hz"', "[sim] sample_rate must be greater than zero"),
        ('jitter = "-1 ms"', "[sim] jitter must not be negative"),
        ('supply_voltage = "0 V"', "[sim] supply_voltage must be greater than zero"),
        ("seed = -1", "[sim] seed must be a whole number"),
        ("seed = 1.5", "[sim] seed must be a whole number"),
        ('noise = "yes"', "[sim] noise must be true or false"),
        ('faults = "wrong_zero"', "[sim] faults must be a list"),
        ('faults = ["wrong_zero", "wrong_zero"]', "more than once"),
        (
            'faults = ["gremlins"]',
            "[sim] unknown fault 'gremlins'; known faults: reversed_direction",
        ),
        ('fault = ["wrong_zero"]', "[sim] unknown key 'fault'"),
    ],
)
def test_bad_sim_settings_rejected(line, expected):
    assert expected in problems_for(with_sim(line))


def test_sim_settings_refused_for_a_real_adapter():
    real = edit(TEMPLATE, "adapter", 'adapter = "hd1910"')
    assert parse_bench(real.encode()).sim == SimSettings()  # an empty [sim] is fine
    text = edit(with_sim('faults = ["runaway"]'), "adapter", 'adapter = "hd1910"')
    assert "[sim] settings apply only to the simulated servo" in problems_for(text)
