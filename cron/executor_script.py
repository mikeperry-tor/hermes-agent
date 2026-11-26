"""Run scheduled scripts on the deployment's managed SSH executor."""

from __future__ import annotations

from contextlib import suppress
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import uuid

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


# The control pipe belongs to this supervisor, never to the script. EOF ends
# the run when the SSH client disconnects. Linux adopts detached descendants
# here so timeout, cancellation, and normal exit can reap the whole run.
_REMOTE_RUN = r"""
import ctypes
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

script, workdir, interpreter, root, roots, timeout, receipt, check_only, python_name = json.loads(sys.argv[1])

def within(path, allowed):
    return any(path.is_relative_to(Path(directory)) for directory in allowed)

def expand_remote_path(value):
    if value.startswith('~') and value != '~' and not value.startswith('~/'):
        raise ValueError('Use ~/ for the executor home directory')
    return Path(value).expanduser()

def resolve_path(value, base):
    value = expand_remote_path(value)
    return (base / value).resolve(strict=True)

def launch_arguments():
    base = Path(root)
    if base.resolve(strict=True) != base or not base.is_dir() or not within(base, roots):
        raise ValueError('The executor scripts directory is unavailable')
    path = resolve_path(script, base)
    if not path.is_relative_to(base) or not path.is_file():
        raise ValueError('The cron script must be a regular file inside its executor scripts directory')
    directory = resolve_path(workdir, base) if workdir else path.parent
    if not within(directory, roots) or (workdir and str(directory) != workdir) or not directory.is_dir():
        raise ValueError('The cron workdir is unavailable on the SSH executor')
    if path.suffix.lower() in ('.sh', '.bash'):
        command = shutil.which('bash')
    elif not interpreter:
        command = shutil.which('python3')
    else:
        requested = expand_remote_path(interpreter)
        if not requested.is_absolute():
            raise ValueError('The executor Python interpreter must have an absolute or ~-prefixed path')
        resolved = requested.resolve(strict=True)
        for name in (requested.name.lower(), resolved.name.lower()):
            if name.startswith('pythonw') or not re.fullmatch(python_name, name):
                raise ValueError('The executor interpreter must be a Python executable')
        command = str(requested)
    if not command or not Path(command).is_file() or not os.access(command, os.X_OK):
        raise ValueError('The cron interpreter is unavailable on the SSH executor')
    return command, str(path), directory

def kill_children():
    children = Path('/proc/thread-self/children')
    while True:
        for pid in children.read_text().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        time.sleep(0.01)

def run():
    command, path, directory = launch_arguments()
    if check_only:
        return 0, str(directory)
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError('Could not supervise cron descendants on the executor')
    with selectors.DefaultSelector() as control:
        control.register(0, selectors.EVENT_READ)
        proc = subprocess.Popen([command, path], cwd=directory, stdin=subprocess.DEVNULL, start_new_session=True)
        stop_at = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                remaining = stop_at - time.monotonic()
                if remaining <= 0:
                    return 'timeout', str(directory)
                if control.select(min(0.1, remaining)):
                    return 'cancelled', str(directory)
            return proc.returncode, str(directory)
        finally:
            kill_children()

try:
    result = run()
except (OSError, RuntimeError, ValueError) as exc:
    print(str(exc), file=sys.stderr)
    result = ('invalid', '')
sys.stderr.write('\n' + receipt + json.dumps(result) + '\n')
"""


def scripts_root(managed: dict, home: Path) -> str:
    from hermes_cli.profiles import validate_profile_name
    from hermes_constants import profile_name_for_home
    from hermes_cli.ssh_project_probe import _absolute_project

    cron = managed.get("cron")
    base = cron.get("ssh_scripts_base") if isinstance(cron, dict) else None
    if not _absolute_project(base) or base == "/":
        raise ValueError("The administrator's SSH cron scripts base directory is unavailable.")
    profile = profile_name_for_home(home)
    if profile is None:
        raise ValueError("The cron profile does not have an executor scripts directory.")
    if profile != "default":
        validate_profile_name(profile)
    return f"{base}/{profile}/scripts"


def _script_endpoint():
    from cron.executor_workspace import executor_policy
    from hermes_cli.ssh_project_probe import managed_project_connection

    managed, roots, terminal, home = executor_policy()
    return managed_project_connection(terminal, str(home)), managed, home, roots


