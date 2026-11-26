"""Failed sandbox inspection stays distinct from missing packages, including RPC status."""
import subprocess
from unittest.mock import patch
from types import SimpleNamespace
import pytest

from tools.bot_desktop import placement, runtime, sandbox_host
from tools.environments import streams
from tui_gateway import methods_display

def test_dependency_probe_status_and_recovery():
    executor = SimpleNamespace(_bd_desktop_user=None)
    where = placement.Placement(placement.TERMINAL, 'ssh')
    for output, expected in [(b'', []), (b'Xvnc\nxauth\n', ['Xvnc', 'xauth'])]:
        with patch.object(streams, 'run_in', return_value=subprocess.CompletedProcess([], 0, output, b'')):
            assert sandbox_host.missing_binaries(executor) == expected
    with patch.object(streams, 'run_in', return_value=subprocess.CompletedProcess([], 255, b'', b'private SSH details')):
        try:
            sandbox_host.missing_binaries(executor)
        except RuntimeError as error:
            assert 'inspect desktop dependencies' in str(error)
            assert 'private SSH details' not in str(error)
        else:
            raise AssertionError('failed probe became an empty missing-command list')

    with patch.object(placement, 'resolve', return_value=where), \
            patch.object(runtime, '_owned_sandbox_env', return_value=None), \
            patch.object(runtime, '_sandbox_env', return_value=executor), \
            patch.object(sandbox_host, 'published_env', return_value={}):
        for missing in ([], ['Xvnc']):
            with patch.object(sandbox_host, 'missing_binaries', return_value=missing):
                status = runtime.status()
                assert status.missing == missing
                assert status.installed == (not missing)
                assert not status.running
                assert bool(status.blocker) == bool(missing)
        with patch.object(runtime, '_sandbox_env', return_value=None):
            status = runtime.status()
            assert not status.installed and not status.running and status.blocker
            assert methods_display._display_snapshot()['blocker'] == status.blocker
        for failure in (RuntimeError('private SSH details'), OSError('private SSH details'),
                        subprocess.TimeoutExpired('private command', 20)):
            with patch.object(sandbox_host, 'missing_binaries', side_effect=failure):
                status = runtime.status()
                assert not status.installed and not status.running
                assert status.missing == []  # Unknown dependencies, not a missing-package verdict.
                assert status.blocker and 'connection' in status.blocker
                assert 'private' not in status.blocker
                snapshot = methods_display._display_snapshot()
                assert not snapshot['installed'] and snapshot['blocker'] == status.blocker
        with patch.object(sandbox_host, 'missing_binaries', return_value=[]), \
                patch.object(sandbox_host, 'published_env', return_value={'DISPLAY': ':20'}):
            assert runtime.status().running  # A later successful probe recovers without a marker.
        with patch.object(sandbox_host, 'missing_binaries', side_effect=RuntimeError('private SSH details')), \
                patch.object(sandbox_host, 'published_env', return_value={'DISPLAY': ':20'}):
            status = runtime.status()
            assert not status.installed and status.blocker and status.running
            assert status.display == ':20'
        with patch.object(sandbox_host, 'missing_binaries', return_value=['Xvnc']), \
                patch.object(sandbox_host, 'published_env', side_effect=OSError('private SSH details')):
            status = runtime.status()
            assert not status.installed and not status.running and status.missing == ['Xvnc']
            assert 'Xvnc' in status.blocker and 'private SSH details' not in status.blocker
        with patch.object(sandbox_host, 'missing_binaries', return_value=[]), \
                patch.object(sandbox_host, 'published_env', side_effect=OSError('private SSH details')):
            status = runtime.status()
            assert status.installed and not status.running and not status.missing
            assert 'inspect the desktop display' in status.blocker


def test_display_subprocess_distinguishes_stopped_screen_from_transport_failure():
    env = SimpleNamespace(_bd_desktop_user=None)
    for code, output, expected in [(3, b'', {}), (0, b'DISPLAY=:20\n', {'DISPLAY': ':20'})]:
        with patch.object(streams, 'run_in', return_value=subprocess.CompletedProcess([], code, output, b'')):
            assert sandbox_host._published(env, '/fixture', strict=True) == expected
    failed = subprocess.CompletedProcess([], 255, b'', b'private SSH details')
    with patch.object(streams, 'run_in', return_value=failed):
        with pytest.raises(RuntimeError, match='Could not inspect the desktop display'):
            sandbox_host._published(env, '/fixture', strict=True)
        assert sandbox_host._published(env, '/fixture') == {}
