"""Executor routing and by-object cleanup compose with native browser ownership."""
import json
import pytest
from unittest.mock import Mock

from tools import browser_use_cli as native
from tools import browser_use_executor as executor
from tools import browser_session_lifecycle as lifecycle


def test_native_executor_route_does_not_launch_gateway_python(monkeypatch):
    routed = {'BU_CDP_WS': 'ws://127.0.0.1:9/devtools/browser/fixture',
              native._EXECUTOR_BROWSER_SENTINEL: 'selected-executor'}
    monkeypatch.setattr(native, '_base_subprocess_env', dict)
    monkeypatch.setattr(native, '_needs_gateway_harness', lambda *args: False)
    monkeypatch.setattr(native, '_route_exec_current', lambda *args: (routed, None, False, 0))
    monkeypatch.setattr(native, '_attach_vault_supervisor', Mock())
    monkeypatch.setattr(native, '_find_cli', Mock(side_effect=AssertionError('gateway Python must not run')))
    remote = Mock(return_value={'success': True})
    monkeypatch.setattr(executor, 'execute', remote)
    assert native.browser_exec('print(1)', session='research', task_id='task') == {'success': True}
    assert remote.call_args.args == ('selected-executor', 'print(1)', 'research', 300, 'task')


def test_missing_gateway_harness_refuses_before_cdp_route_discovery(monkeypatch):
    from tools import browser_session_cdp

    monkeypatch.setattr(native, '_base_subprocess_env', dict)
    monkeypatch.setattr(native, '_find_cli', lambda: None)
    monkeypatch.setattr(browser_session_cdp, 'get_cdp_override_raw', lambda: 'http://cdp.test:9222')
    route = Mock(side_effect=AssertionError('destination cannot be provisioned without the gateway harness'))
    monkeypatch.setattr(native, '_route_exec_current', route)
    assert 'browser-harness is missing' in json.loads(native.browser_exec('print(1)'))['error']
    route.assert_not_called()


def test_cloud_hybrid_sidecar_needs_only_the_executor_harness(monkeypatch):
    from tools import browser_lightpanda, browser_session, browser_session_cdp, browser_session_cloud
    from tools.bot_desktop import placement

    monkeypatch.setattr(native, "_base_subprocess_env", dict)
    monkeypatch.setattr(native, "_real_profile_consented", lambda: True)
    monkeypatch.setattr(native, "_find_cli", lambda: None)
    monkeypatch.setattr(native, "_code_url_verdict", lambda code: native._CodeUrlVerdict(None, True))
    monkeypatch.setattr(browser_session_cdp, "get_cdp_override_raw", lambda: "")
    monkeypatch.setattr(browser_session_cdp, "get_cdp_override", lambda: "")
    monkeypatch.setattr(browser_session_cloud, "get_cloud_provider", lambda: object())
    monkeypatch.setattr(browser_lightpanda, "using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, "ssh"))
    monkeypatch.setattr(placement, "terminal_environment", lambda **_kw: object())
    monkeypatch.setattr(executor, "available", lambda **_kw: True)
    monkeypatch.setattr(executor, "probe_harness", lambda _env: True)
    session = Mock(return_value={"features": {"sandbox": True}, "cdp_url": "ws://127.0.0.1:9"})
    monkeypatch.setattr(browser_session, "get_session_info", session)
    run_remote = Mock(return_value={"success": True})
    monkeypatch.setattr(executor, "execute", run_remote)

    assert native.browser_exec_available()
    code = "goto_url('http://private.test')"
    assert native.browser_exec(code, task_id="hybrid") == {"success": True}
    assert session.call_args.args[0].endswith("::local")
    run_remote.assert_called_once_with(session.call_args.args[0], code, "", 300, "hybrid")


def test_losing_unpublished_browser_cannot_close_published_executor(monkeypatch):
    winner = {'features': {'sandbox': True}}
    loser = {'features': {'sandbox': True}}
    process = Mock()
    current = executor.Session(object(), process, 'home', '/workspace', 'endpoint', winner)
    monkeypatch.setattr(executor, '_sessions', {'key': current})
    lifecycle.release_session_backend('key', loser)
    process.stdin.close.assert_not_called()
    assert executor._sessions['key'] is current
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    lifecycle.release_session_backend('key', winner)
    process.stdin.close.assert_called_once()
    assert 'key' not in executor._sessions


def test_stale_liveness_result_cannot_retire_published_successor(monkeypatch):
    from tools import browser_session
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    key = "reused-executor-browser"
    stale = {"features": {"local": True}}
    process = Mock()
    supervisor = Mock()
    monkeypatch.setattr(browser_session, "_active_sessions", {key: stale})
    monkeypatch.setattr(browser_session, "_session_last_activity", {})
    monkeypatch.setattr(browser_session, "_session_owner_homes", {})
    monkeypatch.setattr(browser_session, "_cleanup_failures", {})
    monkeypatch.setattr(browser_session, "_create_session_for_key", lambda *_args: {"features": {"local": True}})
    monkeypatch.setattr(lifecycle, "start_cleanup_thread", lambda: None)
    monkeypatch.setattr(executor, "_sessions", {})
    monkeypatch.setattr(executor.threading, "Thread", Mock())
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "_by_task", {})
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "_home_by_task", {})

    def finish_slow_probe(info):
        assert info is stale
        monkeypatch.setattr(browser_session, "_backend_dead", lambda observed: observed is stale)
        successor = browser_session.get_session_info(key)
        executor._sessions[key] = executor.Session(object(), process, "home", "/workspace", "endpoint", successor)
        SUPERVISOR_REGISTRY._by_task[key] = supervisor
        return True

    monkeypatch.setattr(browser_session, "_backend_dead", finish_slow_probe)
    result = browser_session.get_session_info(key)
    assert executor._sessions[key].browser is result
    assert browser_session._active_sessions[key] is result
    assert key in browser_session._session_last_activity
    assert SUPERVISOR_REGISTRY.get(key) is supervisor
    supervisor.stop.assert_not_called()
    process.stdin.close.assert_not_called()


