"""Exact POSIX cwd and scoped optional SSH terminal preparation."""
import asyncio
import json
import os
import shlex
import subprocess
import sys
from unittest.mock import Mock, AsyncMock

import pytest

from hermes_cli import web_host_terminal as terminal
from hermes_cli.gateway_connections_ssh_transport import SshEndpoint


@pytest.mark.platforms('posix')
def test_native_shell_launch_preserves_literal_cwd(tmp_path):
    directory = tmp_path / "space ü ' $(touch NEVER); trailing "
    directory.mkdir()
    actual = terminal.safe_cwd(str(directory))
    result = subprocess.run(['bash', '-c', 'pwd -P'], cwd=actual, capture_output=True, text=True, check=True)
    assert result.stdout.rstrip('\n') == str(directory)
    probe = subprocess.run([sys.executable, '-c', terminal._PROBE, str(directory)], capture_output=True, text=True, check=True)
    assert json.loads(probe.stdout) == str(directory)
    assert not (directory / 'NEVER').exists()


@pytest.mark.asyncio
async def test_gateway_mode_does_not_check_executor(monkeypatch,tmp_path):
    monkeypatch.setattr(terminal,'resolve_argv',lambda **kw: (['sh','-i'],str(tmp_path),{},'sh'))
    remote=Mock(side_effect=AssertionError('gateway must not inspect executor'))
    monkeypatch.setattr(terminal,'_executor_connection',remote)
    result=await terminal.prepare(tmp_path,mode='gateway')
    assert result[0]==['sh','-i'] and result[3]=='sh'
    remote.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_selects_its_profile_cwd_without_file_backend(tmp_path):
    profile = tmp_path / 'profile'
    profile.mkdir()
    workspace = tmp_path / 'configured workspace '
    workspace.mkdir()
    explicit = tmp_path / 'explicit workspace '
    explicit.mkdir()
    (profile / 'config.yaml').write_text(
        f'terminal: {{backend: ssh, ssh_host: unused.test, cwd: {json.dumps(str(workspace))}}}\n')

    assert (await terminal.prepare(profile, mode='gateway'))[-1] == str(workspace)
    assert (await terminal.prepare(profile, str(explicit), mode='gateway'))[-1] == str(explicit)


@pytest.mark.asyncio
async def test_executor_failure_has_no_local_fallback(monkeypatch,tmp_path):
    monkeypatch.setattr(terminal,'_resolve_argv',lambda **kw: (['sh'],str(tmp_path),{},'sh'))
    monkeypatch.setattr(terminal,'_executor_connection',lambda: ('ssh',SshEndpoint('fixture','runner'),'/remote'))
    monkeypatch.setattr(terminal.asyncio,'create_subprocess_exec',AsyncMock(return_value=Mock(returncode=255,communicate=AsyncMock(return_value=(b'',None)))))
    with pytest.raises(terminal.ExecutorTerminalError,match='host key') as error:
        await terminal.prepare(tmp_path,mode='executor')
    assert 'private' not in str(error.value)


