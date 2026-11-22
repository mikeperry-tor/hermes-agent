"""Literal cwd survives native shell preparation."""
import subprocess
import pytest
from hermes_cli import web_host_terminal as terminal


@pytest.mark.platforms('posix')
def test_native_shell_launch_preserves_literal_cwd(tmp_path):
    directory = tmp_path / "space ü ' $(touch NEVER); trailing "
    directory.mkdir()
    actual = terminal.safe_cwd(str(directory))
    result = subprocess.run(['bash', '-c', 'pwd -P'], cwd=actual, capture_output=True, text=True, check=True)
    assert result.stdout.rstrip('\n') == str(directory)
    assert not (directory / 'NEVER').exists()
