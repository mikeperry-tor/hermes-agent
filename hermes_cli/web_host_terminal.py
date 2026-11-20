"""Host-terminal policy and process setup for the browser-hosted Desktop UI."""

from __future__ import annotations

import os
import asyncio
import json
import shlex
from contextlib import contextmanager
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

from hermes_cli.config_backend import ConfigBackendUnavailable

META_PREFIX = "\0HERMES_TERMINAL_META:"


def request_allowed() -> bool:
    """Allow host shells only for Webapp on loopback or behind authentication."""
    from hermes_cli.web_server import app
    from hermes_cli.web_server_chat import _LOOPBACK_HOSTS
    from hermes_cli.web_server_surface import policy

    if not policy(app.state).host_terminal:
        return False
    if getattr(app.state, "auth_required", False):
        return True
    return (getattr(app.state, "bound_host", "") or "").strip().lower() in _LOOPBACK_HOSTS


def shell_command(candidate: str) -> Optional[str]:
    """Return an executable shell path for ``candidate``, or None.

    A relative path is refused: the PTY chdirs into the workspace before exec,
    so it would name a different file than the one checked here.
    """
    from hermes_platform.resolver import locate_command

    raw = (candidate or "").strip()
    found = locate_command(raw).command if raw else ()
    return found[0] if found else None


def shell_spec() -> tuple[list[str], str]:
    """Resolve the interactive-shell ladder the native Desktop uses (electron/terminal-ipc.ts).

    POSIX honours ``$SHELL``, then /bin/zsh, /bin/bash, /bin/sh. Windows ignores ``$SHELL``
    (usually a stray MSYS/Git path) and takes PowerShell 7, Windows PowerShell, ``COMSPEC``,
    then cmd.exe. Electron's ``HERMES_DESKTOP_SHELL`` override is not read here: settings do
    not ride on ``HERMES_*`` environment variables.
    """
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
        windows_powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        ladder = ("pwsh.exe", "pwsh", str(windows_powershell), "powershell.exe", os.environ.get("COMSPEC", ""))
        fallback = "cmd.exe"
    else:
        ladder = (os.environ.get("SHELL", ""), "/bin/zsh", "/bin/bash", "/bin/sh")
        fallback = "/bin/sh"
    command = next(filter(None, map(shell_command, ladder)), fallback)

    name = Path(command).name.lower()
    if name.startswith(("pwsh", "powershell")):
        args = ["-NoLogo"]
    elif name.startswith("cmd"):
        args = []
    elif "zsh" in name or "bash" in name:
        args = ["-il"]
    else:
        args = ["-i"]
    return [command, *args], name


def safe_cwd(requested: Optional[str], *, allow_file: bool = True) -> str:
    fallback = Path.home()
    try:
        candidate = Path(requested or fallback).expanduser().resolve()
        if candidate.is_dir():
            return str(candidate)
        if allow_file and candidate.is_file():
            return str(candidate.parent)
    except (OSError, RuntimeError, ValueError):
        pass
    return str(fallback)


def terminal_lc_ctype(env: Mapping[str, str], platform: str) -> Optional[str]:
    """Return the LC_CTYPE a host shell's ``env`` needs added, or None to leave it alone.

    macOS accepts the bare charset name ``UTF-8`` as a locale; glibc rejects it,
    and LC_CTYPE outranks LANG, so forcing it on Linux breaks even a valid LANG.
    There a set LANG is left in charge (copying it would pin a value a login rc
    may still change) and only a missing one falls back to glibc's C.UTF-8.
    Windows shells take their code page from the console, not LC_CTYPE. Desktop's
    terminalLcCtype (electron/terminal-ipc.ts) shares the darwin and C.UTF-8 choices
    but copies LANG into LC_CTYPE and does not defer to LC_ALL.
    """
    if env.get("LC_ALL") or env.get("LC_CTYPE") or platform == "win32":
        return None
    if platform == "darwin":
        return "UTF-8"
    return None if env.get("LANG") else "C.UTF-8"


@contextmanager
def profile_terminal_scope(home: Path, *, managed_strict=False):
    """Bind the complete native scope for shell environment and connection settings."""
    from agent.runtime_scope import bind_profile_scope
    from hermes_constants import get_process_hermes_home
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting
    if home.resolve() != get_process_hermes_home().resolve():
        activate_multi_profile_hosting()
    with bind_profile_scope(home, terminal_managed_strict=managed_strict):
        yield


def resolve_argv(
    *, home: Path, requested_cwd: Optional[str] = None, use_profile_cwd: bool = False,
) -> tuple[list[str], str, dict[str, str], str]:
    """Return argv/cwd/env/name for Webapp's authenticated host terminal in ``home``."""
    with profile_terminal_scope(home):
        return _resolve_argv(home=home, requested_cwd=requested_cwd, use_profile_cwd=use_profile_cwd)


