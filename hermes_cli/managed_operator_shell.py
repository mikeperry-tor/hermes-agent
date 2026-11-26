"""Run operator commands using the deployment's managed terminal placement."""

from __future__ import annotations

import asyncio
import codecs
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import uuid


class OperatorShellUnavailable(RuntimeError):
    """The operator command could not be started in the selected environment."""


def _terminal_error() -> OperatorShellUnavailable:
    return OperatorShellUnavailable("Managed operator shell settings are unavailable. Check the administrator's configuration.")


@dataclass(frozen=True)
class Placement:
    mode: str
    terminal: dict


def placement() -> Placement | None:
    """Read the raw administrator policy, without profile or environment expansion."""
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.managed_scope import load_managed_config
    from hermes_yaml import YAMLError

    try:
        managed = load_managed_config(strict=True)
    except (ConfigBackendUnavailable, OSError, TypeError, ValueError, YAMLError):
        raise _terminal_error() from None
    terminal = managed.get("terminal", {})
    webapp = managed.get("webapp", {})
    if not isinstance(webapp, dict):
        raise _terminal_error()
    if "terminal_environment" not in webapp:
        if "executor_ssh_config" in webapp or os.environ.get("HERMES_MANAGED_DIR", "").strip():
            raise _terminal_error()
        return None
    mode = webapp["terminal_environment"]
    if mode not in ("gateway", "executor") or not isinstance(terminal, dict):
        raise _terminal_error()
    return Placement(mode, terminal)


def classic_shell_cwd(cli, selected: Placement, *, executor: bool = False) -> str | None:
    """Confirm managed placement and read the directory from the classic session's owner."""
    from hermes_cli.gateway_client import GatewayClientError
    from hermes_cli.gateway_side import connection_for

    connection = connection_for(cli)
    if connection is None or not cli.session_id:
        raise OperatorShellUnavailable("The chat owner is unavailable. Reopen the chat to run a shell command.")
    params = {"session_id": cli.session_id}
    if executor and selected.mode == "gateway":
        params["executor"] = True
    try:
        owner = connection.call("shell.cwd", **params)
    except GatewayClientError as exc:
        raise OperatorShellUnavailable("Could not confirm shell placement with the chat owner.") from exc
    return owner_shell_cwd(selected, owner, executor=executor)


def owner_shell_cwd(selected: Placement, owner, *, executor: bool = False) -> str | None:
    """Check the owner's placement and directory, including an explicit null default."""
    if not isinstance(owner, dict) or owner.get("environment") != selected.mode:
        raise OperatorShellUnavailable("The chat and its owner have different managed shell settings.")
    if "cwd" not in owner or (owner["cwd"] is not None and not isinstance(owner["cwd"], str)):
        raise OperatorShellUnavailable("The chat owner did not return a shell directory.")
    cwd = owner["cwd"]
    if selected.mode == "gateway" and not executor and (not cwd or not os.path.isabs(cwd) or "\x00" in cwd):
        raise OperatorShellUnavailable("The chat owner did not return an administrative shell directory.")
    return cwd


# Hold stdin open while the SSH client owns the command. EOF (including a
# disconnect) stops the foreground group while keeping stdout and stderr on
# separate SSH channels. The child has no access to the control pipe.
_REMOTE_COMMAND = """\
import os, selectors, signal, subprocess, sys
from contextlib import suppress

cwd, command, receipt, merge = sys.argv[1:]
try:
    proc = subprocess.Popen(
        ["bash", "-c", command], cwd=cwd, stdin=subprocess.DEVNULL, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge == "merge" else subprocess.PIPE,
    )
except OSError:
    sys.exit(126)

def stop_group():
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)

with selectors.DefaultSelector() as streams:
    streams.register(0, selectors.EVENT_READ)
    streams.register(proc.stdout, selectors.EVENT_READ, sys.stdout.buffer)
    if proc.stderr is not None:
        streams.register(proc.stderr, selectors.EVENT_READ, sys.stderr.buffer)
    try:
        while len(streams.get_map()) > 1 or proc.poll() is None:
            for key, _ in streams.select(None if len(streams.get_map()) > 1 else 0.1):
                if key.fd == 0:
                    stop_group()
                    sys.exit(0)
                chunk = os.read(key.fd, 65536)
                if chunk:
                    key.data.write(chunk)
                    key.data.flush()
                else:
                    streams.unregister(key.fileobj)
    finally:
        if proc.returncode is None:
            stop_group()
            proc.wait()
code = proc.returncode
sys.stderr.write(receipt + str(code if code >= 0 else 128 - code) + "\\n")
"""


