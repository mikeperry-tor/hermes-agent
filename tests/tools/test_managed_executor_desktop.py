"""The managed desktop selection controls native browser and display entrypoints."""
import json
from unittest.mock import Mock

import pytest

from tools import browser_session, browser_session_cdp, browser_session_lifecycle
from tools import browser_session_sandbox, browser_use_cli, browser_use_executor
from tools.bot_desktop import install, placement, runtime


@pytest.fixture
def managed(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(root))
    document = root / "config.yaml"
    document.write_text("bot_desktop:\n  executor_only: true\n")
    return document


def test_executor_only_admits_only_terminal_placement(managed, monkeypatch):
    monkeypatch.setattr(placement, "_setting", lambda: placement.GATEWAY)
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    assert placement.resolve().where == placement.TERMINAL
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "local")
    assert placement.resolve().where == placement.REFUSED
    managed.write_text("{}\n")
    assert placement.resolve().where == placement.GATEWAY


@pytest.mark.parametrize("contents", ["null\n", "bot_desktop:\n  executor_only: no-thanks\n"])
def test_invalid_policy_refuses_browser_before_route_or_executor_setup(managed, monkeypatch, contents):
    managed.write_text(contents)
    dispatch = Mock()
    monkeypatch.setattr(browser_use_executor, "execute", dispatch)
    error = json.loads(browser_use_cli.browser_exec("print(1)"))["error"].lower()
    assert "managed" in error and "policy" in error
    assert placement.resolve().where == placement.REFUSED
    assert not browser_use_cli.browser_exec_available()
    dispatch.assert_not_called()


def test_browser_dispatch_never_consults_profile_routes(managed, monkeypatch):
    dispatch = Mock(return_value="executor-result")
    monkeypatch.setattr(browser_use_executor, "execute", dispatch)
    monkeypatch.setenv("BU_CDP_WS", "ws://not-the-executor.test:8020")
    monkeypatch.setenv("BROWSER_CDP_URL", "http://not-the-executor.test:8020")
    for name in ("_route_exec_current", "_base_subprocess_env", "_find_cli"):
        monkeypatch.setattr(browser_use_cli, name, Mock(side_effect=AssertionError(name)))

    assert browser_session_cdp.get_cdp_override_raw() == ""
    assert browser_use_cli.browser_exec("print(1)", session="task") == "executor-result"
    assert "requires a browser in the terminal executor" in json.loads(
        browser_use_cli.browser_exec("print(1)", local=True),
    )["error"]
    dispatch.assert_called_once()


def test_executor_only_discovery_checks_remote_dependencies_without_starting_screen(managed, monkeypatch):
    from types import SimpleNamespace
    from plugins.computer_use.cua import CuaDriverProvider
    from tools.bot_desktop import sandbox_host
    from tools.computer_use import cua_backend_driver
    from tools.environments import streams

    env = SimpleNamespace()
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    terminal = Mock(return_value=None)
    monkeypatch.setattr(placement, "terminal_environment", terminal)
    start = Mock(side_effect=AssertionError("discovery started the screen"))
    monkeypatch.setattr(runtime, "start", start)
    monkeypatch.setattr(cua_backend_driver, "cua_driver_binary_available",
                        Mock(side_effect=AssertionError("checked the gateway driver")))
    missing = Mock(side_effect=lambda _env, *, extra, **_kw: list(extra))
    monkeypatch.setattr(sandbox_host, "missing_binaries", missing)
    monkeypatch.setattr(sandbox_host, "desktop_home", lambda _env, **_kw: "/home/runner")
    monkeypatch.setattr(sandbox_host, "_user_for", lambda _env, **_kw: "runner")
    harness = Mock(return_value=SimpleNamespace(returncode=0, stdout=b'harness-missing\n'))
    monkeypatch.setattr(streams, "run_in", harness)
    provider = CuaDriverProvider()

    assert not provider.is_available() and not browser_use_cli.browser_exec_available()
    terminal.assert_called_with(create=True)
    terminal.return_value = env
    assert not provider.is_available() and not browser_use_cli.browser_exec_available()
    assert missing.call_args.kwargs["timeout"] == 4.0
    assert isinstance(missing.call_args.kwargs["deadline"], float)  # browser_exec_available shares one budget
    monkeypatch.setattr(browser_use_cli, "_find_cli", lambda: ["/gateway/venv/bin/browser-harness"])
    harness.return_value.stdout = b'harness-ready\n'
    assert not provider.is_available() and browser_use_cli.browser_exec_available()
    harness.return_value.returncode = 255
    assert not browser_use_cli.browser_exec_available()
    harness.return_value.returncode = 0
    assert browser_use_cli.browser_exec_available()
    assert harness.call_args.args[0] is env
    start.assert_not_called()


