"""Inspect real shell paths, including a missing leaf accepted by GNU realpath."""
import subprocess

import pytest

from hermes_cli.ssh_workspace_fs import SshWorkspaceFs, SshWorkspaceFsError


class ShellEnvironment:
    timeout = 30

    def __init__(self, root):
        self.cwd = self._remote_home = str(root)

    def execute(self, command, **kwargs):
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True, check=False)
        return {'returncode': result.returncode, 'output': result.stdout}


@pytest.mark.platforms('posix')
def test_missing_regular_directory_and_symlink_metadata(tmp_path):
    regular = tmp_path / 'file.txt'
    regular.write_bytes(b'hello')
    (tmp_path / 'link').symlink_to(regular)
    (tmp_path / 'broken').symlink_to(tmp_path / 'absent')
    fs = SshWorkspaceFs(ShellEnvironment(tmp_path))
    for name in ('file.txt', 'link'):
        assert fs.inspect_file(name) == (str(regular), 5)
    for name, code in [('absent', 'ENOENT'), ('.', 'ENOTREG')]:
        with pytest.raises(SshWorkspaceFsError) as error:
            fs.inspect_file(name)
        assert error.value.code == code
    with pytest.raises(SshWorkspaceFsError) as error:
        fs.inspect_file('broken')
    # GNU realpath accepts a missing leaf; BSD realpath rejects it.
    assert error.value.code in {'ENOENT', 'ENOTREG'}
