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


# == Added in plan step 8 =========================================================


def test_characterize_with_a_missing_limit_is_a_bench_file_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench.toml").write_text(
        TEMPLATE.replace('speed_max = "180 deg/s"\n', ""), encoding="utf-8"
    )
    assert main(["characterize", "--json"]) == 2
    output = json_of(capsys)
    assert output["command"] == "characterize" and output["verdict"] == "error"
    assert "speed_max" in output["summary"]
    assert not (tmp_path / "runs").exists()


def test_characterize_without_a_load_is_refused_before_contact(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    text = TEMPLATE.split("[load]")[0] + "[sim]\n"
    (tmp_path / "bench.toml").write_text(text, encoding="utf-8")
    assert main(["characterize", "--json"]) == 2
    output = json_of(capsys)
    assert output["verdict"] == "refused" and "[load]" in output["summary"]
    assert not (tmp_path / "runs").exists()


# == Regressions from the outside review, 2026-11-16 ==================================


def _unsafe_then(error):
    """A command that runs one experiment whose safe state is not confirmed, then raises."""
    from servobench.adapters.sim import SimServo
    from servobench.contract import NoReply
    from servobench.experiments.runner import Outcome, run_experiment
    from servobench.experiments.spec import Hold

    class Deaf(SimServo):
        def enter_safe_state(self):
            raise NoReply("no answer")

    def command(bench, **kwargs):
        result = run_experiment(Deaf(), bench, Hold(0.0, 1.0))
        assert result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED
        raise error

    return command


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("bug after the run")])
def test_a_failure_after_an_unconfirmed_safe_state_never_reads_as_safe(
    tmp_path, monkeypatch, capsys, error
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench.toml").write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(cli, "run_check", _unsafe_then(error))
    assert main(["check", "--json"]) == 4
    output = json_of(capsys)
    assert output["verdict"] == "safe_state_not_confirmed"
    assert output["needs_human"] is True and "cut its power" in output["next"]
    for claim in ("had not left the actuator enabled", "never leaves the actuator enabled"):
        assert claim not in output["next"] and claim not in output["summary"]


# == Regressions from the second outside review, 2026-11-16 ===========================
#
# The safe-state warning must reach stderr, flushed, before anything is tried
# on stdout, and no failure to write either stream may raise out of the
# command or change its exit code, the interpreter's own flush at exit included.

import io  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

UNSAFE = CommandResult(
    "check", "safe_state_not_confirmed", "SAFE STATE NOT CONFIRMED. Cut its power.",
    ExitCode.DEVICE, True, next="Tell the human now.",
)  # fmt: skip


class _Stream(io.TextIOBase):
    """Logs every write and flush to ``log`` (shared between streams), and may fail."""

    def __init__(self, name, log, broken=False):
        self.label, self.log, self.broken = name, log, broken

    def writable(self):
        return True

    def write(self, text):
        self.log.append((self.label, "write", text))
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")
        return len(text)

    def flush(self):
        self.log.append((self.label, "flush", ""))
        if self.broken:
            raise BrokenPipeError(32, "Broken pipe")

    def close(self):
        self.broken = False  # so that finalising this test stream never raises
        super().close()


def _streams(monkeypatch, *, stdout_broken=False, stderr_broken=False):
    log = []
    monkeypatch.setattr(sys, "stdout", _Stream("stdout", log, stdout_broken))
    monkeypatch.setattr(sys, "stderr", _Stream("stderr", log, stderr_broken))
    return log


def _written(log, name):
    return "".join(text for stream, kind, text in log if stream == name and kind == "write")


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("stdout_broken", [False, True], ids=["stdout-open", "stdout-broken"])
def test_the_safe_state_warning_is_on_stderr_and_flushed_before_stdout_is_tried(
    monkeypatch, as_json, stdout_broken
):
    log = _streams(monkeypatch, stdout_broken=stdout_broken)
    assert cli._emit(UNSAFE, as_json=as_json) == 4
    assert "SAFE STATE NOT CONFIRMED" in _written(log, "stderr")
    first_stdout = next(i for i, (stream, _, _) in enumerate(log) if stream == "stdout")
    before = log[:first_stdout]
    assert ("stderr", "flush", "") in before, log
    assert "SAFE STATE NOT CONFIRMED" in "".join(text for _, _, text in before)


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "text"])
@pytest.mark.parametrize(
    ("verdict", "code"),
    [
        ("pass", ExitCode.PASS),
        ("fail", ExitCode.FAIL),
        ("error", ExitCode.USAGE),
        ("refused", ExitCode.SAFETY),
        ("error", ExitCode.DEVICE),
        ("safe_state_not_confirmed", ExitCode.DEVICE),
    ],
)
@pytest.mark.parametrize(
    "broken", ["stdout", "stderr", "both"], ids=lambda which: f"{which}-broken"
)
def test_a_stream_that_cannot_be_written_never_raises_or_changes_the_exit_code(
    monkeypatch, as_json, verdict, code, broken
):
    _streams(
        monkeypatch,
        stdout_broken=broken in ("stdout", "both"),
        stderr_broken=broken in ("stderr", "both"),
    )
    result = CommandResult("check", verdict, f"{verdict}: a summary.", code, True)
    assert cli._emit(result, as_json=as_json) == int(code)