def test_executor_only_retains_native_website_policy(managed, monkeypatch):
    from tools import browser_url_policy

    monkeypatch.setattr(browser_url_policy, "check_website_access", lambda url: {
        "message": "Blocked by the selected website policy", "host": "example.invalid",
        "rule": "example.invalid", "source": "fixture",
    })
    command = Mock()
    monkeypatch.setattr(browser_use_executor, "execute", command)
    result = json.loads(browser_use_cli.browser_exec("goto_url('https://example.invalid')"))
    assert result["error"] == "Blocked by the selected website policy"
    command.assert_not_called()


def test_session_creation_refuses_cached_non_executor_and_keeps_native_ownership(managed, monkeypatch):
    old = {"features": {"cdp_override": True}, "cdp_url": "ws://other.test"}
    new = {"features": {"local": True, "sandbox": True}, "cdp_url": "ws://executor.test"}
    monkeypatch.setattr(browser_session, "_active_sessions", {"task": old})
    monkeypatch.setattr(browser_session, "_session_last_activity", {})
    monkeypatch.setattr(browser_session, "_session_owner_homes", {})
    monkeypatch.setattr(browser_session_lifecycle, "start_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_session_lifecycle, "release_session_backend", lambda *args: None)
    monkeypatch.setattr(browser_session, "_backend_dead", Mock(side_effect=AssertionError("old endpoint consulted")))
    monkeypatch.setattr(browser_session, "_create_session_for_key", Mock(side_effect=AssertionError("default route")))
    require_terminal = Mock(return_value=placement.TERMINAL)
    monkeypatch.setattr(runtime, "tool_placement", require_terminal)
    monkeypatch.setattr(browser_session_sandbox, "create_sandbox_session", lambda key: new)

    assert browser_session.get_session_info("task")["cdp_url"] == new["cdp_url"]
    require_terminal.assert_called_once_with(expected_sandbox=True)


def test_gateway_package_install_refuses_managed_executor(managed, monkeypatch):
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    provision = Mock()
    monkeypatch.setattr(install, "_run", provision)
    with pytest.raises(RuntimeError, match="executor image"):
        install.install_packages(ask_password=lambda: "", on_line=lambda line: None)
    provision.assert_not_called()


@pytest.mark.parametrize("invalid_policy", [False, True])
def test_current_policy_controls_existing_desktop_viewing(managed, monkeypatch, invalid_policy):
    import asyncio
    from PIL import ImageGrab
    from hermes_cli.web_routers.display import _open_rfb
    from tools.bot_desktop import sandbox_host, thumbnail
    from tui_gateway import server

    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    if invalid_policy:
        managed.write_text("bot_desktop: invalid\n")
    monkeypatch.setattr(runtime, "_launcher_pid", lambda: 42)
    monkeypatch.setattr(runtime, "_read", lambda path: "DISPLAY=:88")
    gateway_socket = Mock(side_effect=AssertionError("gateway socket consulted"))
    monkeypatch.setattr(runtime, "rfb_socket_path", gateway_socket)
    local_grab = Mock(side_effect=AssertionError("gateway framebuffer consulted"))
    monkeypatch.setattr(ImageGrab, "grab", local_grab)
    # A previous sandbox must also be inaccessible while its managed policy is invalid.
    monkeypatch.setattr(sandbox_host, "_read_marker", lambda: {"backend": "SSH"} if invalid_policy else {})
    monkeypatch.setattr(runtime, "sandbox_screen_running", lambda: invalid_policy)
    monkeypatch.setattr(runtime, "_owned_sandbox_env", lambda: None)
    monkeypatch.setattr(runtime, "_sandbox_env", lambda **kwargs: None)
    open_stream = Mock(side_effect=AssertionError("forbidden sandbox stream"))
    monkeypatch.setattr(runtime, "open_rfb_stream", open_stream)

    assert thumbnail.thumbnail_data_url() is None
    status = runtime.status()
    assert not status.running and status.display is None and status.install_command is None
    error = "validate the managed desktop policy" if invalid_policy else "not running"
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "display.observe", "params": {}})
    assert ("validate the managed desktop policy" if invalid_policy else "inspect") in response["error"]["message"]
    with pytest.raises(OSError, match=error):
        asyncio.run(_open_rfb(managed.parent))
    gateway_socket.assert_not_called()
    local_grab.assert_not_called()
    open_stream.assert_not_called()


