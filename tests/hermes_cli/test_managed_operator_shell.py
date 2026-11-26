import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_cli import managed_operator_shell as shell


def test_placement_ignores_profile_and_dotenv(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    path = managed / "config.yaml"
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("TERMINAL_SSH_HOST", "profile-host")
    monkeypatch.setenv("PROJECT_DIR", "expanded")
    terminal = {"backend": "ssh", "ssh_host": "managed-host", "cwd": "/literal ${PROJECT_DIR}"}
    path.write_text(json.dumps({"terminal": terminal, "webapp": {"terminal_environment": "executor"}}))

    assert shell.placement() == shell.Placement("executor", terminal)
    path.write_text('terminal: {backend: ssh}')
    with pytest.raises(shell.OperatorShellUnavailable):
        shell.placement()
    path.unlink()
    with pytest.raises(shell.OperatorShellUnavailable):
        shell.placement()


def test_executor_arguments_pin_ssh_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    key = tmp_path / "key"
    config = tmp_path / "settings"
    key.touch()
    config.touch()
    terminal = {"backend": "ssh", "ssh_host": "executor", "ssh_user": "runner", "ssh_port": 22,
                "ssh_key": str(key), "ssh_config": str(config), "cwd": "/workspace"}
    argv, _ = shell.executor_command(shell.Placement("executor", terminal), "pwd", None)

    assert Path(argv[0]).is_absolute()
    assert Path(argv[0]).parent != tmp_path
    assert argv[1:4] == ["-T", "-F", str(config)]
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[argv.index("-p") + 1] == "22"
    assert argv[argv.index("-i") + 1] == str(key)
    assert argv[-2] == "runner@executor"
    key.unlink()
    with pytest.raises(shell.OperatorShellUnavailable, match="is missing"):
        shell.executor_command(shell.Placement("executor", terminal), "pwd", None)


@pytest.mark.platforms("posix")
def test_remote_command_preserves_literal_cwd_streams_and_status(tmp_path):
    cwd = tmp_path / " project ${name} '雪 "
    cwd.mkdir()
    receipt = "test-receipt="
    argv = [sys.executable, "-c", shell._REMOTE_COMMAND, str(cwd),
            "pwd -P; printf 'stderr text' >&2; exit 255", receipt, "separate"]
    reader, writer = os.pipe()
    try:
        process = subprocess.run(argv, stdin=reader, capture_output=True, text=True, timeout=5, check=False)
    finally:
        os.close(reader)
        os.close(writer)
    result = shell.executor_result(argv, receipt, process.returncode, process.stdout, process.stderr)
    assert Path(result.stdout.rstrip("\n")) == cwd.resolve()
    assert result.stderr == "stderr text"
    assert result.returncode == 255


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("command", ["echo started; sleep 60", "sleep 60 & echo started"])
def test_remote_command_stops_group_when_control_pipe_closes(tmp_path, command):
    argv = [sys.executable, "-c", shell._REMOTE_COMMAND, str(tmp_path),
            command, "test-receipt=", "separate"]
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline() == "started\n"
        process.stdin.close()
        process.stdin = None
        assert process.communicate(timeout=5) == ("", "")
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
        if process.stdin:
            process.stdin.close()


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
async def test_classic_output_streams_in_order_before_interruption(tmp_path, monkeypatch):
    command = "printf 'first\\n'; printf 'second\\n' >&2; sleep 60"
    argv = [sys.executable, "-c", shell._REMOTE_COMMAND, str(tmp_path), command, "receipt=", "merge"]
    monkeypatch.setattr(shell, 'executor_command', lambda *args, **kwargs: (argv, 'receipt='))
    got_output = asyncio.Event()
    lines = []

    def emit(line):
        lines.append(line)
        if len(lines) == 2:
            got_output.set()

    task = asyncio.create_task(shell.run_executor_async(shell.Placement("executor", {}), command, None,
                                                       emit=emit, timeout=30))
    try:
        await asyncio.wait_for(got_output.wait(), 5)
        assert lines == ['first', 'second']
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_classic_shell_uses_owner_cwd_and_stops_on_mode_disagreement(monkeypatch, capsys):
    from hermes_cli import bang_shell, gateway_chat_view

    selected = shell.Placement("executor", {})
    monkeypatch.setattr(shell, "placement", lambda: selected)
    monkeypatch.setattr(bang_shell, "check_bang_approval", lambda _: {"approved": True})
    command = AsyncMock(return_value=4)
    monkeypatch.setattr(gateway_chat_view, "_interactive_executor_shell", command)
    rpc = AsyncMock(return_value={"environment": "executor", "cwd": "/literal project"})
    view = gateway_chat_view.GatewayChatView(SimpleNamespace(rpc=rpc), {"stored_session_id": "stored-id"})

    assert await view.shell_line("!pwd")
    rpc.assert_awaited_once_with("shell.cwd", session_id="stored-id")
    command.assert_awaited_once_with(selected, "pwd", "/literal project", bang_shell.DEFAULT_TIMEOUT)
    assert "exited 4" in capsys.readouterr().err

    command.reset_mock()
    rpc.return_value = {"environment": "gateway", "cwd": "/home"}
    assert await view.shell_line("!pwd")
    command.assert_not_awaited()
    assert "different managed shell settings" in capsys.readouterr().err

    rpc.return_value = {"environment": "executor"}
    assert await view.shell_line("!pwd")
    command.assert_not_awaited()
    assert "did not return a shell directory" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["executor", "gateway"])