def test_check_with_an_unconfirmed_safe_state_and_a_closed_stdout_returns_4(
    tmp_path, monkeypatch
):
    from servobench.adapters.sim import SimServo
    from servobench.contract import CommunicationError

    def never_safe(self):
        raise CommunicationError("device gone")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "bench.toml").write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(SimServo, "enter_safe_state", never_safe)
    log = _streams(monkeypatch, stdout_broken=True)
    assert main(["check", "--json"]) == 4
    assert _written(log, "stderr").startswith("servobench: SAFE STATE NOT CONFIRMED")


# -- in a real process: the interpreter flushes stdout again at exit -----------------

_ENTRY = "import sys; from servobench.cli import main; sys.exit(main())"  # as the script does


def _run_with_closed_stdout(cwd, *args, close_stderr=False):
    read_end, write_end = os.pipe()
    os.close(read_end)  # nobody will ever read: every write to it fails (EPIPE)
    try:
        return subprocess.run(
            [sys.executable, "-c", _ENTRY, *args],
            cwd=cwd,
            stdout=write_end,
            stderr=write_end if close_stderr else subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=120,
        )
    finally:
        os.close(write_end)


def _bench_with_faults(tmp_path, *faults):
    text = TEMPLATE.replace('# faults = ["wrong_zero"]', f"faults = {json.dumps(list(faults))}")
    (tmp_path / "bench.toml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("mode", [["--json"], []], ids=["json", "text"])
def test_a_real_process_with_a_closed_stdout_keeps_exit_4_and_the_warning(tmp_path, mode):
    _bench_with_faults(tmp_path, "ignores_safe_state")
    done = _run_with_closed_stdout(tmp_path, "check", *mode)
    assert done.returncode == 4, done.stderr
    assert done.stderr.startswith("servobench: SAFE STATE NOT CONFIRMED"), done.stderr
    assert "Traceback" not in done.stderr and "Exception ignored" not in done.stderr


@pytest.mark.parametrize("mode", [["--json"], []], ids=["json", "text"])
def test_a_real_process_with_a_closed_stdout_keeps_exit_0(tmp_path, mode):
    _bench_with_faults(tmp_path)
    done = _run_with_closed_stdout(tmp_path, "check", "--no-motion", *mode)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""


def test_a_real_process_with_both_streams_closed_keeps_its_exit_code(tmp_path):
    _bench_with_faults(tmp_path, "ignores_safe_state")
    assert _run_with_closed_stdout(tmp_path, "check", close_stderr=True).returncode == 4
    _bench_with_faults(tmp_path)
    assert _run_with_closed_stdout(tmp_path, "init", close_stderr=True).returncode == 2