def test_executor_response_preserves_task_vault_target(monkeypatch):
    from tools import browser_session, browser_supervisor, interrupt
    from tools.bot_desktop import runtime
    process = Mock()
    forwarded = 'ws://127.0.0.1:10002/devtools/browser/replacement'
    current = executor.Session(object(), process, 'home', '/workspace', 'endpoint', {'cdp_url': forwarded})
    current.responses.put(json.dumps({'returncode': 0, 'stdout': 'remote output', 'stderr': '', 'target_id': 'owned-tab'}).encode())
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', lambda *args: current)
    monkeypatch.setattr(executor, '_sessions', {'named-sidecar-key': current})
    monkeypatch.setattr(browser_session, 'run_fenced_dispatch', lambda fn: fn(lambda: None))
    monkeypatch.setattr(interrupt, 'is_interrupted', lambda: False)
    monkeypatch.setattr(runtime, 'touch_activity', lambda: None)
    supervisor = Mock()
    registry = browser_supervisor.SUPERVISOR_REGISTRY
    monkeypatch.setattr(registry, '_by_task', {'task': supervisor})
    monkeypatch.setattr(registry, '_home_by_task', {})
    attach = Mock(return_value=supervisor)
    monkeypatch.setattr(registry, 'get_or_start', attach)
    route_key = native._backend_cache_key('task', '')
    monkeypatch.setattr(native, '_real_profile_fallback', {route_key: 'real profile unavailable; using fresh browser'})
    monkeypatch.setattr(native, '_real_profile_note_delivered', set())
    stale = {'BU_CDP_WS': 'ws://127.0.0.1:10001/devtools/browser/old',
             native._EXECUTOR_BROWSER_SENTINEL: 'named-sidecar-key'}
    monkeypatch.setattr(native, '_base_subprocess_env', dict)
    monkeypatch.setattr(native, '_route_exec_current', lambda *args: (stale, None, False, 0))
    monkeypatch.setattr(native, '_needs_gateway_harness', lambda *args: False)
    result = native.browser_exec('print(1)', task_id='task')
    assert json.loads(result)['output'] == 'remote output'
    assert 'using fresh browser' in json.loads(result)['real_profile_unavailable']
    assert [call.args for call in supervisor.set_driven_target.call_args_list] == [(None,), ('owned-tab',)]
    assert attach.call_args.kwargs['cdp_url'] == forwarded
    assert attach.call_args.kwargs['task_id'] == 'task'
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    lifecycle.release_session_backend('named-sidecar-key', current.browser)
    assert registry.get('task') is None


@pytest.mark.parametrize('pause', ['before-lookup', 'before-publication'])
def test_retired_vault_attach_cannot_displace_the_replacement(monkeypatch, pause):
    from tools import browser_supervisor

    key, task = 'sidecar', 'vault-task'
    old_url = 'ws://127.0.0.1:10001/devtools/browser/old'
    new_url = 'ws://127.0.0.1:10002/devtools/browser/new'
    old = executor.Session(object(), Mock(), 'home', '/workspace', 'old', {})
    replacement = executor.Session(object(), Mock(), 'home', '/workspace', 'new', {})
    monkeypatch.setattr(executor, '_sessions', {key: old})
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    registry = browser_supervisor._SupervisorRegistry()
    monkeypatch.setattr(browser_supervisor, 'SUPERVISOR_REGISTRY', registry)
    started = {}

    def replace():
        executor.close(owner=old)
        executor._sessions[key] = replacement

    def make_supervisor(*, cdp_url, **_config):
        supervisor = Mock(cdp_url=cdp_url)
        started[cdp_url] = supervisor
        if pause == 'before-publication' and cdp_url == old_url:
            supervisor.start.side_effect = lambda **_kwargs: replace()
        return supervisor

    monkeypatch.setattr(browser_supervisor, 'CDPSupervisor', make_supervisor)
    native_attach = native._attach_vault_supervisor

    def attach(cdp_url, task_id, **kwargs):
        if pause == 'before-lookup' and cdp_url == old_url:
            replace()
        return native_attach(cdp_url, task_id, **kwargs)

    monkeypatch.setattr(native, '_attach_vault_supervisor', attach)
    with pytest.raises(RuntimeError, match='closed before dispatch'):
        with executor._attach_task_supervisor(old, key, old_url, task, lambda: None):
            pytest.fail('retired executor still admitted a command')
    with executor._attach_task_supervisor(replacement, key, new_url, task, lambda: None) as winner:
        assert winner is started[new_url]
    assert registry.get(task) is winner and replacement.supervisors[task] is winner
    winner.stop.assert_not_called()
    if pause == 'before-publication':
        started[old_url].stop.assert_called_once()
    else:
        assert old_url not in started


