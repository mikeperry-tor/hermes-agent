"""Computer-use transport recovery keeps the selected desktop destination."""
from unittest.mock import AsyncMock, Mock
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import subprocess
import threading
import time

import pytest

from tools.bot_desktop import placement
from tools.computer_use import cua_backend, cua_backend_session


@pytest.mark.parametrize('where', [placement.TERMINAL, placement.REFUSED])
@pytest.mark.parametrize('mode', ['bounded', 'unrestricted'])
def test_remote_permissions_refused_before_constructing_local_daemon(monkeypatch, where, mode):
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(where, 'ssh'))
    daemon = Mock(side_effect=AssertionError('must not construct a local daemon'))
    monkeypatch.setattr(cua_backend, '_EmbeddedCuaDaemon', daemon)
    with pytest.raises(ValueError, match='standard permissions only'):
        cua_backend.CuaDriverBackend(mode)
    daemon.assert_not_called()


@pytest.mark.parametrize('mode', ['standard', 'bounded', 'unrestricted'])
def test_gateway_permissions_still_supported(monkeypatch, mode):
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(placement.GATEWAY, 'local'))
    daemon = Mock()
    monkeypatch.setattr(cua_backend, '_EmbeddedCuaDaemon', daemon)
    backend = cua_backend.CuaDriverBackend(mode)
    assert backend.permission_mode == mode
    assert daemon.call_count == (mode != 'standard')


@pytest.mark.parametrize('mode', ['bounded', 'unrestricted'])
def test_placement_change_during_start_does_not_launch_gateway_daemon(monkeypatch, mode):
    import pm

    where = placement.GATEWAY
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(where, 'ssh'))
    daemon = Mock()
    monkeypatch.setattr(cua_backend, '_EmbeddedCuaDaemon', lambda *args, **kwargs: daemon)
    monkeypatch.setattr(cua_backend, 'sandbox_mcp_invocation', lambda: ['fixture-transport'])
    backend = cua_backend.CuaDriverBackend(mode)

    def switch_placement(_extra):
        nonlocal where
        where = placement.TERMINAL

    monkeypatch.setattr(pm, 'ensure_import', switch_placement)
    with pytest.raises(ValueError, match='standard permissions only'):
        backend.start()
    daemon.start.assert_not_called()


def test_remote_cli_fallback_cannot_resolve_host_driver_after_placement_changes(monkeypatch):
    session = cua_backend_session._CuaDriverSession(Mock())
    session._sandbox_transport = True
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(placement.GATEWAY, 'local'))
    driver = Mock(side_effect=AssertionError('must not resolve host driver'))
    monkeypatch.setattr(cua_backend_session._driver, 'resolve_cua_driver_cmd', driver)
    with pytest.raises(RuntimeError, match='Gateway-local CLI fallback'):
        session._call_tool_via_cli('get_window_state', {}, 30)
    driver.assert_not_called()


def test_gateway_cli_fallback_remains_usable(monkeypatch):
    session = cua_backend_session._CuaDriverSession(Mock())
    monkeypatch.setattr(cua_backend_session._driver, 'resolve_cua_driver_cmd', lambda: 'cua-driver')
    monkeypatch.setattr(cua_backend, 'cua_driver_child_env', dict)
    monkeypatch.setattr(cua_backend_session, '_cli_run_json', lambda *args: {'ok': True})
    assert not session._call_tool_via_cli('list_apps', {}, 30)['isError']


