import json

import pytest

from servobench import __version__, cli
from servobench.cli import main
from servobench.commands import CommandResult, ExitCode
from servobench.limits import TEMPLATE, load_bench


def test_version_is_set():
    assert __version__


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_is_a_usage_error():
    assert main([]) == 2


def test_init_writes_a_loadable_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / "bench.toml").read_text(encoding="utf-8") == TEMPLATE
    assert load_bench(tmp_path / "bench.toml").target.adapter == "sim"


def test_init_twice_leaves_the_first_file_untouched(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    path = tmp_path / "bench.toml"
    path.write_text(TEMPLATE + "# the owner's edit\n", encoding="utf-8")
    before = path.read_bytes()

    assert main(["init"]) == 2
    assert path.read_bytes() == before
    assert "already exists" in capsys.readouterr().err


# == Added in plan step 7 ==========================================================

def json_of(capsys):
    return json.loads(capsys.readouterr().out)


def test_bad_arguments_with_json_still_give_one_json_object(capsys):
    assert main(["check", "--json", "--fast"]) == 2
    output = json_of(capsys)
    assert output["verdict"] == "error" and "unrecognized arguments: --fast" in output["summary"]
    assert output["exit_code"] == 2 and output["needs_human"] is False


def test_bad_arguments_in_text_mode_go_to_stderr(capsys):
    assert main(["runs", "--limit", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "expected 1 or more" in captured.err


def test_json_may_come_before_or_after_the_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--json", "init"]) == 0
    assert json_of(capsys)["verdict"] == "pass"
    assert main(["init", "--json"]) == 2
    assert json_of(capsys)["summary"].startswith("Error: bench.toml already exists")


def test_a_missing_bench_file_says_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--json"]) == 2
    output = json_of(capsys)
    assert "not found; run the init command here" in output["summary"]
    assert output["needs_human"] is True


def test_an_unknown_adapter_lists_the_known_ones(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench.toml").write_text(
        TEMPLATE.replace('adapter = "sim"', 'adapter = "hd1910"'), encoding="utf-8"
    )
    assert main(["check", "--json"]) == 2
    output = json_of(capsys)
    assert "adapter 'hd1910' is not a known adapter; known adapters: sim" in output["summary"]
    assert not (tmp_path / "runs").exists()


def test_an_unexpected_exception_is_json_with_its_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench.toml").write_text(TEMPLATE, encoding="utf-8")

    def broken(*args, **kwargs):
        raise KeyError("oops")

    monkeypatch.setattr(cli, "run_check", broken)
    assert main(["check", "--json"]) == 4
    output = json_of(capsys)
    assert output["verdict"] == "error" and output["needs_human"] is True
    assert output["error"]["type"] == "KeyError"
    assert "Traceback (most recent call last)" in output["error"]["traceback"]
    # In text mode the verdict comes first and the traceback is shown, not hidden.
    assert main(["check"]) == 4
    out = capsys.readouterr().out
    assert out.startswith("verdict: error (exit 4)")
    assert "KeyError: 'oops'" in out


def test_ctrl_c_outside_an_experiment_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench.toml").write_text(TEMPLATE, encoding="utf-8")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_check", interrupted)
    assert main(["check", "--json"]) == 3
    assert json_of(capsys)["verdict"] == "aborted"


def test_a_result_that_cannot_be_printed_still_gives_json(capsys):
    bad = CommandResult("check", "pass", "x", ExitCode.PASS, False, metrics={"v": float("nan")})
    assert cli._emit(bad, as_json=True) == 4
    output = json_of(capsys)
    assert output["verdict"] == "error" and "while printing" in output["summary"]


def test_safe_state_not_confirmed_is_also_written_to_stderr(capsys):
    result = CommandResult(
        "check", "safe_state_not_confirmed", "SAFE STATE NOT CONFIRMED. Cut its power.",
        ExitCode.DEVICE, True,
    )  # fmt: skip
    assert cli._emit(result, as_json=True) == 4
    captured = capsys.readouterr()
    assert json.loads(captured.out)["verdict"] == "safe_state_not_confirmed"
    assert "SAFE STATE NOT CONFIRMED" in captured.err
    cli._emit(result, as_json=False)
    assert capsys.readouterr().out.startswith("verdict: SAFE STATE NOT CONFIRMED (exit 4)")


def test_runs_lists_and_shows_the_records(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["runs", "--json"]) == 0
    assert json_of(capsys)["metrics"]["total"] == 0
    assert main(["runs", "show", "--json"]) == 2
    assert "no run records" in json_of(capsys)["summary"]

    (tmp_path / "bench.toml").write_text(TEMPLATE, encoding="utf-8")
    assert main(["check", "--no-motion", "--json"]) == 0
    first = json_of(capsys)["run_dir"]
    assert main(["check", "--json"]) == 0
    second = json_of(capsys)["run_dir"]

    assert main(["runs", "--json"]) == 0
    listed = json_of(capsys)["metrics"]["runs"]
    assert [run["run_dir"] for run in listed] == [second, first]
    assert [run["verdict"] for run in listed] == ["pass", "partial"]
    assert main(["runs", "--limit", "1", "--json"]) == 0
    assert len(json_of(capsys)["metrics"]["runs"]) == 1

    assert main(["runs", "show", "--json"]) == 0
    shown = json_of(capsys)
    assert shown["verdict"] == "pass" and shown["run_dir"] == second
    assert shown["metrics"]["run"]["metrics"]["items"]["direction"]["status"] == "pass"
    assert main(["runs", "--json", "show", first.split("/")[-1]]) == 0
    shown = json_of(capsys)
    assert shown["metrics"]["run"]["verdict"] == "partial"
    assert shown["summary"].startswith(f"Run {first.split('/')[-1]} (check): verdict partial.")
    assert main(["runs", "show", "nope", "--json"]) == 2

    assert main(["runs"]) == 0
    assert "partial" in capsys.readouterr().out