@pytest.mark.parametrize("phase", ["attaching", "running"])
def test_task_rejects_other_named_endpoint_until_active_command_finishes(monkeypatch, phase):
    from tools import browser_session, browser_supervisor, interrupt
    from tools.bot_desktop import runtime

    first_url = "ws://127.0.0.1:10001/devtools/browser/first"
    second_url = "ws://127.0.0.1:10002/devtools/browser/second"
    first = executor.Session(object(), Mock(), "home", "/workspace", "first", {"cdp_url": first_url})
    second = executor.Session(object(), Mock(), "home", "/workspace", "second", {"cdp_url": second_url})
    for session in (first, second):
        session.responses.put(json.dumps({"returncode": 0, "stdout": "done"}).encode())
    sessions = {"named-first": first, "named-second": second}
    monkeypatch.setattr(executor, "_sessions", sessions)
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, "_session", sessions.__getitem__)
    monkeypatch.setattr(browser_session, "run_fenced_dispatch", lambda fn: fn(lambda: None))
    monkeypatch.setattr(runtime, "touch_activity", lambda: None)
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: False)
    registry = browser_supervisor._SupervisorRegistry()
    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", registry)
    started = {}
    competing = []

    def run_second():
        return json.loads(executor.execute("named-second", "print(2)", task_id="shared-task"))

    def overlap():
        competing.append(run_second())
        second.process.stdin.write.assert_not_called()

    def make_supervisor(*, cdp_url, **_config):
        supervisor = Mock(cdp_url=cdp_url)
        started[cdp_url] = supervisor
        if cdp_url == first_url and phase == "attaching":
            supervisor.start.side_effect = lambda **_kwargs: overlap()
        return supervisor

    if phase == "running":
        first.process.stdin.write.side_effect = lambda _data: overlap()
    monkeypatch.setattr(browser_supervisor, "CDPSupervisor", make_supervisor)
    assert json.loads(executor.execute("named-first", "print(1)", task_id="shared-task"))["success"]
    assert "different endpoint" in competing[0]["error"]
    assert registry.get("shared-task") is started[first_url]
    assert executor._sessions == {"named-first": first, "named-second": second}
    started[first_url].stop.assert_not_called()

    assert run_second()["success"]  # a different endpoint can be selected sequentially
    assert registry.get("shared-task") is started[second_url]
    started[first_url].stop.assert_called_once()
    started[second_url].stop.assert_not_called()


def test_failed_old_call_cannot_close_a_successor_transport(monkeypatch):
    from tools import browser_session, interrupt
    from tools.bot_desktop import runtime
    browser = {}
    old = executor.Session(object(), Mock(), 'home', '/workspace', 'endpoint', browser)
    successor = executor.Session(object(), Mock(), 'home', '/workspace', 'endpoint', browser)
    old.responses.put(None)
    monkeypatch.setattr(executor, '_sessions', {'key': successor})
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', lambda *args: old)
    monkeypatch.setattr(browser_session, 'run_fenced_dispatch', lambda fn: fn(lambda: None))
    monkeypatch.setattr(interrupt, 'is_interrupted', lambda: False)
    monkeypatch.setattr(runtime, 'touch_activity', lambda: None)
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    result = executor.execute('key', 'print(1)')
    assert 'outcome may be unknown' in json.loads(result)['error']
    successor.process.stdin.close.assert_not_called()
    assert executor._sessions['key'] is successor


def test_executor_route_obeys_real_human_lease_without_acquiring_transport(monkeypatch):
    from tools.bot_desktop import lease
    session = Mock(side_effect=AssertionError("human-held browser must not acquire an executor"))
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, "_session", session)
    lease.acquire("native-executor-test")
    try:
        result = json.loads(executor.execute("key", "raise AssertionError('model ran')"))
        assert result["code"] == "human_has_control"
        session.assert_not_called()
    finally:
        lease.release("native-executor-test")