@pytest.mark.parametrize('previous, replacement', [
    (True, placement.GATEWAY), (False, placement.TERMINAL),
])
def test_mcp_reconnect_refuses_changed_placement_before_start(monkeypatch, previous, replacement):
    from tools.bot_desktop import runtime

    session = cua_backend_session._CuaDriverSession(Mock())
    session._sandbox_transport = previous
    monkeypatch.setattr(placement, 'resolve', lambda: placement.Placement(replacement, 'ssh'))
    gateway_driver = Mock()
    start_screen = Mock()
    monkeypatch.setattr(cua_backend_session._driver, 'resolve_cua_driver_cmd', gateway_driver)
    monkeypatch.setattr(runtime, 'start', start_screen)
    with pytest.raises(RuntimeError, match='desktop placement changed'):
        asyncio.run(session._lifecycle_coro())
    gateway_driver.assert_not_called()
    start_screen.assert_not_called()
    assert session._sandbox_transport is previous


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [placement.TERMINAL, placement.REFUSED])
async def test_warm_gateway_driver_rechecks_placement_before_writing(monkeypatch, replacement):
    session = cua_backend_session._CuaDriverSession(Mock())
    session._sandbox_transport = False
    send = AsyncMock(return_value=object())
    session._session = Mock(call_tool=send)
    monkeypatch.setattr(cua_backend_session, "_extract_tool_result", lambda value: value)
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.GATEWAY, "local"))
    await session._call_tool_async("list_apps", {})
    send.assert_awaited_once()
    send.reset_mock()

    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(replacement, "ssh", "Policy unavailable"))
    with pytest.raises(cua_backend_session.DesktopChanged):
        await session._call_tool_async("list_apps", {})
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["executor", "launcher"])
async def test_warm_executor_driver_rejects_a_replaced_owner_on_the_same_display(monkeypatch, change):
    from tools.bot_desktop import runtime

    env = object()
    display = {"DISPLAY": ":20", "DBUS_SESSION_BUS_ADDRESS": "first bus"}
    session = cua_backend_session._CuaDriverSession(Mock())
    session._sandbox_transport = True
    session._sandbox_desktop = (env, display)
    send = AsyncMock()
    session._session = Mock(call_tool=send)
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, "ssh"))
    monkeypatch.setattr(runtime, "owns_tool_sandbox", lambda _env: True)
    successor = ((object(), display) if change == "executor"
                 else (env, {**display, "DBUS_SESSION_BUS_ADDRESS": "second bus"}))
    monkeypatch.setattr(runtime, "tool_sandbox", lambda *, reattach: successor)

    with pytest.raises(cua_backend_session.DesktopChanged, match="recorded desktop changed"):
        await session._call_tool_async("list_apps", {})
    with pytest.raises(cua_backend_session.DesktopChanged, match="recorded desktop changed"):
        await session._lifecycle_coro()
    send.assert_not_awaited()


@pytest.mark.parametrize("block_at", ["desktop", "lease"])
def test_timed_out_checks_cannot_send_a_driver_request(monkeypatch, block_at):
    bridge = cua_backend_session._AsyncBridge()
    session = cua_backend_session._CuaDriverSession(bridge)
    send = AsyncMock()
    session._session = Mock(call_tool=send)
    entered, finish, done = threading.Event(), threading.Event(), threading.Event()

    def blocked_check():
        entered.set()
        assert finish.wait(10)
        done.set()

    if block_at == "desktop":
        monkeypatch.setattr(session, "_assert_desktop", blocked_check)

    def call_driver():
        token = cua_backend_session.dispatch_fence.set(blocked_check if block_at == "lease" else None)
        try:
            return bridge.run(session._call_tool_async("list_apps", {}), 1.0)
        finally:
            cua_backend_session.dispatch_fence.reset(token)

    bridge.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(call_driver)
            try:
                assert entered.wait(10)
                with pytest.raises(TimeoutError):
                    call.result(timeout=10)
            finally:
                finish.set()
            assert done.wait(10)
            bridge.run(asyncio.sleep(0))
        send.assert_not_awaited()
    finally:
        finish.set()
        bridge.stop()


def test_gateway_cli_retry_requires_current_desktop_placement(monkeypatch):
    where = placement.GATEWAY
    session = cua_backend_session._CuaDriverSession(Mock())
    session._sandbox_transport = False
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(where, "ssh"))
    monkeypatch.setattr(cua_backend_session._driver, "resolve_cua_driver_cmd", lambda: "cua-driver")
    monkeypatch.setattr(cua_backend, "cua_driver_child_env", dict)
    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "temporary error"))
    monkeypatch.setattr(subprocess, "run", run)

    def change_placement(_seconds):
        nonlocal where
        where = placement.TERMINAL

    monkeypatch.setattr(time, "sleep", change_placement)
    with pytest.raises(cua_backend_session.DesktopChanged, match="placement changed"):
        session._call_tool_via_cli("list_apps", {}, 5)
    run.assert_called_once()