def managed_script_source():
    """Return checked script lookup and a bounded read for lifecycle inspection."""
    import posixpath
    from cron.lifecycle_guard import _MAX_REFERENCED_SCRIPT_BYTES
    from hermes_cli.ssh_project_probe import _under_roots

    backend, managed, home, roots = _script_endpoint()

    def read(path, grants):
        if not _under_roots(path, grants):
            raise SshWorkspaceFsError("EACCES", "Script is outside the executor's permitted directories.")
        target, _ = backend.inspect_file(path)
        if not _under_roots(target, grants):
            raise SshWorkspaceFsError("EACCES", "Script resolves outside the executor's permitted directories.")
        data, _, _ = backend.read_bytes(target, max_bytes=_MAX_REFERENCED_SCRIPT_BYTES)
        return target, data.decode("utf-8-sig", errors="replace")

    def primary(script):
        root = scripts_root(managed, home)
        if backend.inspect_projects([root], roots)[root].get("cwd") != root:
            raise SshWorkspaceFsError("ENOENT", f"The executor scripts directory {root} is unavailable.")
        path = script if script.startswith(("/", "~")) else posixpath.join(root, script)
        return read(backend.normalize(path), (root,))

    def reference(path):
        if not _under_roots(path, roots):
            return None
        try:
            return read(path, roots)[1]
        except SshWorkspaceFsError as exc:
            if exc.code in ("ENOENT", "ENOTREG"):
                return None
            raise

    return primary, reference, roots[0], backend.normalize("~")


def check_executor_script(script, workdir=None, interpreter=None) -> str | None:
    from cron.scheduler_script import _get_script_timeout

    ok, outcome = run_executor_script(script, workdir, interpreter, _get_script_timeout(), check_only=True)
    return None if ok else str(outcome)


def executor_script_command(script, workdir, interpreter, timeout, *, check_only=False):
    """Build a command on the validated profile's pinned SSH transport."""
    from cron.lifecycle_guard import _INTERPRETER_IMAGE_RE

    backend, managed, home, roots = _script_endpoint()
    root = scripts_root(managed, home)
    receipt = "hermes-cron-" + uuid.uuid4().hex + "="
    data = json.dumps((script, workdir, interpreter, root, roots, timeout, receipt, check_only,
                       _INTERPRETER_IMAGE_RE.pattern))
    command = "exec python3 -c " + shlex.quote(_REMOTE_RUN) + " " + shlex.quote(data)
    return [*backend.ssh_command(), command], receipt, root


def _read_script_result(proc, control, receipt, cancel_event):
    from cron.cancellation import cancel_reason

    reason = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            # An EOF lets the remote supervisor kill and reap descendants.
            control.close()
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired as exc:
                raise SshWorkspaceFsError("ECONN", "Could not confirm executor cron cancellation.") from exc
            reason = cancel_reason(cancel_event)
            break
        try:
            stdout, stderr = proc.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            continue
    before, separator, status = stderr.rpartition("\n" + receipt)
    if proc.returncode or not separator or not status.endswith("\n"):
        raise SshWorkspaceFsError("ECONN", "The SSH executor did not report a cron script result.")
    try:
        status, cwd = json.loads(status)
    except (TypeError, ValueError) as exc:
        raise SshWorkspaceFsError("EIO", "The SSH executor returned an invalid cron script result.") from exc
    return status, cwd, stdout, before, reason


def run_executor_script(script, workdir, interpreter, timeout, cancel_event=None, *, check_only=False, job=None):
    """Return the scheduled script result, with separate output and exit diagnostics."""
    if workdir:
        from cron.executor_workspace import executor_workdir

        verified = executor_workdir(workdir)
        if verified is not None:
            workdir = verified[0]
    argv, receipt, root = executor_script_command(script, workdir, interpreter, timeout, check_only=check_only)
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as incoming, os.fdopen(writer, "wb") as control:
        proc = subprocess.Popen(argv, stdin=incoming, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        incoming.close()
        try:
            status, cwd, stdout, stderr, reason = _read_script_result(proc, control, receipt, cancel_event)
        finally:
            with suppress(ProcessLookupError):
                if proc.poll() is None:
                    control.close()
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    if status == "cancelled":
        return False, f"Script cancelled: {reason}"
    if status == "timeout":
        return False, f"Script timed out after {timeout}s: {script}"
    if status == "invalid":
        return False, f"{stderr.strip()}. Expected a script under {root}."
    if status == 0 and isinstance(job, dict) and cwd:
        job["_delivery_cwd"] = cwd
    return status == 0, (stdout, stderr, int(status))