@pytest.mark.parametrize('change', ['interrupt', 'takeover-and-return'])
@pytest.mark.parametrize('wait', ['setup', 'owner-lock'])
def test_queued_call_rechecks_admission_without_retiring_transport(monkeypatch, change, wait):
    from contextlib import contextmanager
    from tools import interrupt
    from tools.bot_desktop import lease, runtime

    interrupted = False
    current = executor.Session(object(), Mock(), 'home', '/workspace', 'endpoint', {})
    monkeypatch.setattr(executor, '_sessions', {'key': current})
    monkeypatch.setattr(interrupt, 'is_interrupted', lambda: interrupted)
    monkeypatch.setattr(runtime, 'touch_activity', lambda: None)
    monkeypatch.setattr(runtime, 'published_env', lambda: {'DISPLAY': ':20'})
    current.responses.put(json.dumps({'returncode': 0, 'stdout': 'still usable', 'stderr': ''}))

    def revoke():
        nonlocal interrupted
        if change == 'interrupt':
            interrupted = True
        else:
            lease.acquire('queued-executor-test')
            lease.release('queued-executor-test')

    @contextmanager
    def lock():
        if wait == 'owner-lock':
            revoke()
        yield

    def open_session(*_args):
        if wait == 'setup':
            revoke()
        return current

    current.lock = lock()
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', open_session)
    result = json.loads(executor.execute('key', 'print(1)'))
    assert ('interrupted before launch' in result.get('error', '') if change == 'interrupt'
            else result.get('code') == 'human_has_control')
    current.process.stdin.write.assert_not_called()
    current.process.stdin.close.assert_not_called()
    assert executor._sessions['key'] is current

    import threading
    current.lock = threading.Lock()
    interrupted = False
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', lambda *_args: current)
    assert json.loads(executor.execute('key', 'print(2)'))['output'] == 'still usable'


def test_failed_buffered_pipe_close_still_reaps_both_owners(monkeypatch):
    import subprocess
    import sys
    from agent import memory_provider

    reaper = Mock()
    monkeypatch.setattr(memory_provider, 'spawn_context_thread', reaper)
    with subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE) as failed:
        failed.wait(timeout=10)
        failed.stdin.write(b'buffered request')
        with pytest.raises(BrokenPipeError):
            failed.stdin.flush()
        other = Mock()
        monkeypatch.setattr(executor, '_sessions', {
            'failed': executor.Session(object(), failed, 'home', '/workspace', 'endpoint', {}),
            'other': executor.Session(object(), other, 'home', '/workspace', 'endpoint', {}),
        })
        executor.close()
        assert not executor._sessions
        assert [call.kwargs['args'][0] for call in reaper.call_args_list] == [failed, other]
        other.stdin.close.assert_called_once()


def test_same_named_transport_preserves_separate_task_workspaces(monkeypatch):
    from tools.bot_desktop import runtime

    current = executor.Session(object(), Mock(), 'home', '/mounted workspace', 'endpoint', {})
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', lambda *_args: current)
    monkeypatch.setattr(runtime, 'touch_activity', lambda: None)
    workspaces = []
    for task in ('one', 'two', 'one'):
        current.responses.put(json.dumps({'returncode': 0, 'stdout': '', 'stderr': ''}))
        result = json.loads(executor.execute('same-key', 'print(1)', session='same', task_id=task))
        request = json.loads(current.process.stdin.write.call_args.args[0])
        assert request['workspace'] == result['workspace']
        assert result['downloads'] == '/mounted workspace/downloads'
        workspaces.append(result['workspace'])
    assert workspaces[0] == workspaces[2] != workspaces[1]


def test_availability_follows_active_executor_without_provisioning(monkeypatch):
    from hermes_cli.config import _validate_config_key
    from tools.bot_desktop import placement, sandbox_host
    from tools import browser_lightpanda, browser_session_cdp, browser_session_cloud
    from tools.environments import streams

    from types import SimpleNamespace

    assert _validate_config_key('browser.executor_python') == (True, None)
    assert _validate_config_key('browser.executor_workspace') == (True, None)

    env = SimpleNamespace()
    lookup = Mock(return_value=env)
    probe = Mock(return_value=Mock(returncode=0, stdout=b'harness-ready\n'))
    monkeypatch.setattr(native, '_has_cdp_env', lambda env: False)
    monkeypatch.setattr(browser_session_cdp, 'get_cdp_override_raw', lambda: None)
    monkeypatch.setattr(browser_session_cloud, 'get_cloud_provider', lambda: None)
    monkeypatch.setattr(browser_session_cloud, 'is_headed_mode', lambda: False)
    monkeypatch.setattr(browser_lightpanda, 'using_lightpanda_engine', lambda: False)
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(placement.TERMINAL, 'ssh'))
    monkeypatch.setattr(placement, 'terminal_environment', lookup)
    monkeypatch.setattr(sandbox_host, 'desktop_home', lambda env, **_kw: '/executor/home')
    monkeypatch.setattr(sandbox_host, '_user_for', lambda env, **_kw: 'runner')
    monkeypatch.setattr(executor, '_executor_python', lambda: '/executor/python')
    monkeypatch.setattr(streams, 'run_in', probe)
    gateway = Mock(return_value=None)
    monkeypatch.setattr(native, '_find_cli', gateway)
    assert native.browser_exec_available()
    lookup.assert_called_once_with(create=False)
    assert probe.call_args.args[0] is env
    assert '/executor/python' in probe.call_args.args[1]
    assert probe.call_args.kwargs['user'] == 'runner'
    assert probe.call_args.kwargs['timeout'] == 4.0  # remaining discovery budget, capped at probe_timeout
    assert native.browser_exec_available()
    probe.return_value.stdout = b'harness-missing\n'
    assert not native.browser_exec_available()
    lookup.return_value = None
    assert native.browser_exec_available()
    assert gateway.call_count == 4
    monkeypatch.setattr(browser_session_cdp, 'get_cdp_override_raw', lambda: 'http://fixture.test')
    assert not native.browser_exec_available()
    assert lookup.call_count == 4
    monkeypatch.setattr(browser_session_cdp, 'get_cdp_override_raw', lambda: None)
    monkeypatch.setattr(browser_lightpanda, 'using_lightpanda_engine', lambda: True)
    gateway.return_value = ['python', '-m', 'browser_harness.run']
    assert native.browser_exec_available()
    assert lookup.call_count == 4


