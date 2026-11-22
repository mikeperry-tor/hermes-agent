"""Unmapped live Chromium state cannot prevent selected state from syncing back."""
import tarfile
from unittest.mock import Mock

import pytest

from tools.environments.ssh import SSHEnvironment


@pytest.mark.platforms('posix')
def test_archive_excludes_live_profile_before_safe_extraction(tmp_path):
    remote = tmp_path / 'remote'
    state = remote / '.hermes'
    (state / 'skills').mkdir(parents=True)
    (state / 'skills' / 'example.md').write_text('selected state')
    profile = state / 'bot-desktop' / 'browser-profile'
    profile.mkdir(parents=True)
    (profile / 'SingletonLock').symlink_to('/outside/lock')
    (profile / 'Preferences').write_text('browser state')
    env = SSHEnvironment.__new__(SSHEnvironment)
    env._remote_home, env.host = str(remote), 'fixture'
    env._build_ssh_command = Mock(return_value=['bash', '-c'])
    archive = tmp_path / 'download.tar'
    env._ssh_bulk_download(archive)
    extracted = tmp_path / 'extracted'
    with tarfile.open(archive) as tar:
        tar.extractall(extracted, filter='data')
    assert (extracted / str(state).lstrip('/') / 'skills' / 'example.md').read_text() == 'selected state'
    assert not (extracted / str(profile).lstrip('/')).exists()
    assert (profile / 'SingletonLock').is_symlink()
    assert (profile / 'Preferences').read_text() == 'browser state'