def test_retire_gateway_display_after_enabling_executor_only(managed, monkeypatch):
    from tools.bot_desktop import sandbox_host

    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    monkeypatch.setattr(sandbox_host, "_read_marker", dict)
    monkeypatch.setattr(runtime, "is_supported_host", lambda: True)
    retire = Mock(return_value=True)
    monkeypatch.setattr(runtime, "_stop_locked", retire)
    assert runtime.stop()
    retire.assert_called_once()



@pytest.mark.asyncio
async def test_rfb_redeems_in_sandbox_after_local_registry_is_lost(managed, monkeypatch):
    import subprocess
    import sys
    from hermes_cli.web_routers.display import _open_rfb
    from tools.bot_desktop import sandbox_host

    from types import SimpleNamespace
    from tools import terminal_tool

    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    owner = ["executor.test", "runner", "2222", "/key", []]
    monkeypatch.setattr(sandbox_host, "_read_marker", lambda: {"backend": "SSHEnvironment", "ssh": owner})
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "ssh", "ssh_host": "executor.test", "ssh_user": "runner", "ssh_port": 2222,
        "ssh_key": "/key", "ssh_config": "",
    })
    env = SimpleNamespace(_bd_desktop_user=None)
    monkeypatch.setattr(sandbox_host, "_owner_identity", lambda env: {"backend": "SSHEnvironment", "ssh": owner})
    lookup = Mock(side_effect=lambda *, create: env if create else None)
    monkeypatch.setattr(runtime, "_sandbox_env", lookup)
    open_stream = Mock(side_effect=lambda *_: subprocess.Popen(
        [sys.executable, "-c", "print('RFB 003.008')"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    ))
    monkeypatch.setattr(sandbox_host, "open_rfb_stream", open_stream)

    reader, writer, relay = await _open_rfb(managed.parent)
    try:
        assert await reader.readline() == b"RFB 003.008\n"
        assert [call.kwargs["create"] for call in lookup.call_args_list] == [False, True]
        assert open_stream.call_args.args[0] is env
    finally:
        writer.close()
        await writer.wait_closed()
        relay.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["connection", "deadline"])
async def test_rfb_closes_cleanly_when_sandbox_reattachment_fails(managed, monkeypatch, error):
    from subprocess import TimeoutExpired
    from unittest.mock import AsyncMock
    from hermes_cli.web_routers import display
    from tools.environments.base import EnvironmentConnectionError

    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    failure = EnvironmentConnectionError("SSH unavailable") if error == "connection" else TimeoutExpired("fixture", 15)
    monkeypatch.setattr(runtime, "_owned_sandbox_env", Mock(side_effect=failure))
    ws = Mock(close=AsyncMock())
    await display._bridge(ws, {"hermes_home": str(managed.parent)})
    ws.close.assert_awaited_once_with(code=4001, reason="Bot Desktop is not running")


def test_external_browser_commands_refuse_before_probe_or_launch(managed, monkeypatch, capsys):
    from hermes_cli import browser_preview_signins, cli_browser_gateway, cli_commands_mixin
    from tools import browser_session_real_profile
    from tui_gateway import server

    no_probe = Mock(side_effect=AssertionError("external CDP probe"))
    monkeypatch.setattr(cli_browser_gateway, "_normalize_cdp_url", no_probe)
    monkeypatch.setattr(browser_session_real_profile, "_launch", no_probe)

    cli_commands_mixin._browser_connect(None, "https://example.invalid")
    assert "external CDP" in capsys.readouterr().out
    result = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "browser.manage",
                                    "params": {"action": "connect", "url": "https://example.invalid"}})
    assert "external CDP" in result["error"]["message"]
    assert "real profile" in browser_preview_signins.export_signins()["error"]
    no_probe.assert_not_called()


