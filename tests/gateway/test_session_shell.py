"""Ink ``!cmd`` runs for its own local session, in that session's directory (dokterdok N22)."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle


@pytest.mark.platforms("posix")
def test_shell_exec_runs_in_the_session_cwd_behind_the_safety_gates(tmp_path):
    """The owner serves ``shell.exec`` with the ``session_id`` Ink sends: the command runs in the
    session's frozen launch cwd (not the daemon's), its exit code and output come back, the
    owner's credentials are scrubbed from the child, and a dangerous or sessionless command is
    refused."""
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir()
    user.mkdir()
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home),
               PYTHONPATH=str(root), PYTHONUNBUFFERED='1')
    result = subprocess.run([sys.executable, str(root / 'tests/gateway/fixtures/shell_exec_peer.py')],
                            cwd=root, env=env, capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((home / 'receipt.json').read_text())
    assert receipt['owner_cwd'] != receipt['session_cwd']
    ran = receipt['ran']
    assert isinstance(ran, dict) and ran.get('code') == 3, receipt
    assert Path(ran['stdout'].strip()).resolve() == Path(receipt['session_cwd']).resolve(), receipt
    assert ran['stderr'] == 'key=', receipt
    assert receipt['dangerous'].get('message') == 'dangerous_command', receipt
    assert receipt['sessionless'] == 4001, receipt
    assert receipt['cancellation']['cancel']['result'] == {}
    assert receipt['cancellation']['long-command']['error']['message'] == 'command_interrupted'


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['docker', 'ssh'])
async def test_shell_exec_refusal_names_a_sandboxed_terminal_backend(tmp_path, monkeypatch, backend):
    """``shell.exec`` runs on the gateway host, so a session whose frozen terminal policy is a
    sandbox is refused (#134889 TUI-16) and the refusal names that backend for the client. A local
    session still runs. Monkeypatched: the session's policy lookup and local-source check."""
    from types import SimpleNamespace

    from gateway import session_local, session_policy
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session

    work = tmp_path / 'work'
    work.mkdir()
    terminal = {'TERMINAL_ENV': backend}
    monkeypatch.setattr(session_local, 'authorize_local_source', lambda runner, source: True)
    monkeypatch.setattr(session_policy, 'policy_for_source', lambda runner, source: SimpleNamespace(
        cwd=str(work), terminal_json=json.dumps(terminal)))
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, 's', 'test')
        epoch = db.runtime.begin_epoch(instance_id='current')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(tmp_path / 'home'),
                                     instance_id='current', persistence=storage_handle(db.scope.profile_dir), epoch=epoch)
        authority.sessions['s'] = LiveSession(None, 'route')
        connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})

        async def call():
            return await connection.dispatch({'id': 1, 'method': 'shell.exec',
                                              'params': {'session_id': 's', 'command': 'echo ran'}})

        refused = await call()
        assert refused['error']['data'] == {'reason': 'unsupported_terminal_backend',
                                            'field': 'terminal.backend', 'detail': backend}, refused
        terminal['TERMINAL_ENV'] = 'local'
        ran = await call()
        assert ran.get('result', {}).get('stdout', '').strip() == 'ran', ran


@pytest.mark.asyncio
async def test_managed_code_shell_uses_owning_route_and_requires_native_operator(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from gateway import session_local, session_policy, session_shell
    from hermes_cli.managed_operator_shell import Placement
    from persistence.core.errors import AuthorityRefusal
    from tools.terminal_tool import clear_session_cwd, record_session_cwd

    managed = Placement('executor', {})
    monkeypatch.setattr(session_shell, 'placement', lambda: managed)
    monkeypatch.setattr(session_local, 'authorize_local_source', lambda runner, source: True)
    monkeypatch.setattr(session_policy, 'policy_for_source', lambda runner, source: SimpleNamespace(
        cwd='/owner-cwd', terminal_json='{"TERMINAL_ENV":"ssh"}'))
    monkeypatch.setattr('gateway.session_busy_controls.authorize', lambda *args: None)
    calls = []

    async def run(placement, cmd, cwd, *, timeout):
        calls.append((placement, cmd, cwd, timeout))
        return subprocess.CompletedProcess([], 4, 'stdout', 'stderr')

    monkeypatch.setattr(session_shell, 'run_executor_async', run)
    authority = SimpleNamespace(sessions={'stored-id': SimpleNamespace(source=object(), route='actual-route')},
                                runner=object(), profile_id=str(tmp_path), _require_admission_open=lambda: None)
    actor = SimpleNamespace(capabilities={'session:operator'})
    connection = SimpleNamespace(authority=authority, actor=actor, native_owner=False)
    params = {'session_id': 'stored-id', 'command': 'pwd'}
    ref = SimpleNamespace(session_id='stored-id')
    record_session_cwd('stored-id', '/wrong')
    record_session_cwd('actual-route', '/literal ${name}')
    try:
        with pytest.raises(AuthorityRefusal, match='permission_denied'):
            await session_shell.shell_exec(connection, ref, params)
        assert calls == []
        connection.native_owner = True
        result = await session_shell.shell_exec(connection, ref, params)
        managed = Placement('gateway', {})
        assert await session_shell.shell_cwd(connection, ref, {}) == {'environment': 'gateway', 'cwd': '/owner-cwd'}
        assert await session_shell.shell_cwd(connection, ref, {'executor': True}) == {
            'environment': 'gateway', 'cwd': '/literal ${name}'}
        monkeypatch.setattr('tools.file_tools_paths.managed_ssh_workspace_roots', lambda: ('/workspace',), raising=False)
        assert await session_shell.shell_cwd(connection, ref, {}) == {
            'environment': 'gateway', 'cwd': str(tmp_path)}
        local = await session_shell.shell_exec(connection, ref, params)
        assert local['stdout'].strip() == str(tmp_path.resolve())
        def unavailable_roots():
            raise ValueError('managed executor roots are unavailable')

        monkeypatch.setattr('tools.file_tools_paths.managed_ssh_workspace_roots', unavailable_roots, raising=False)
        assert await session_shell.shell_cwd(connection, ref, {}) == {
            'environment': 'gateway', 'cwd': str(tmp_path)}
        assert (await session_shell.shell_exec(connection, ref, params))['stdout'].strip() == str(tmp_path.resolve())
        with pytest.raises(AuthorityRefusal, match='invalid_params'):
            await session_shell.shell_cwd(connection, ref, {'executor': 'true'})
    finally:
        clear_session_cwd('stored-id')
        clear_session_cwd('actual-route')
    assert calls == [(Placement('executor', {}), 'pwd', '/literal ${name}', session_shell.TIMEOUT_SECONDS)]
    assert result == {'code': 4, 'stdout': 'stdout', 'stderr': 'stderr'}