@pytest.mark.parametrize("action", ["bang", "quick"])
def test_classic_interactive_shell_checks_its_owner_before_running(monkeypatch, mode, action):
    from cli import HermesCLI
    from hermes_cli import bang_shell, gateway_side
    from hermes_cli.gateway_client import GatewayClientError

    cli = HermesCLI.__new__(HermesCLI)
    cli.session_id = "stored-id"
    cli._console_print = Mock()
    selected = shell.Placement(mode, {})
    owner_cwd = "/executor workspace ${literal}"
    call = Mock(return_value={"environment": mode, "cwd": owner_cwd})
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    run_local = Mock(return_value=0)
    monkeypatch.setattr(bang_shell, "bang_shell_enabled", lambda: True)
    monkeypatch.setattr(bang_shell, "check_bang_approval", lambda _: {"approved": True})
    monkeypatch.setattr(bang_shell, "run_bang_command", run_local)
    monkeypatch.setattr(gateway_side, "connection_for", lambda _: SimpleNamespace(call=call))
    monkeypatch.setattr(shell, "placement", lambda: selected)
    monkeypatch.setattr(shell, "run_executor", run)

    def invoke():
        if action == "bang":
            return cli.handle_bang_shell("!pwd")
        return cli._run_quick_command("/pwd", {"type": "exec", "command": "pwd"}, "")

    assert invoke()
    params = {"executor": True} if mode == "gateway" and action == "quick" else {}
    call.assert_called_once_with("shell.cwd", session_id="stored-id", **params)
    if mode == "gateway" and action == "bang":
        assert run_local.call_args.args == ("pwd",)
        assert run_local.call_args.kwargs["cwd"] == owner_cwd
        assert run_local.call_args.kwargs["strict_cwd"] is True
        run.assert_not_called()
    else:
        assert run.call_args.args == (selected, "pwd", owner_cwd)
        run_local.assert_not_called()

    run.reset_mock()
    run_local.reset_mock()
    call.return_value = {"environment": mode}
    assert invoke()
    call.return_value = {"environment": "executor" if mode == "gateway" else "gateway", "cwd": "/wrong"}
    assert invoke()
    if mode == "gateway" and action == "bang":
        for malformed in (None, "relative", "/nul\x00"):
            call.return_value = {"environment": mode, "cwd": malformed}
            assert invoke()
    call.side_effect = GatewayClientError("lost connection")
    assert invoke()
    run.assert_not_called()
    run_local.assert_not_called()