def _resolve_argv(*, home, requested_cwd=None, use_profile_cwd=False):
    from hermes_cli import __version__
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
    from hermes_platform.host.facts import os_family
    from tools.environments.local import build_subprocess_env, served_profile_child_env
    from tools.terminal_scope import enforce_no_refusal, get_terminal_scope
    enforce_no_refusal()
    base_env = served_profile_child_env(target_home=home)
    for env_var in TERMINAL_CONFIG_ENV_MAP.values():
        base_env.pop(env_var, None)
    terminal_scope = get_terminal_scope()
    assert terminal_scope is not None  # both runtime scopes bind the complete policy
    base_env.update(terminal_scope)
    env = build_subprocess_env(base=base_env, scrub_secrets=True)

    # A dashboard started through `pnpm run` carries the package manager's
    # settings; the user's own shell (and pnpm) must not inherit them. Package
    # managers inject lowercase names: a user's own NPM_CONFIG_REGISTRY stays.
    for key in list(env):
        if key.startswith(("npm_config_", "npm_package_", "pnpm_config_")):
            env.pop(key, None)
    for key in ("NO_COLOR", "FORCE_COLOR", "COLORFGBG"):
        env.pop(key, None)
    env["COLORTERM"] = "truecolor"
    env["TERM"] = "xterm-256color"
    env["TERM_PROGRAM"] = "Hermes"
    env["TERM_PROGRAM_VERSION"] = __version__
    env["HERMES_DESKTOP_TERMINAL"] = "1"
    if lc_ctype := terminal_lc_ctype(env, os_family()):
        env["LC_CTYPE"] = lc_ctype

    argv, shell_name = shell_spec()
    if use_profile_cwd and requested_cwd is None:
        # Browser terminals can start without consulting the separate file backend.
        configured = terminal_scope.get("TERMINAL_CWD")
        if configured and configured.strip() not in {"", ".", "auto", "cwd"}:
            return argv, safe_cwd(configured, allow_file=False), env, shell_name
    return argv, safe_cwd(requested_cwd), env, shell_name


def query_dimension(raw: Optional[str], default: int, maximum: int) -> int:
    try:
        value = int(raw or default)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(2, min(maximum, value))


class ExecutorTerminalError(RuntimeError):
    """A startup diagnostic safe to show in the browser."""


def configured_terminal() -> tuple[str, str]:
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.managed_scope import load_managed_config
    from hermes_constants import get_process_hermes_home
    from hermes_yaml import YAMLError

    unavailable = 'Terminal launch configuration is unavailable; repair it and restart the Webapp'
    try:
        with profile_terminal_scope(get_process_hermes_home()):
            managed = load_managed_config(strict=True)
            config = load_user_config_effective(fail_closed=True, managed_snapshot=managed)
    except (ConfigBackendUnavailable, OSError, TypeError, ValueError, YAMLError):
        raise ExecutorTerminalError(unavailable) from None
    webapp = config.get('webapp', {})
    if not isinstance(webapp, dict):
        raise ExecutorTerminalError('webapp must be a mapping')
    mode = webapp.get('terminal_environment', 'gateway')
    if mode not in ('gateway', 'executor'):
        raise ExecutorTerminalError('webapp.terminal_environment must be gateway or executor')
    ssh_config = webapp.get('executor_ssh_config', '') if mode == 'executor' else ''
    if not isinstance(ssh_config, str) or (ssh_config and not Path(ssh_config).is_absolute()):
        raise ExecutorTerminalError('webapp.executor_ssh_config must be an absolute SSH configuration path')
    return mode, ssh_config


def environment(application=None):
    if application is None:
        from hermes_cli.web_server import app as application
    return getattr(application.state, 'terminal_environment', 'gateway')


def _executor_connection():
    from tools.terminal_scope import terminal_env
    from tools.terminal_tool import _resolve_config_cwd
    from hermes_cli.gateway_connections_ssh_transport import (
        SshEndpoint, SshReachError, resolve_ssh_binary, validate_key_path, validate_ssh_target,
    )

    if terminal_env('TERMINAL_ENV', 'local') != 'ssh':
        raise ExecutorTerminalError('Executor terminals currently require terminal.backend: ssh')
    host = terminal_env('TERMINAL_SSH_HOST', '')
    user = terminal_env('TERMINAL_SSH_USER', '')
    key = terminal_env('TERMINAL_SSH_KEY', '')
    try:
        port = int(terminal_env('TERMINAL_SSH_PORT', '22'))
        validate_ssh_target(host, user, port)
        validate_key_path(key)
    except (ValueError, SshReachError):
        raise ExecutorTerminalError('Executor terminal: check the configured SSH host, user, port and identity') from None
    cwd, _ = _resolve_config_cwd('ssh', False)
    binary = resolve_ssh_binary()
    key_path = str(Path(key).expanduser().absolute()) if key else ''
    return binary, SshEndpoint(host, user, port, key_path), cwd