def test_executor_cancellation_is_not_replayed_and_preserves_another_owner(monkeypatch):
    from tools import browser_session, interrupt
    current = executor.Session(object(), Mock(), 'home', '/workspace', 'endpoint', {})
    other = executor.Session(object(), Mock(), 'home', '/workspace', 'endpoint', {})
    monkeypatch.setattr(executor, '_sessions', {'key': current, 'other': other})
    acquire = Mock(return_value=current)
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', acquire)
    monkeypatch.setattr(browser_session, 'run_fenced_dispatch', lambda fn: fn(lambda: None))
    # Interrupt after the write is flushed, while the response is pending.
    monkeypatch.setattr(interrupt, 'is_interrupted', lambda: current.process.stdin.flush.called)
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    result = json.loads(executor.execute('key', 'print(1)'))
    assert 'Do not replay automatically' in result['error']
    acquire.assert_called_once()
    current.process.stdin.write.assert_called_once()
    current.process.stdin.close.assert_called_once()
    other.process.stdin.close.assert_not_called()
    assert executor._sessions == {'other': other}


@pytest.mark.parametrize('failure', [False, True])
def test_remote_screenshot_is_fetched_and_partial_gateway_copy_is_removed(tmp_path, monkeypatch, failure):
    from tools import browser_session, interrupt
    from tools.bot_desktop import runtime
    env = Mock()
    def fetch(remote, target, **kwargs):
        assert remote == '/executor/capture.png'
        target.write_bytes(b'fixture raster bytes')
        if failure:
            raise OSError('fixture transfer failed')
    env.fetch_file.side_effect = fetch
    current = executor.Session(env, Mock(), 'home', '/workspace', 'endpoint', {})
    current.responses.put(json.dumps({'returncode': 0, 'stdout': '', 'stderr': '',
        'screenshot': '/executor/capture.png'}).encode())
    monkeypatch.setattr(executor, '_sessions', {'key': current})
    monkeypatch.setattr(executor, "_validated_endpoint", Mock(), raising=False)
    monkeypatch.setattr(executor, '_session', lambda *args: current)
    monkeypatch.setattr(browser_session, 'run_fenced_dispatch', lambda fn: fn(lambda: None))
    monkeypatch.setattr(interrupt, 'is_interrupted', lambda: False)
    monkeypatch.setattr(runtime, 'touch_activity', lambda: None)
    monkeypatch.setattr(native, '_native_screenshot_result', Mock(return_value=None))
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    result = json.loads(executor.execute('key', 'print(capture_screenshot())'))
    target = env.fetch_file.call_args.args[1]
    if failure:
        assert 'fixture transfer failed' in result['error']
        assert not target.exists()
        current.process.stdin.close.assert_called_once()
    else:
        assert result['screenshot_path'] == str(target)
        assert target.read_bytes() == b'fixture raster bytes'
        native._native_screenshot_result.assert_called_once()