def test_companion_tools_use_only_current_executor_supervisor(managed, monkeypatch):
    from hermes_constants import hermes_home_key
    from tools import browser_dialog_tool, browser_vault_tool
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    old = Mock()
    supervisor = Mock(return_value=old)
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", supervisor)
    monkeypatch.setattr(browser_use_executor, "_sessions", {})
    monkeypatch.setattr(browser_session, "session_for_task", Mock(side_effect=AssertionError("old browser queried")))
    assert browser_vault_tool._ensure_supervisor("task") is None
    assert not json.loads(browser_dialog_tool.browser_dialog("dismiss", task_id="task"))["success"]
    old.respond_to_dialog.assert_not_called()

    current = Mock()
    current.respond_to_dialog.return_value = {"ok": True}
    process = Mock()
    process.poll.return_value = None
    supervisor.return_value = current
    record = browser_use_executor.Session(object(), process, hermes_home_key(), "", "", {})
    record.supervisors["task"] = current
    browser_use_executor._sessions["named-session"] = record
    monkeypatch.setattr(runtime, "owns_tool_sandbox", lambda env: env is record.env)
    assert browser_vault_tool._ensure_supervisor("task") is current
    assert not json.loads(browser_dialog_tool.browser_dialog("dismiss", task_id="task"))["success"]
    current.respond_to_dialog.assert_not_called()
    monkeypatch.setattr(runtime, "owns_tool_sandbox", lambda env: False)
    assert browser_vault_tool._ensure_supervisor("task") is None

    managed.write_text("bot_desktop: invalid\n")
    assert browser_vault_tool._ensure_supervisor("task") is None
    assert "managed desktop policy" in json.loads(browser_dialog_tool.browser_dialog("dismiss", task_id="task"))["error"]



def test_stale_ssh_marker_never_reconfigures_terminal_during_observation(managed, monkeypatch):
    from tools import terminal_tool
    from tools.bot_desktop import sandbox_host

    owner = {"backend": "SSHEnvironment", "ssh": ["old-host", "runner", "22", "", []]}
    marker = managed.parent / "sandbox.json"
    monkeypatch.setattr(sandbox_host, "_marker", lambda: marker)
    marker.write_text(json.dumps(owner))
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "ssh")
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {
        "env_type": "ssh", "ssh_host": "new-host", "ssh_user": "runner", "ssh_port": 22,
        "ssh_key": "", "ssh_config": "",
    })
    lookup = Mock(return_value=None)
    monkeypatch.setattr(runtime, "_sandbox_env", lookup)
    assert runtime._owned_sandbox_env() is None
    with pytest.raises(RuntimeError, match="restore its terminal settings"):
        runtime.stop()
    with pytest.raises(RuntimeError, match="stop it before starting another"):
        runtime._start_in_sandbox(1)
    assert json.loads(marker.read_text()) == owner
    assert all(call.kwargs["create"] is False for call in lookup.call_args_list)


def test_raw_cdp_frame_dispatch_refuses_warm_nonexecutor_endpoint(managed, monkeypatch):
    from tools import browser_cdp_tool

    frame_route = Mock(side_effect=AssertionError("stale supervisor consulted"))
    monkeypatch.setattr(browser_cdp_tool, "_browser_cdp_via_supervisor", frame_route)
    for document in ("bot_desktop:\n  executor_only: true\n", "bot_desktop: invalid\n"):
        managed.write_text(document)
        result = json.loads(browser_cdp_tool.browser_cdp("Target.getTargets", frame_id="warm-frame", task_id="task"))
        assert "CDP" in result["error"] or "managed desktop policy" in result["error"]
    frame_route.assert_not_called()


