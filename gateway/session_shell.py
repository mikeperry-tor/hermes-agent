"""Run a captured operator shell command for a local session."""
import asyncio
from functools import partial
import json
import os
import subprocess

from hermes_cli.managed_operator_shell import OperatorShellUnavailable, placement, run_executor_async
from persistence.core.errors import AuthorityRefusal

TIMEOUT_SECONDS = 30
_LOCAL_BACKENDS = frozenset({'', 'local'})


def handlers(connection):
    return {'shell.exec': partial(shell_exec, connection), 'shell.cwd': partial(shell_cwd, connection)}


def _operator_session(connection, ref, params):
    from gateway.session_busy_controls import authorize

    authorize(connection, ref, params, 'session:control')
    authority = connection.authority
    authority._require_admission_open()
    from gateway.session_local import authorize_local_source
    from gateway.session_policy import policy_for_source
    live = authority.sessions[ref.session_id]
    # Local terminal sessions only: an API, messaging or A2A session has no composer, and running
    # commands for it would be a remote-execution surface with no human at the keyboard.
    if authorize_local_source(authority.runner, live.source) is not True:
        raise AuthorityRefusal('permission_denied')
    policy = policy_for_source(authority.runner, live.source)
    if policy is None:
        raise AuthorityRefusal('permission_denied')
    try:
        selected = placement()
    except OperatorShellUnavailable as exc:
        raise AuthorityRefusal('operator_shell_unavailable', detail=str(exc)) from None
    if selected is not None and (not connection.native_owner or 'session:operator' not in connection.actor.capabilities):
        raise AuthorityRefusal('permission_denied')
    return live, policy, selected


def _agent_shell_cwd(connection, policy):
    from tools import file_tools_paths

    managed_roots = getattr(file_tools_paths, 'managed_ssh_workspace_roots', None)
    if managed_roots is None:
        return policy.cwd
    try:
        if managed_roots() is None:
            return policy.cwd
    except ValueError:
        pass
    return str(connection.authority.profile_id)


async def shell_cwd(connection, ref, params):
    """Read the managed shell directory from the owner of this chat."""
    if (set(params) - {'session_id', 'profile', 'executor'} or not ref.session_id
            or not isinstance(params.get('executor', False), bool)):
        raise AuthorityRefusal('invalid_params')
    live, policy, selected = _operator_session(connection, ref, params)
    if selected is None:
        raise AuthorityRefusal('operator_shell_unavailable')
    from gateway.session_terminal_cwd import operator_ssh_cwd
    try:
        cwd = (operator_ssh_cwd(connection.authority.runner, live.route, policy)
               if selected.mode == 'executor' or params.get('executor') is True
               else _agent_shell_cwd(connection, policy))
    except OperatorShellUnavailable as exc:
        raise AuthorityRefusal('operator_shell_unavailable', detail=str(exc)) from None
    return {'environment': selected.mode, 'cwd': cwd}


async def shell_exec(connection, ref, params):
    command = params.get('command')
    if (set(params) - {'session_id', 'profile', 'command'} or not isinstance(ref.session_id, str)
            or not ref.session_id or not isinstance(command, str) or not command.strip()):
        raise AuthorityRefusal('invalid_params')
    live, policy, selected = _operator_session(connection, ref, params)
    if selected is None:
        backend = str(json.loads(policy.terminal_json).get('TERMINAL_ENV') or '').strip().lower()
        if backend not in _LOCAL_BACKENDS:
            raise AuthorityRefusal('unsupported_terminal_backend', field='terminal.backend', detail=backend)
    from tools.approval_detection import detect_dangerous_command, detect_hardline_command
    # No prompt here, so anything the agent's gate would ask about is refused, including a command
    # the scanners could not read to the end (detect_dangerous_command reports that too).
    if detect_hardline_command(command)[0] or detect_dangerous_command(command)[0]:
        raise AuthorityRefusal('dangerous_command')
    if selected is None or selected.mode == 'gateway':
        cwd = policy.cwd if selected is None else _agent_shell_cwd(connection, policy)
        return await _run(command, cwd)
    from gateway.session_terminal_cwd import operator_ssh_cwd
    try:
        cwd = operator_ssh_cwd(connection.authority.runner, live.route, policy)
        result = await run_executor_async(selected, command, cwd, timeout=TIMEOUT_SECONDS)
    except TimeoutError:
        raise AuthorityRefusal('command_timeout') from None
    except OperatorShellUnavailable as exc:
        raise AuthorityRefusal('operator_shell_unavailable', detail=str(exc)) from None
    return _redacted_result(result)


async def _run(command, cwd):
    from gateway.run_inbound_hooks import _stop_exec_tree
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.environments.local import build_subprocess_env
    if not await asyncio.to_thread(os.path.isdir, cwd):
        raise AuthorityRefusal('cwd_unavailable')
    env = build_subprocess_env(strip_launch_profile=True)
    try:
        process = await asyncio.create_subprocess_shell(
            command, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=subprocess.DEVNULL, creationflags=windows_hide_flags(),
            start_new_session=True,  # detached-spawn: ok — awaited; cancellation kills the shell group
        )
    except OSError:
        raise AuthorityRefusal('command_failed') from None
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as exc:
        await asyncio.shield(_stop_exec_tree(process))
        if isinstance(exc, TimeoutError):
            raise AuthorityRefusal('command_timeout') from None
        raise
    return _redacted_result(subprocess.CompletedProcess(
        command, process.returncode, stdout.decode('utf-8', 'replace'), stderr.decode('utf-8', 'replace'),
    ))


def _redacted_result(result):
    from agent.redact import redact_sensitive_text

    # Redact before tailing so a credential crossing the slice boundary cannot survive as fragments.
    stdout = redact_sensitive_text(result.stdout or '', force=True, redact_url_credentials=True)[-4000:]
    stderr = redact_sensitive_text(result.stderr or '', force=True, redact_url_credentials=True)[-2000:]
    return {'stdout': stdout, 'stderr': stderr, 'code': result.returncode}