@pytest.mark.parametrize('configured_workspace', ['', '/persistent volume/browser-use'])
def test_executor_stream_uses_selected_owner_and_sanitized_environment(monkeypatch, configured_workspace):
    from tools.bot_desktop import runtime
    monkeypatch.setattr(runtime, "owns_tool_sandbox", lambda env: True)
    from types import SimpleNamespace
    from hermes_cli import process_identity
    from tools import browser_session, browser_session_lifecycle, browser_session_sandbox, browser_session_cloud
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    env = SimpleNamespace(cwd='/executor/exact workspace')
    browser = {'features': {'sandbox': True}, 'launcher_key': 'selected-owner',
               'cdp_url': 'ws://127.0.0.1:51234/devtools/browser/exact'}
    monkeypatch.setattr(executor, '_sessions', {})
    monkeypatch.setattr(browser_session, 'get_session_info', lambda key: browser)
    monkeypatch.setattr(browser_session_sandbox, '_sandbox', lambda: (env, {'DISPLAY': ':20', 'PRIVATE_GATEWAY_VALUE': 'secret'}))
    monkeypatch.setattr(browser_session_sandbox, '_shared_key', lambda *args: 'selected-owner')
    monkeypatch.setattr(browser_session_sandbox, '_live_devtools', lambda *args: (1234, '/devtools/browser/exact'))
    monkeypatch.setattr(sandbox_host, '_user_for', lambda selected: 'desktop-user')
    monkeypatch.setattr(sandbox_host, 'browser_profile_dir', lambda selected: '/executor/profile')
    monkeypatch.setattr(sandbox_host, 'desktop_home', lambda selected: '/executor/home')
    monkeypatch.setattr(streams, 'sandbox_temp_dir', lambda selected: '/executor/tmp')
    monkeypatch.setattr(streams, 'forward_port', lambda *args, **kwargs: 51234)
    monkeypatch.setattr(browser_session_cloud, 'browser_section', lambda: {
        'executor_python': '/executor/python', 'executor_workspace': configured_workspace})
    command = Mock(return_value=['fixture-ssh'])
    monkeypatch.setattr(streams, 'remote_command', command)
    process = Mock(pid=123)
    monkeypatch.setattr(executor.subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setattr(process_identity, 'own_process_groups', Mock())
    monkeypatch.setattr(executor.threading, 'Thread', Mock())
    current = executor._session('key')
    selected, argv = command.call_args.args
    assert selected is env and command.call_args.kwargs['user'] == 'desktop-user'
    assert argv[:2] == ['env', '-i']
    assert any(arg.startswith('BH_AGENT_WORKSPACE=/executor/tmp/bh/') for arg in argv)
    assert 'HOME=/executor/home' in argv and 'DISPLAY=:20' in argv
    assert not any('PRIVATE_GATEWAY_VALUE' in arg for arg in argv)
    assert '/executor/python' in argv
    expected = configured_workspace or '/executor/home/.hermes/browser-use'
    assert current.browser is browser and current.workspace_root == expected
    assert f'BH_DOWNLOAD_DIR={expected}/downloads' in argv

    # A different browser owner after a terminal cd must keep the same root.
    env.cwd = '/unrelated cwd after terminal command'
    successor = executor._session('successor')
    assert successor.workspace_root == current.workspace_root

    retire = Mock()
    monkeypatch.setattr(browser_session_lifecycle, 'cleanup_single_session', retire)
    command.reset_mock()
    observations = [
        (None, False),
        (browser_session_sandbox.GONE, True),
        ((1234, '/devtools/browser/replaced'), True),
    ]
    for observed, expected_retirement in observations:
        monkeypatch.setattr(browser_session_sandbox, '_live_devtools', lambda *args: observed)
        retire.reset_mock()
        with pytest.raises(RuntimeError):
            executor._validated_endpoint('successor', env, browser, successor.endpoint)
        if expected_retirement:
            retire.assert_called_once_with('successor', expected=browser)
        else:
            retire.assert_not_called()
        successor.process.stdin.write.assert_not_called()
        command.assert_not_called()



@pytest.mark.parametrize("pause", ["runner lock", "supervisor admission"])
def test_dispatch_rechecks_the_endpoint_after_waiting(monkeypatch, pause):
    from contextlib import contextmanager
    from tools import browser_session, interrupt
    from tools.bot_desktop import runtime

    endpoint = "ws://127.0.0.1:1234/devtools/browser/old"
    current = executor.Session(object(), Mock(), "home", "/workspace", endpoint, {"cdp_url": endpoint})
    current.responses.put(json.dumps({"returncode": 0, "stdout": "old response"}).encode())
    stale = False
    attached = False

    def verify(key, env, info, expected):
        assert (key, env, info, expected) == ("key", current.env, current.browser, endpoint)
        if stale:
            raise RuntimeError("Executor Chromium changed during setup; retry the command")
        return endpoint

    @contextmanager
    def runner_lock():
        nonlocal stale
        if pause == "runner lock":
            stale = True
        yield

    @contextmanager
    def admit_supervisor(*args):
        nonlocal stale, attached
        attached = True
        if pause == "supervisor admission":
            stale = True
        yield None

    current.lock = runner_lock()
    monkeypatch.setattr(executor, "_session", lambda _key: current)
    monkeypatch.setattr(executor, "_validated_endpoint", verify, raising=False)
    monkeypatch.setattr(executor, "_attach_task_supervisor", admit_supervisor)
    monkeypatch.setattr(browser_session, "run_fenced_dispatch", lambda fn: fn(lambda: None))
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: False)
    monkeypatch.setattr(runtime, "touch_activity", lambda: None)

    result = json.loads(executor.execute("key", "print(1)"))
    assert "changed during setup" in result.get("error", ""), result
    assert attached == (pause == "supervisor admission")
    current.process.stdin.write.assert_not_called()


def test_cua_discovery_shares_one_absolute_budget_with_env_creation(monkeypatch):
    import subprocess
    import time as _time
    from types import SimpleNamespace
    from tools.bot_desktop import placement, sandbox_host
    from tools.environments import streams

    env = SimpleNamespace()
    clock = {'offset': 0.0}
    real = _time.monotonic
    monkeypatch.setattr(sandbox_host.time, 'monotonic', lambda: real() + clock['offset'])
    monkeypatch.setattr(placement, 'resolve', lambda: SimpleNamespace(where=placement.TERMINAL))
    monkeypatch.setattr(
        placement, 'terminal_environment',
        lambda create=True: (clock.__setitem__('offset', 5.0), env)[1],
    )
    monkeypatch.setattr(sandbox_host, '_user_for', lambda _env, **_kw: None)
    seen = []

    def run_in(_env, argv, **kwargs):
        seen.append(kwargs['timeout'])
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    monkeypatch.setattr(streams, 'run_in', run_in)
    assert sandbox_host.ready_executor(extra=("cua-driver",), probe_timeout=4.0) is env
    assert seen and 0.25 <= seen[-1] < 4.0  # env creation already consumed part of the shared budget


