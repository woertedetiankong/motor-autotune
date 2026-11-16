import pytest

from servobench import __version__
from servobench.cli import main
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