def test_rfb_reattachment_uses_ticket_profile_terminal_settings(managed, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli.web_routers.display import _rfb_sandbox
    from tools.bot_desktop import sandbox_host
    from tools.terminal_scope import terminal_env

    launch = tmp_path / "launch"
    named = launch / "profiles" / "remote"
    named.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    (named / "config.yaml").write_text("terminal:\n  backend: ssh\n  ssh_host: named-executor\n")
    env = SimpleNamespace(_bd_desktop_user=None)

    def acquire():
        assert terminal_env("TERMINAL_ENV") == "ssh"
        assert terminal_env("TERMINAL_SSH_HOST") == "named-executor"
        return env

    monkeypatch.setattr(runtime, "_owned_sandbox_env", acquire)
    assert _rfb_sandbox(named)[0] is env
    assert terminal_env("TERMINAL_ENV") == "local"



def test_sandbox_stop_retains_marker_after_connection_failure(managed, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    marker = managed.parent / "sandbox.json"
    marker.write_text('{"ssh":["fixture"]}')
    monkeypatch.setattr(sandbox_host, "_marker", lambda: marker)
    env = SimpleNamespace(_bd_desktop_user=None)
    monkeypatch.setattr(runtime, "_owned_sandbox_env", lambda: env)
    result = Mock(return_value=subprocess.CompletedProcess([], 255))
    monkeypatch.setattr(streams, "run_in", result)

    with pytest.raises(RuntimeError, match="Could not stop the desktop"):
        runtime.stop()
    assert marker.exists()
    result.return_value = subprocess.CompletedProcess([], 3)
    assert not runtime.stop()
    assert not marker.exists()



@pytest.mark.asyncio
async def test_rfb_policy_refusal_closes_with_the_desktop_error(managed, monkeypatch):
    from unittest.mock import AsyncMock
    from hermes_cli.web_routers import display
    from tools.terminal_scope import TerminalPolicyUnavailable

    monkeypatch.setattr(placement, "_terminal_backend", Mock(side_effect=TerminalPolicyUnavailable("fixture refusal")))
    ws = Mock(close=AsyncMock())
    await display._bridge(ws, {"hermes_home": str(managed.parent)})
    ws.close.assert_awaited_once_with(code=4001, reason="Bot Desktop is not running")



def test_corrupt_ownership_blocks_desktop_replacement(managed, monkeypatch):
    from tools.bot_desktop import sandbox_host

    marker = managed.parent / "sandbox.json"
    marker.write_text("{")
    monkeypatch.setattr(sandbox_host, "_marker", lambda: marker)
    create = Mock(side_effect=AssertionError("replacement acquired"))
    monkeypatch.setattr(runtime, "_sandbox_env", create)
    with pytest.raises(RuntimeError, match="stored desktop owner"):
        runtime._start_in_sandbox(1)
    create.assert_not_called()


def test_lost_detached_launch_reply_keeps_owner(managed, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    marker = managed.parent / "sandbox.json"
    monkeypatch.setattr(sandbox_host, "_marker", lambda: marker)
    env = SimpleNamespace(_bd_desktop_user=None, _container_id="c0ffee")
    monkeypatch.setattr(sandbox_host, "_published", lambda *args: {})
    monkeypatch.setattr(sandbox_host, "stop", lambda *args: False)
    monkeypatch.setattr(sandbox_host, "missing_binaries", lambda *args: [])

    def transport(_env, argv, **kwargs):
        if "setsid -f" in argv[-1]:
            assert json.loads(marker.read_text())["container"] == "c0ffee"
            raise subprocess.TimeoutExpired("launch", 1)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(streams, "run_in", transport)
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox_host.start(env, "default", geometry="1280x800")
    assert json.loads(marker.read_text())["profile"] == "default"



@pytest.mark.platforms("linux")
def test_remote_stop_does_not_signal_unrelated_reused_pid(managed, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    env = SimpleNamespace(_bd_desktop_user=None, get_temp_dir=lambda: str(managed.parent))
    desktop = managed.parent / "hermes-bot-desktop" / "default"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(streams, "run_in", lambda _env, argv, **kw: subprocess.run(argv, capture_output=True, check=False))
    with subprocess.Popen(["sleep", "30"], start_new_session=True) as other:
        try:
            (desktop / "launcher.pid").write_text(str(other.pid))
            assert not sandbox_host.stop(env, "default")
            assert other.poll() is None
        finally:
            other.terminate()