def test_discovery_probes_share_one_budget_under_registry_ceiling(monkeypatch):
    import subprocess
    import time as _time
    from types import SimpleNamespace
    from tools import browser_session_cloud
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    env = SimpleNamespace(_bd_desktop_user=None, _bd_desktop_home="/home/runner")
    seen = []

    def run_in(_env, argv, **kwargs):
        seen.append(kwargs['timeout'])
        return subprocess.CompletedProcess(argv, 0, b'harness-ready\n', b'')

    monkeypatch.setattr(streams, 'run_in', run_in)
    monkeypatch.setattr(browser_session_cloud, 'browser_section', lambda: {'executor_python': 'python3'})
    deadline = _time.monotonic() + 0.5
    assert executor.available(env=env, probe_timeout=4.0, deadline=deadline)
    assert seen and all(timeout <= 0.5 for timeout in seen)
    # A cold sequential discovery keeps its whole budget under registry's 10s check_fn ceiling.
    assert sandbox_host.probe_budget(_time.monotonic() + 8.0, 4.0) <= 4.0
    assert sandbox_host.probe_budget(_time.monotonic() - 1.0, 4.0) == 0.25


def test_desktop_user_probe_recovers_after_client_error(monkeypatch):
    from types import SimpleNamespace
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    env = SimpleNamespace()
    responses = [Mock(returncode=1, stdout=b''), Mock(returncode=0, stdout=b'desktop-user\n')]
    monkeypatch.setattr(streams, 'run_in', Mock(side_effect=responses))
    with pytest.raises(RuntimeError, match='inspect the desktop account'):
        sandbox_host._user_for(env)
    assert sandbox_host._user_for(env) == sandbox_host.DESKTOP_USER