@pytest.mark.asyncio
async def test_executor_missing_bash_refuses_before_producing_shell_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, '_executor_settings',
        lambda _home: (dict(os.environ, PATH=str(tmp_path)),
                       ('ssh', SshEndpoint('fixture', 'runner'), str(tmp_path))))
    spawn = asyncio.create_subprocess_exec

    async def apply_probe(*argv, **kwargs):
        _, flag, probe, cwd = shlex.split(argv[-1])
        return await spawn(sys.executable, flag, probe, cwd, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', apply_probe)
    with pytest.raises(terminal.ExecutorTerminalError, match='Bash is unavailable'):
        await terminal.prepare(tmp_path, mode='executor')


@pytest.mark.asyncio
async def test_executor_shell_argv_keeps_remote_cwd_as_data(monkeypatch,tmp_path):
    requested="/remote/space ' $(touch NEVER); trailing "
    monkeypatch.setattr(terminal,'_resolve_argv',lambda **kw: (['sh'],str(tmp_path),{},'sh'))
    monkeypatch.setattr(terminal,'_executor_connection',lambda: ('ssh',SshEndpoint('fixture','runner'),'/remote'))
    monkeypatch.setattr(terminal.asyncio,'create_subprocess_exec',AsyncMock(return_value=Mock(returncode=0,communicate=AsyncMock(return_value=(json.dumps(requested).encode(),None)))))
    argv,cwd,_env,label,remote=await terminal.prepare(tmp_path,requested,mode='executor')
    assert argv[:2]==['ssh','-tt'] and argv[-2]=='runner@fixture'
    assert cwd==str(tmp_path) and remote==requested and label=='Executor · runner@fixture'


@pytest.mark.asyncio
async def test_cancel_preparation_kills_and_reaps_the_actual_probe(monkeypatch, tmp_path):
    import asyncio
    from tests.waiting import async_wait_until
    monkeypatch.setattr(terminal, '_resolve_argv', lambda **kw: (['sh'], str(tmp_path), dict(__import__('os').environ), 'sh'))
    monkeypatch.setattr(terminal, '_executor_connection', lambda: ('ssh', SshEndpoint('fixture', 'runner'), '/remote'))
    spawn = asyncio.create_subprocess_exec
    processes = []
    ready = tmp_path / 'ready'
    async def probe(*args, **kwargs):
        proc = await spawn(sys.executable, '-c',
            f'import pathlib,threading; pathlib.Path({str(ready)!r}).touch(); threading.Event().wait()', **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', probe)
    preparing = asyncio.create_task(terminal.prepare(tmp_path, mode='executor'))
    try:
        await async_wait_until(ready.exists, timeout=10)
        assert not preparing.done()
        preparing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(preparing, 10)
        assert processes[0].returncode is not None
    finally:
        preparing.cancel()
        if processes and processes[0].returncode is None:
            processes[0].kill()
            await processes[0].wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('simultaneous', [False, True])
async def test_startup_disconnect_cancels_admission_and_removes_racing_shell(monkeypatch, simultaneous):
    import asyncio
    from types import SimpleNamespace
    from hermes_cli import web_host_terminal_sessions as sessions
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    shell = SimpleNamespace(key='owned-shell')
    async def admit(ws, registry):
        entered.set()
        try:
            await release.wait()
            return shell, object()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    monkeypatch.setattr(sessions, '_admit', admit)
    ended = asyncio.get_running_loop().create_future()
    registry = SimpleNamespace(remove=AsyncMock())
    startup = asyncio.create_task(sessions._admit_startup(object(), registry, ended))
    await entered.wait()
    if simultaneous:
        release.set()
    ended.set_result(None)
    with pytest.raises(sessions.WebSocketDisconnect):
        await asyncio.wait_for(startup, 10)
    if simultaneous:
        registry.remove.assert_awaited_once_with('owned-shell')
    else:
        assert cancelled.is_set()
        registry.remove.assert_not_called()


@pytest.mark.asyncio
async def test_real_startup_route_observes_disconnect_before_any_pty_spawn(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import threading
    from hermes_cli import web_host_terminal_sessions as sessions, web_server_chat
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def preparing(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(terminal, 'prepare', preparing)
    loop_thread = threading.get_ident()

    def request_home(profile):
        assert threading.get_ident() != loop_thread
        return tmp_path

    monkeypatch.setattr(sessions, '_request_home', request_home)
    spawn = Mock(side_effect=AssertionError('disconnected startup spawned a shell'))
    monkeypatch.setattr(web_server_chat.PtyBridge, 'spawn', spawn)
    inbox = asyncio.Queue()
    registry = sessions.HostTerminalRegistry()
    ws = SimpleNamespace(state=SimpleNamespace(ssh_terminal=None), query_params={'environment': 'executor'},
        app=SimpleNamespace(state=SimpleNamespace(host_terminals=registry, terminal_environment='executor')),
        _hermes_auth_identity={'provider': 'fixture', 'user_id': 'operator'}, receive=inbox.get)
    task = asyncio.create_task(sessions.host_terminal(ws))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert not task.done()  # Connected preparation remains valid without a work deadline.
        inbox.put_nowait({'type': 'websocket.disconnect'})
        await asyncio.wait_for(task, 10)
        assert cancelled.is_set()
        assert not registry._sessions and not registry.owners
        spawn.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registry.close_all()


@pytest.mark.asyncio
async def test_pinned_receiver_refuses_new_environment_but_keeps_token_close():
    from types import SimpleNamespace
    from hermes_cli import web_host_terminal_sessions as sessions

    ws = SimpleNamespace(
        state=SimpleNamespace(), query_params={'environment': 'executor', 'action': 'start'},
        app=SimpleNamespace(state=SimpleNamespace(terminal_environment='gateway')),
        _hermes_auth_identity={'provider': 'fixture', 'user_id': 'operator'}, send_text=AsyncMock(), close=AsyncMock(),
    )
    registry = SimpleNamespace(create=AsyncMock(), resolve=AsyncMock(), remove=AsyncMock(), _sessions={})
    with pytest.raises(terminal.ExecutorTerminalError, match='environment changed'):
        await sessions._admit(ws, registry)
    registry.create.assert_not_called()

    token = 'A' * 43
    registry._sessions[token] = object()
    registry.resolve.return_value = (object(), SimpleNamespace(environment='executor'))
    ws.query_params = {'attach': token, 'environment': 'executor', 'action': 'close'}
    assert await sessions._admit(ws, registry) is None
    registry.resolve.assert_awaited_once_with(token, ws, ('fixture', 'operator'), closing=True)
    registry.remove.assert_awaited_once_with(token)


def test_executor_connection_uses_selected_profile_settings_and_restores_scope(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    selected = tmp_path / 'selected'
    selected.mkdir()
    key = selected / 'identity'
    key.write_text('synthetic key path; no connection is made')
    (selected / 'config.yaml').write_text(
        'terminal:\n  backend: ssh\n  ssh_host: executor.test\n  ssh_user: worker\n'
        f'  ssh_port: 2222\n  ssh_key: {key}\n  cwd: /selected workspace\n')
    env, (_, endpoint, cwd) = terminal._executor_settings(selected)
    assert env["TERMINAL_SSH_HOST"] == endpoint.host
    assert endpoint.host == 'executor.test' and endpoint.user == 'worker'
    assert endpoint.port == 2222 and endpoint.key_path == str(key)
    assert cwd == '/selected workspace'
    assert get_hermes_home() == home


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    'first-metadata', 'replay', 'none', 'in-flight-token', 'early-ack', 'wrong-token', 'acknowledged',
])
async def test_startup_disconnect_retains_only_published_shells(tmp_path, monkeypatch, failure):
    import asyncio
    from types import SimpleNamespace
    from hermes_cli import web_host_terminal_sessions as sessions, web_server_chat
    from tests.hermes_cli.test_web_host_terminal_persistence import IdleBridge
    inbox = asyncio.Queue()
    observed_disconnect = asyncio.Event()
    metadata = []
    async def receive():
        item = await inbox.get()
        if item['type'] == 'websocket.disconnect':
            observed_disconnect.set()
        return item
    async def send_text(text):
        if failure == 'first-metadata':
            raise OSError('viewer gone before metadata')
        frame = json.loads(text.removeprefix(terminal.META_PREFIX))
        if frame.get('preparing'):
            assert frame['ackStartup']
            if failure == 'early-ack':
                inbox.put_nowait({'type': 'websocket.receive', 'text': terminal.META_PREFIX + '{"attached":true}'})
            return
        metadata.append(frame)
        if failure in {'acknowledged', 'wrong-token'}:
            token = frame['terminalId'] if failure == 'acknowledged' else 'different-terminal'
            echo = terminal.META_PREFIX + json.dumps({'attached': token}, separators=(',', ':'))
            inbox.put_nowait({'type': 'websocket.receive', 'text': echo})
        inbox.put_nowait({'type': 'websocket.disconnect'})
    async def send_bytes(data):
        # Metadata was published, then the reader observed viewer loss while
        # the native replay send was in flight. Replay itself still succeeds.
        await observed_disconnect.wait()
        if failure == 'replay':
            raise OSError('viewer gone during replay')
    async def prepare(*args, **kwargs):
        return ['fixture-shell'], str(tmp_path), {}, 'fixture-shell', str(tmp_path)
    monkeypatch.setattr(terminal, 'prepare', prepare)
    monkeypatch.setattr(sessions, '_request_home', lambda profile: tmp_path)
    bridge = IdleBridge()
    monkeypatch.setattr(web_server_chat.PtyBridge, 'spawn', lambda *args, **kwargs: bridge)
    registry = sessions.HostTerminalRegistry()
    ws = SimpleNamespace(state=SimpleNamespace(ssh_terminal=None), query_params={'environment': 'executor'},
        app=SimpleNamespace(state=SimpleNamespace(host_terminals=registry, terminal_environment='executor')),
        _hermes_auth_identity={'provider': 'fixture', 'user_id': 'operator'}, receive=receive,
        send_text=send_text, send_bytes=send_bytes, close=AsyncMock())
    if failure in {'in-flight-token', 'early-ack', 'wrong-token', 'acknowledged'}:
        ws.query_params['startup'] = '1'
    try:
        await asyncio.wait_for(sessions.host_terminal(ws), 10)
        if failure in {'first-metadata', 'in-flight-token', 'early-ack', 'wrong-token'}:
            assert bool(metadata) == (failure != 'first-metadata')
            assert not registry._sessions
            assert bridge.closed.is_set()
            return
        token = metadata[0]['terminalId']
        assert token in registry._sessions
        assert registry._sessions[token].alive and not registry._sessions[token].attached
        assert not bridge.closed.is_set()
    finally:
        await registry.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['serve', 'dashboard', 'gateway', 'webapp'])
async def test_only_terminal_surface_reads_launch_settings(monkeypatch, surface):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from hermes_cli import config_effective, web_server_app

    @asynccontextmanager
    async def fixture_lifespan(_app):
        yield

    app = FastAPI()
    app.state.ui_surface = surface
    invalid = Mock(return_value={'webapp': {'terminal_environment': 'invalid'}})
    monkeypatch.setattr(config_effective, 'load_user_config_effective', invalid)
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', fixture_lifespan)
    async with web_server_app.app_lifespan(app):
        if surface == 'webapp':
            invalid.assert_called_once_with(fail_closed=True, managed_snapshot={})
            assert terminal.environment(app) is None
            assert 'webapp.terminal_environment' in app.state.terminal_config_error
        else:
            invalid.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('source, text', [
    ('profile', 'webapp: [invalid'), ('profile', 'webapp: false'),
    ('profile', 'false'), ('profile', '[]'), ('profile', '0'), ('profile', "''"),
    ('managed', '[]'),
])
async def test_failed_launch_settings_never_select_a_gateway_shell(tmp_path, monkeypatch, source, text):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from fastapi import FastAPI
    import hermes_constants
    from hermes_cli import web_host_terminal_sessions as sessions, web_server_app

    @asynccontextmanager
    async def fixture_lifespan(_app):
        yield

    profile = tmp_path / 'profile'
    profile.mkdir()
    config = profile / 'config.yaml'
    if source == 'managed':
        managed = tmp_path / 'managed'
        managed.mkdir()
        monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
        config = managed / 'config.yaml'
    config.write_text(text)
    monkeypatch.setattr(hermes_constants, 'get_process_hermes_home', lambda: profile)
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', fixture_lifespan)

    app = FastAPI()
    app.state.ui_surface = 'webapp'
    registry = app.state.host_terminals = SimpleNamespace(_closed=False, create=AsyncMock())
    ws = SimpleNamespace(state=SimpleNamespace(), app=app, query_params={}, close=AsyncMock(),
                         receive=AsyncMock(side_effect=asyncio.Event().wait))
    async with web_server_app.app_lifespan(app):
        assert terminal.environment(app) is None
        # The requested profile is healthy by the time the operator connects.
        # This must not turn a failed launch selection into a gateway terminal.
        config.write_text('webapp:\n  terminal_environment: executor\n')
        await sessions.host_terminal(ws)
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs['code'] == 4411
        registry.create.assert_not_awaited()
    async with web_server_app.app_lifespan(app):
        assert terminal.environment(app) == 'executor'
        assert app.state.terminal_config_error is None
        ws.query_params = {'environment': 'unavailable'}
        ws.close.reset_mock()
        await sessions.host_terminal(ws)
        assert ws.close.call_args.kwargs == {'code': 4411, 'reason': 'Terminal environment changed; reload the WebApp'}
        registry.create.assert_not_awaited()


@pytest.mark.parametrize('text', ['', '# no terminal override\n', 'null'])
def test_absent_user_terminal_options_with_empty_managed_mapping_keep_gateway_default(tmp_path, monkeypatch, text):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, 'get_process_hermes_home', lambda: tmp_path)
    (tmp_path / 'config.yaml').write_text(text)
    managed = tmp_path / 'managed'
    managed.mkdir()
    (managed / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    assert terminal.configured_terminal() == ('gateway', '')


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_pinned_ssh_configuration_applies_to_probe_and_pty(monkeypatch, tmp_path):
    import shutil
    import subprocess
    from hermes_cli.gateway_connections_ssh_transport import SshEndpoint

    ssh = shutil.which('ssh')
    if not ssh:
        pytest.skip('OpenSSH is unavailable')
    ssh_config = tmp_path / 'ssh_config'
    known_hosts = tmp_path / 'known_hosts'
    ssh_config.write_text(
        f'Host executor.test\n  UserKnownHostsFile "{known_hosts}"\n  Port 2222\n'
        '  ForwardAgent yes\n  ForwardX11 yes\n  LocalForward 12345 localhost:12346\n'
    )
    key = tmp_path / 'executor key'
    key.touch()
    monkeypatch.setattr(terminal, '_executor_connection',
                        lambda: (ssh, SshEndpoint('executor.test', 'runner', 22, str(key)), '/default'))
    probe = AsyncMock()
    probe.communicate.return_value = (b'"/default"', b'')
    probe.returncode = 0
    launch = AsyncMock(return_value=probe)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    argv, *_ = await terminal.prepare(tmp_path, mode='executor', ssh_config=str(ssh_config))

    for invocation in (launch.call_args.args, argv):
        effective = await asyncio.to_thread(subprocess.check_output, [invocation[0], '-G', *invocation[1:]], text=True)
        options = dict(line.split(' ', 1) for line in effective.splitlines() if not line.startswith('identityfile '))
        assert options['user'] == 'runner' and options['port'] == '22'
        assert options['hostname'] == 'executor.test'
        assert f'identityfile {key}' in effective.splitlines()
        assert options['userknownhostsfile'] == str(known_hosts)
        assert options['stricthostkeychecking'] == 'true'
        assert options['identitiesonly'] == 'yes' and options['forwardagent'] == 'no'
        assert options['forwardx11'] == 'no' and 'localforward' not in options

    launch.reset_mock()
    key.unlink()
    with pytest.raises(terminal.ExecutorTerminalError, match='configured SSH identity is missing'):
        await terminal.prepare(tmp_path, mode='executor', ssh_config=str(ssh_config))
    launch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('seam', ['_resolve_argv', '_executor_connection'])
async def test_configuration_backend_failure_refuses_only_startup_and_recovers(tmp_path, monkeypatch, seam):
    import asyncio
    from types import SimpleNamespace
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli import web_host_terminal_sessions as sessions, web_server_chat
    local = Mock(return_value=(['sh'], str(tmp_path), {}, 'sh'))
    connection = Mock(return_value=('ssh', SshEndpoint('fixture', 'runner'), '/remote'))
    monkeypatch.setattr(terminal, '_resolve_argv', local)
    monkeypatch.setattr(terminal, '_executor_connection', connection)
    refusing = local if seam == '_resolve_argv' else connection
    refusing.side_effect = ConfigBackendUnavailable('SYNTHETIC-PRIVATE-BACKEND-DETAILS')
    process = Mock(returncode=0, communicate=AsyncMock(return_value=(b'"/remote"', None)))
    probe = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', probe)
    spawn = Mock(side_effect=AssertionError('unavailable configuration spawned a PTY'))
    monkeypatch.setattr(web_server_chat.PtyBridge, 'spawn', spawn)
    monkeypatch.setattr(sessions, '_request_home', lambda profile: tmp_path)
    registry = sessions.HostTerminalRegistry()
    inbox = asyncio.Queue()
    ws = SimpleNamespace(state=SimpleNamespace(ssh_terminal=None), query_params={'environment': 'executor'},
        app=SimpleNamespace(state=SimpleNamespace(host_terminals=registry, terminal_environment='executor')),
        _hermes_auth_identity={'provider': 'fixture', 'user_id': 'operator'}, receive=inbox.get, close=AsyncMock())
    try:
        await sessions.host_terminal(ws)
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs['code'] == 4411
        reason = ws.close.call_args.kwargs['reason']
        assert 'configuration' in reason and 'SYNTHETIC-PRIVATE' not in reason
        probe.assert_not_called()
        spawn.assert_not_called()
        assert not registry._sessions and not registry.owners
        # The same event loop serves a successful retry after the read is repaired.
        refusing.side_effect = None
        result = await terminal.prepare(tmp_path, mode='executor')
        assert result[-1] == '/remote'
        probe.assert_awaited_once()
    finally:
        await registry.close_all()


def test_unsearchable_managed_path_cannot_select_default_gateway(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli import config_effective

    managed = tmp_path / 'managed'
    managed.mkdir()
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    monkeypatch.setattr(config_effective, 'load_user_config_effective', lambda **_: {})
    stat = Path.stat

    def unsearchable(path, *args, **kwargs):
        if path in (managed, managed / 'config.yaml'):
            raise PermissionError('SYNTHETIC-PRIVATE-MANAGED-LOCATION')
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        is_dir = Path.is_dir
        scoped.setattr(Path, 'is_dir', lambda path: False if path == managed else is_dir(path))
        scoped.setattr(Path, 'stat', unsearchable)
        with pytest.raises(terminal.ExecutorTerminalError, match='launch configuration is unavailable'):
            terminal.configured_terminal()
    assert terminal.configured_terminal() == ('gateway', '')  # a genuinely absent file is optional


def test_launch_decision_uses_the_managed_snapshot_it_validated(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = tmp_path / 'managed'
    managed.mkdir()
    path = managed / 'config.yaml'
    path.write_text('webapp:\n  terminal_environment: executor\n  executor_ssh_config: /fixed/ssh_config\n')
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    native_read = managed_scope.load_managed_config
    reads = []

    def edited_after_read(*, strict=False):
        reads.append(strict)
        config = native_read(strict=strict)
        if strict and reads.count(True) == 1:
            path.unlink()
        return config

    monkeypatch.setattr(managed_scope, 'load_managed_config', edited_after_read)
    assert terminal.configured_terminal() == ('executor', '/fixed/ssh_config')
    assert reads.count(True) == 1
    for invalid in ('webapp: [unterminated', 'null'):
        path.write_text(invalid)
        with pytest.raises(terminal.ExecutorTerminalError, match='launch configuration is unavailable'):
            terminal.configured_terminal()


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
@pytest.mark.parametrize('broken_policy', ['terminal: [unfinished', 'terminal: null', 'terminal: []', 'null'])
async def test_new_executor_shell_uses_one_managed_terminal_snapshot(tmp_path, monkeypatch, broken_policy):
    import shutil
    from hermes_cli import managed_scope

    if not shutil.which('ssh'):
        pytest.skip('OpenSSH is unavailable')
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'config.yaml').write_text(
        'terminal: {backend: ssh, ssh_host: fallback.test, ssh_user: fallback, cwd: /fallback}\n')
    managed = tmp_path / 'managed'
    managed.mkdir()
    config = managed / 'config.yaml'
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    pins = ('terminal: {backend: ssh, ssh_host: pinned.test, ssh_user: runner, '
            'ssh_port: 2209, cwd: /pinned}\n')
    config.write_text(pins)
    real_read = managed_scope.load_managed_config
    strict_reads = []

    def replace_after_read(*, strict=False):
        snapshot = real_read(strict=strict)
        if strict:
            strict_reads.append(snapshot)
            config.write_text(broken_policy)
        return snapshot

    process = Mock(returncode=0, communicate=AsyncMock(return_value=(b'"/pinned"', None)))
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(managed_scope, 'load_managed_config', replace_after_read)
    argv, _, env, _, cwd = await terminal.prepare(profile, mode='executor')

    assert len(strict_reads) == 1
    probe = launch.call_args.args
    for invocation in (probe, argv):
        assert 'runner@pinned.test' in invocation
        assert invocation[invocation.index('-p') + 1] == '2209'
        assert '/pinned' in invocation[-1]
    assert cwd == '/pinned'
    assert env['TERMINAL_SSH_HOST'] == 'pinned.test'
    assert launch.call_args.kwargs['env']['TERMINAL_CWD'] == '/pinned'

    launch.reset_mock()
    with pytest.raises(terminal.ExecutorTerminalError, match='configuration is unavailable'):
        await terminal.prepare(profile, mode='executor')
    launch.assert_not_called()

    monkeypatch.setattr(managed_scope, 'load_managed_config', real_read)
    config.write_text(pins)
    repaired = await terminal.prepare(profile, mode='executor')
    assert repaired[-1] == '/pinned'
    launch.assert_awaited_once()



@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', ['terminal:', 'terminal: []', '- terminal'])
async def test_executor_rejects_invalid_profile_before_using_launch_ssh_defaults(tmp_path, monkeypatch, malformed):
    import hermes_constants
    from tui_gateway import launch_profile_policy

    monkeypatch.setattr(hermes_constants, 'get_process_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, 'get_routing_process_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(launch_profile_policy, 'launch_terminal_env', lambda: {
        'TERMINAL_ENV': 'ssh', 'TERMINAL_SSH_HOST': 'fallback.test', 'TERMINAL_SSH_USER': 'fallback',
    })
    (tmp_path / 'config.yaml').write_text(malformed)
    probe = AsyncMock(side_effect=AssertionError('invalid profile selected inherited SSH settings'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', probe)
    with pytest.raises(terminal.ExecutorTerminalError, match='configuration is unavailable'):
        await terminal.prepare(tmp_path, mode='executor')
    probe.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('inaccessible', ['.env', 'config.yaml'])
async def test_executor_refuses_inaccessible_profile_before_using_inherited_ssh(tmp_path, monkeypatch, inaccessible):
    import builtins
    import os
    import hermes_constants
    from tui_gateway import launch_profile_policy

    monkeypatch.setattr(hermes_constants, 'get_process_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, 'get_routing_process_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(launch_profile_policy, 'launch_terminal_env', lambda: {
        'TERMINAL_ENV': 'ssh', 'TERMINAL_SSH_HOST': 'fallback.test', 'TERMINAL_SSH_USER': 'fallback',
    })
    path = tmp_path / inaccessible
    path.touch()
    real_open = builtins.open
    real_stat = os.stat
    can_read = False

    def profile_open(file, *args, **kwargs):
        if file == path and not can_read:
            raise PermissionError('private profile path')
        return real_open(file, *args, **kwargs)

    def profile_stat(file, *args, **kwargs):
        if file in (path, str(path)) and not can_read:
            raise PermissionError('private profile path')
        return real_stat(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', profile_open)
    monkeypatch.setattr(os, 'stat', profile_stat)
    probe = AsyncMock(return_value=Mock(returncode=0, communicate=AsyncMock(return_value=(b'"/remote"', None))))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', probe)

    with pytest.raises(terminal.ExecutorTerminalError, match='configuration is unavailable') as error:
        await terminal.prepare(tmp_path, mode='executor')
    assert 'private profile path' not in str(error.value)
    probe.assert_not_called()
    can_read = True
    assert (await terminal.prepare(tmp_path, mode='executor'))[-1] == '/remote'
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_ssh_port_refuses_admission_but_unrelated_tool_timeout_does_not(tmp_path, monkeypatch):
    import hermes_constants
    from tui_gateway import launch_profile_policy

    monkeypatch.setattr(hermes_constants, 'get_process_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, 'get_routing_process_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(launch_profile_policy, 'launch_terminal_env', lambda: {
        'TERMINAL_ENV': 'ssh', 'TERMINAL_SSH_HOST': 'executor.test', 'TERMINAL_SSH_USER': 'runner',
    })
    config = tmp_path / 'config.yaml'
    config.write_text('terminal:\n  ssh_port: private-invalid-value\n  timeout: irrelevant-to-the-pty\n')
    probe = AsyncMock(return_value=Mock(returncode=0, communicate=AsyncMock(return_value=(b'"/remote"', None))))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', probe)

    with pytest.raises(terminal.ExecutorTerminalError, match='check the configured SSH') as error:
        await terminal.prepare(tmp_path, mode='executor')
    assert 'private-invalid-value' not in str(error.value)
    probe.assert_not_called()
    config.write_text('terminal:\n  ssh_port: 2222\n  timeout: irrelevant-to-the-pty\n')
    assert (await terminal.prepare(tmp_path, mode='executor'))[-1] == '/remote'
    probe.assert_awaited_once()


def test_webapp_executor_terminal_config_keys_are_registered():
    from hermes_cli.config import _validate_config_key

    assert _validate_config_key('webapp.terminal_environment') == (True, None)
    assert _validate_config_key('webapp.executor_ssh_config') == (True, None)