def _executor_settings(home):
    from tools.terminal_scope import TerminalPolicyUnavailable

    try:
        with profile_terminal_scope(home, managed_strict=True):
            _, _, env, _ = _resolve_argv(home=home)
            return env, _executor_connection()
    except TerminalPolicyUnavailable:
        raise ExecutorTerminalError(
            'Executor terminal configuration is unavailable; repair it and retry') from None


async def prepare(home, requested_cwd=None, *, mode=None, ssh_config=''):
    """Prepare before the PTY spawn lock; no sandbox, sync or remote Hermes server is created."""
    from hermes_cli.gateway_connections_ssh_transport import build_exec_args
    mode = environment() if mode is None else mode
    if mode not in ('gateway', 'executor'):
        raise ExecutorTerminalError('Invalid terminal environment')
    try:
        if mode == 'gateway':
            argv, cwd, env, shell = await asyncio.to_thread(
                resolve_argv, home=home, requested_cwd=requested_cwd, use_profile_cwd=True)
            return argv, cwd, env, shell, cwd
        env, (binary, endpoint, default_cwd) = await asyncio.to_thread(_executor_settings, home)
    except ConfigBackendUnavailable:
        # Native unavailable reads deliberately bypass Exception/default handlers.
        # Contain this operation before SystemExit reaches the admission task.
        raise ExecutorTerminalError(
            'Terminal configuration is unavailable; restore its backend and retry') from None
    if ssh_config and endpoint.key_path and not await asyncio.to_thread(Path(endpoint.key_path).is_file):
        raise ExecutorTerminalError('Executor terminal: configured SSH identity is missing')
    requested = default_cwd if requested_cwd is None else requested_cwd
    if not isinstance(requested, str) or not requested or '\x00' in requested:
        raise ExecutorTerminalError('Executor terminal cwd must be a nonempty path without NUL')
    from hermes_cli.gateway_connections_ssh_transport import login_shell_quote
    probe_command = 'python3 -c ' + login_shell_quote(_PROBE) + ' ' + login_shell_quote(requested)
    probe = await asyncio.create_subprocess_exec(
        binary, '-n', *build_exec_args(endpoint, probe_command, ssh_config=ssh_config), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await probe.communicate()  # health: allow HX006 -- startup viewer cancellation kills/reaps SSH; native connect/keepalive owns transport, no total work deadline
    except BaseException:
        # A closed startup viewer revokes this probe, not an arbitrary elapsed
        # duration. Killing/reaping SSH also closes its remote command channel.
        if probe.returncode is None:
            probe.kill()
        await probe.wait()  # health: allow HX006 -- killed this exact SSH child above; reap without draining descendant pipes
        raise
    if probe.returncode:
        if probe.returncode == 2:
            reason = 'cwd is missing or inaccessible'
        elif probe.returncode == 3:
            reason = 'Bash is unavailable on the executor'
        else:
            reason = 'SSH connection or remote Python failed; check the executor, identity and host key'
        raise ExecutorTerminalError('Executor terminal: ' + reason)
    try:
        remote_cwd = json.loads(output)
        if not isinstance(remote_cwd, str) or not remote_cwd.startswith('/') or '\x00' in remote_cwd:
            raise ValueError('invalid cwd')
    except ValueError:
        raise ExecutorTerminalError('Executor returned an invalid cwd') from None
    command = 'cd -- ' + shlex.quote(remote_cwd) + ' && exec bash --rcfile <(printf %s ' + shlex.quote(_RC) + ') -i'
    argv = [binary, '-tt', *build_exec_args(endpoint, 'bash -c ' + login_shell_quote(command), ssh_config=ssh_config)]
    return argv, str(home), env, 'Executor · ' + endpoint.target, remote_cwd


_PROBE = '''import json, os, shutil, sys
from pathlib import Path
if not shutil.which('bash'):
    sys.exit(3)
try:
    p = Path(sys.argv[1]).expanduser().resolve(strict=True)
    if p.is_file(): p = p.parent
    os.chdir(p)
    print(json.dumps(os.getcwd()))
except (OSError, RuntimeError, ValueError):
    sys.exit(2)
'''

# bash --rcfile preserves system/user initialization and existing prompt hooks.
# OSC 7 encodes whitespace and metacharacters as URI data, never shell syntax.
_RC = r'''
if [ -f /etc/bash.bashrc ]; then source /etc/bash.bashrc; fi
if [ -f ~/.bashrc ]; then source ~/.bashrc; fi
_hermes_terminal_cwd() {
    python3 -c 'import os, urllib.parse; print("\033]7;file://executor" + urllib.parse.quote(os.getcwd(), safe="/") + "\007", end="", flush=True)'
}
if declare -p PROMPT_COMMAND 2>/dev/null | grep -q 'declare -a'; then
    PROMPT_COMMAND+=(_hermes_terminal_cwd)
else
    PROMPT_COMMAND="${PROMPT_COMMAND:+${PROMPT_COMMAND}; }_hermes_terminal_cwd"
fi
'''