def executor_command(selected: Placement, command: str, cwd: str | None, *,
                     merge: bool = False) -> tuple[list[str], str]:
    from hermes_cli.gateway_connections_ssh_transport import (
        SshEndpoint, SshReachError, build_exec_args, login_shell_quote,
        validate_key_path, validate_ssh_target,
    )

    terminal = selected.terminal
    if terminal.get("backend") != "ssh":
        raise _terminal_error()
    host = terminal.get("ssh_host")
    user = terminal.get("ssh_user")
    port = terminal.get("ssh_port")
    key = terminal.get("ssh_key")
    config = terminal.get("ssh_config")
    if not all(isinstance(value, str) and value for value in (host, user, key, config)):
        raise _terminal_error()
    if not Path(key).is_absolute() or not Path(config).is_absolute():
        raise _terminal_error()
    if cwd is None:
        cwd = terminal.get("cwd")
        if cwd is None:
            roots = terminal.get("ssh_workspace_roots")
            if isinstance(roots, list) and roots:
                cwd = roots[0]
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\x00" in cwd:
        raise OperatorShellUnavailable("The executor working directory is unavailable.")
    try:
        validate_ssh_target(host, user, port)
        validate_key_path(key)
        validate_key_path(config)
        if not Path(key).is_file() or not Path(config).is_file():
            raise OperatorShellUnavailable("The managed SSH identity or configuration file is missing.")
    except (OSError, ValueError, SshReachError):
        raise OperatorShellUnavailable("The managed SSH settings are unusable. Check the endpoint, identity, and SSH client.") from None
    binary = shutil.which("ssh", path=os.defpath) if os.name == "posix" else None
    if binary is None:
        raise OperatorShellUnavailable("The system OpenSSH client is unavailable.")
    endpoint = SshEndpoint(host, user, port, key)
    receipt = "hermes-shell-" + uuid.uuid4().hex + "="
    fields = (_REMOTE_COMMAND, cwd, command, receipt, "merge" if merge else "separate")
    remote = "exec python3 -c " + " ".join(login_shell_quote(value) for value in fields)
    return [binary, "-T", *build_exec_args(endpoint, remote, ssh_config=config)], receipt


def executor_result(argv: list[str], receipt: str, status: int, stdout: str, stderr: str) -> subprocess.CompletedProcess:
    if status == 126:
        raise OperatorShellUnavailable("The executor working directory or shell is unavailable.")
    if status != 0:
        raise OperatorShellUnavailable("Could not run the executor shell. Check SSH connectivity, identity, and host key.")
    before, separator, after = stderr.rpartition(receipt)
    code, newline, trailing = after.partition("\n")
    if not separator or not newline or not code.isascii() or not code.isdecimal() or len(code) > 3 or int(code) > 255:
        raise OperatorShellUnavailable("The executor shell exited without a command status.")
    return subprocess.CompletedProcess(argv, int(code), stdout, before + trailing)


def run_executor(selected: Placement, command: str, cwd: str | None, *, timeout: int,
                 emit=None) -> subprocess.CompletedProcess:
    try:
        return asyncio.run(run_executor_async(selected, command, cwd, timeout=timeout, emit=emit))
    except TimeoutError:
        raise subprocess.TimeoutExpired("ssh", timeout) from None


async def _stream_lines(stream, emit) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    while chunk := await stream.read(65536):
        lines = (pending + decoder.decode(chunk)).split("\n")
        pending = lines.pop()
        for line in lines:
            emit(line.rstrip("\r"))
    if pending := pending + decoder.decode(b"", final=True):
        emit(pending)


async def _collect(process, emit):
    if emit is None:
        return await process.communicate()
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(_stream_lines(process.stdout, emit))
        stderr = tasks.create_task(process.stderr.read())
    await process.wait()
    return b"", stderr.result()


def managed_shell_refusal() -> tuple[int, str] | None:
    """Return ``(rpc_code, message)`` when legacy local shell RPCs must defer to managed placement."""
    try:
        if placement() is not None:
            return (4005, "Managed shell commands require a local operator chat on the session owner.")
    except OperatorShellUnavailable as exc:
        return (5003, str(exc))
    return None


async def run_executor_async(selected: Placement, command: str, cwd: str | None, *,
                             timeout: int, emit=None) -> subprocess.CompletedProcess:
    argv, receipt = executor_command(selected, command, cwd, merge=emit is not None)
    reader, writer = os.pipe()
    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=reader, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={},
            )
        except OSError:
            raise OperatorShellUnavailable("Could not start the managed SSH client.") from None
        try:
            stdout, stderr = await asyncio.wait_for(_collect(process, emit), timeout=timeout)
        except BaseException:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
            raise
    finally:
        os.close(reader)
        os.close(writer)
    return executor_result(
        argv, receipt, process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"),
    )
