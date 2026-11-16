import pytest

from servobench import __version__
from servobench.cli import main


def test_version_is_set():
    assert __version__


def test_version_flag_prints_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out