@pytest.mark.platforms("posix")
def test_harness_probe_keeps_inherited_python_packages_out_of_the_runner(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace
    from tools.environments import streams

    injections = tmp_path / "extra-packages"
    package = injections / "browser_harness"
    package.mkdir(parents=True)
    for module in ("__init__", "run", "admin"):
        (package / (module + ".py")).touch()
    python = tmp_path / "python-no-site"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    python.chmod(0o700)
    monkeypatch.setattr(executor, "_executor_python", lambda: str(python))
    monkeypatch.setenv("PYTHONPATH", str(injections))
    # The package is usable with the executor's inherited environment.
    subprocess.run([str(python), "-c", "import browser_harness.run"], check=True, env=os.environ)

    def run_in(_env, argv, **kwargs):
        return subprocess.run(argv, capture_output=True, timeout=kwargs["timeout"], check=False)

    monkeypatch.setattr(streams, "run_in", run_in)
    env = SimpleNamespace(_bd_desktop_user=None, _bd_desktop_home=str(tmp_path))
    assert not executor.probe_harness(env)


def test_stock_sandbox_without_remote_harness_falls_back_to_gateway_cli(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from tools import browser_lightpanda, browser_session, browser_session_cdp, browser_session_cloud
    from tools.bot_desktop import placement, runtime

    env = SimpleNamespace(_bd_desktop_user=None, _bd_desktop_home="/home/desktop")
    monkeypatch.setattr(native, "_has_cdp_env", lambda _env: False)
    monkeypatch.setattr(browser_session_cdp, "get_cdp_override_raw", lambda: None)
    monkeypatch.setattr(browser_session_cloud, "get_cloud_provider", lambda: None)
    monkeypatch.setattr(browser_session_cloud, "is_headed_mode", lambda: False)
    monkeypatch.setattr(browser_lightpanda, "using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, "docker"))
    monkeypatch.setattr(placement, "executor_only", lambda: False, raising=False)
    monkeypatch.setattr(placement, "terminal_environment", lambda create=False: env)
    monkeypatch.setattr(runtime, "_owned_sandbox_env", lambda reattach=False: env)
    from tools.environments import streams
    probe = Mock(return_value=Mock(returncode=1, stdout=b''))
    monkeypatch.setattr(streams, "run_in", probe)
    monkeypatch.setattr(native, "_find_cli", lambda: ["/gateway/venv/bin/browser-harness"])
    assert native.browser_exec_available()

    session = {
        "session_name": "hermes-sandbox-stock",
        "cdp_url": "ws://127.0.0.1:42222/devtools/browser/stock",
        "features": {"headed": True, "sandbox": True},
    }
    monkeypatch.setattr(browser_session, "get_session_info", lambda key, force_local=False: session)
    monkeypatch.setattr(browser_session, "run_fenced", lambda _info, fn: fn())
    monkeypatch.setattr(native, "_supervise_browser_command",
                        lambda *_a, **_kw: __import__("contextlib").nullcontext(None))
    monkeypatch.setattr(native, "_driven_daemons", {})
    monkeypatch.setattr(native, "_own_harness_daemon_until_owned", lambda *_a, **_kw: None)
    monkeypatch.setattr(native, "_own_harness_daemon", lambda *_a, **_kw: None)
    monkeypatch.setattr(native, "_record_driven_tab", lambda *_a, **_kw: None)
    monkeypatch.setattr(native, "_reload_if_republished", lambda *_a, **_kw: None)
    executor_called = Mock(side_effect=AssertionError("executor runner should not be used"))
    monkeypatch.setattr(executor, "execute", executor_called)
    seen = {}

    def fake_run(cmd, code, child_env, timeout):
        seen["cmd"] = cmd[0]
        seen["cdp"] = child_env.get("BU_CDP_WS")
        seen["sentinel"] = child_env.get(native._EXECUTOR_BROWSER_SENTINEL)
        return subprocess.CompletedProcess(cmd, 0, "stock-ok\n", "")

    monkeypatch.setattr(native, "_run_cli_killing_process_group", fake_run)
    unavailable = json.loads(native.browser_exec("print(1)", task_id="stock"))
    assert "Check the executor and retry" in unavailable["error"]
    assert not seen
    probe.return_value.returncode = 0
    probe.return_value.stdout = b'harness-missing\n'
    out = json.loads(native.browser_exec("print(1)", task_id="stock"))
    assert out["success"] and out["output"].strip() == "stock-ok"
    assert seen == {
        "cmd": "/gateway/venv/bin/browser-harness",
        "cdp": "ws://127.0.0.1:42222/devtools/browser/stock",
        "sentinel": None,
    }
    executor_called.assert_not_called()


def test_cold_session_startup_does_not_stall_other_keys_or_supervisor_lookup(monkeypatch):
    import threading
    from agent import memory_provider
    from hermes_constants import hermes_home_key
    from tools import browser_session, browser_session_sandbox, browser_supervisor, interrupt
    from tools.bot_desktop import placement, runtime

    entered = threading.Event()
    release = threading.Event()
    warm_proc = Mock()
    warm_proc.poll.return_value = None
    sup_b = Mock()
    warm = executor.Session(object(), warm_proc, hermes_home_key(), "/workspace", "ws://warm",
                            {"features": {"sandbox": True}, "cdp_url": "ws://warm"})
    warm.supervisors["task-b"] = sup_b
    warm.responses.put(json.dumps({"returncode": 0, "stdout": "warm-ok", "stderr": ""}).encode())
    to_close = executor.Session(object(), Mock(), hermes_home_key(), "/workspace", "ws://close",
                                {"features": {"sandbox": True}, "cdp_url": "ws://close"})
    registry = browser_supervisor._SupervisorRegistry()
    registry._by_task["task-b"] = sup_b
    monkeypatch.setattr(browser_supervisor, "SUPERVISOR_REGISTRY", registry)
    monkeypatch.setattr(memory_provider, "spawn_context_thread", Mock())

    monkeypatch.setattr(executor, "_sessions", {"key-b": warm, "key-c": to_close})
    monkeypatch.setattr(executor, "_key_locks", {})
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, "ssh"))
    monkeypatch.setattr(runtime, "owns_tool_sandbox", lambda _env: True, raising=False)
    monkeypatch.setattr(runtime, "touch_activity", lambda: None)
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: False)
    monkeypatch.setattr(browser_session, "run_fenced_dispatch", lambda fn: fn(lambda: None))
    monkeypatch.setattr(executor, "_attach_task_supervisor",
                        lambda *_a, **_kw: __import__("contextlib").nullcontext(None))

    def get_info(key):
        if key == "key-a":
            return {"features": {"sandbox": True}, "cdp_url": "ws://cold"}
        return warm.browser

    def slow_sandbox():
        if threading.current_thread() is t:
            entered.set()
            assert release.wait(timeout=5.0)
            raise RuntimeError("abort cold startup after concurrency check")
        return warm.env, {"DISPLAY": ":20"}

    monkeypatch.setattr(browser_session, "get_session_info", get_info)
    monkeypatch.setattr(browser_session_sandbox, "_sandbox", slow_sandbox)
    monkeypatch.setattr(executor, "_validated_endpoint", lambda _k, _e, info, expected=None: expected or info["cdp_url"])

    cold_errors = []
    t = threading.Thread(target=lambda: cold_errors.append(json.loads(executor.execute("key-a", "print(1)"))))
    t.start()
    try:
        assert entered.wait(timeout=5.0)
        assert executor.task_supervisor("task-b") is sup_b
        executor.close(cache_key="key-c")
        assert "key-c" not in executor._sessions
        warm_out = json.loads(executor.execute("key-b", "print(2)", task_id="task-b"))
        assert warm_out["success"] and warm_out["output"].strip() == "warm-ok"
    finally:
        release.set()
        t.join(timeout=5.0)
    assert cold_errors and "abort cold startup" in cold_errors[0].get("error", "")
