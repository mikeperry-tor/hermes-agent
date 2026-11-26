"""Read Git context from a verified executor checkout."""

from __future__ import annotations

import json

from hermes_cli._subprocess_compat import _GIT_CONFIG_OVERRIDES, harden_git_argv
from hermes_cli.ssh_project_probe import _PROJECT_PROBE_DEFS, _under_roots
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

_READ_COMMANDS = frozenset({"diff", "log", "status"})
_TIMEOUT = 30

_GIT_QUERY = _PROJECT_PROBE_DEFS + r"""
import re
import selectors
import time


def bounded_git(cwd, arguments, environment, deadline, limit):
    proc = subprocess.Popen(
        ['git', '--no-pager', '-C', cwd, *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )
    outputs = (bytearray(), bytearray())
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, 0)
            selector.register(proc.stderr, selectors.EVENT_READ, 1)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                ready = selector.select(max(0, remaining))
                if remaining <= 0 or not ready:
                    raise TimeoutError('Git timed out on the SSH executor.')
                for key, _ in ready:
                    block = os.read(key.fd, min(65536, limit + 1 - sum(map(len, outputs))))
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    outputs[key.data].extend(block)
                    if sum(map(len, outputs)) > limit:
                        raise ValueError('Git output exceeds the response size limit.')
        status = proc.wait(timeout=max(0, deadline - time.monotonic()))
        return status, *(output.decode('utf-8', 'replace') for output in outputs)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def overrides_for_executor(env, overrides):
    env['GIT_CONFIG_COUNT'] = str(len(overrides))
    for index, (name, value) in enumerate(overrides.items()):
        env[f'GIT_CONFIG_KEY_{index}'] = name
        env[f'GIT_CONFIG_VALUE_{index}'] = value


def filter_overrides(cwd, env, deadline):
    pattern = r'^(include(if\..*)?|filter\..+)\.'
    status, output, _ = bounded_git(
        cwd, ['config', '--no-includes', '--name-only', '-z', '--get-regexp', pattern],
        env, deadline, 32768,
    )
    if status == 1:
        return {}
    if status != 0:
        raise ValueError('Git filter discovery failed on the SSH executor.')
    keys = output.split('\0')[:-1]
    if len(keys) > 256 or any(key.startswith(('include.', 'includeif.')) for key in keys):
        raise ValueError('Git could not verify the checkout configuration on the SSH executor.')
    overrides = {}
    for key in keys:
        if re.fullmatch(r'filter\..+\.(?:clean|smudge|process)', key, flags=re.IGNORECASE):
            overrides[key] = ''
            overrides[key.rsplit('.', 1)[0] + '.required'] = 'false'
    return overrides


def check_git_storage(common, roots):
    for entry in ('config', 'config.worktree', 'objects', 'objects/info/alternates', 'objects/info/http-alternates'):
        path = common / entry
        if os.path.lexists(path) and not allowed(str(path.resolve(strict=True)), roots):
            raise ValueError('Git metadata resolves outside the applied SSH workspace roots.')
    if any(os.path.lexists(common / path) for path in ('objects/info/alternates', 'objects/info/http-alternates')):
        raise ValueError('Git alternate object stores are unavailable through the SSH workspace.')

def execute(request):
    cwd, roots, arguments, limit = (request[key] for key in ('cwd', 'roots', 'argv', 'limit'))
    roots = tuple(root for root in roots if os.path.isdir(root) and os.path.realpath(root) == root)
    if os.path.realpath(cwd) != cwd or not allowed(cwd, roots):
        raise ValueError('The checkout is unavailable on the SSH executor.')
    location = git_directory(cwd, roots)
    if location is None:
        raise ValueError('The selected directory has no usable Git checkout on the SSH executor.')
    root, common = location
    check_git_storage(common, roots)
    env = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
    env.update(
        GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
        GIT_CEILING_DIRECTORIES=os.path.dirname(root),
        GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', GIT_ALLOW_PROTOCOL='',
    )
    deadline = time.monotonic() + request['timeout']
    overrides = request['overrides']
    overrides_for_executor(env, overrides)
    status, output, _ = bounded_git(cwd, ['rev-parse', '--show-toplevel'], env, deadline, 32768)
    if status or output.rstrip('\n') != root:
        raise ValueError('Git could not verify the checkout on the SSH executor.')
    filters = filter_overrides(cwd, env, deadline)
    if not request.get('write'):
        overrides_for_executor(env, {**overrides, **filters})
    status, output, error = bounded_git(cwd, arguments, env, deadline, limit)
    return {'status': status, 'output': output, 'stderr': error}


try:
    print(json.dumps(execute(json.load(sys.stdin))))
except (OSError, subprocess.SubprocessError, ValueError) as exc:
    print(json.dumps({'error': str(exc)}))
"""


def run_executor_git(operations, cwd, roots, arguments, limit, *, write=False, timeout=_TIMEOUT):
    if not _under_roots(cwd, roots) or not arguments:
        raise SshWorkspaceFsError("EINVAL", "Git is outside the selected SSH workspace.")
    request = json.dumps({
        "cwd": cwd, "roots": roots, "argv": harden_git_argv(arguments), "limit": limit,
        "timeout": timeout, "overrides": _GIT_CONFIG_OVERRIDES, "write": write,
    })
    try:
        reply = json.loads(operations._framed_ssh_file_output(_GIT_QUERY, stdin_data=request))
    except (OSError, ValueError) as exc:
        raise SshWorkspaceFsError("EIO", "Could not run Git on the SSH executor.") from exc
    if isinstance(reply, dict) and isinstance(reply.get("error"), str):
        raise SshWorkspaceFsError("EIO", reply["error"])
    if (not isinstance(reply, dict) or type(reply.get("status")) is not int
            or not isinstance(reply.get("output"), str) or not isinstance(reply.get("stderr"), str)):
        raise SshWorkspaceFsError("EIO", "The SSH executor returned an invalid Git result.")
    return reply["status"], reply["output"], reply["stderr"]


def git_reference(operations, cwd, roots, arguments, limit):
    if not arguments or arguments[0] not in _READ_COMMANDS:
        raise SshWorkspaceFsError("EINVAL", "Unsupported Git reference.")
    status, output, error = run_executor_git(operations, cwd, roots, arguments, limit)
    if status:
        raise SshWorkspaceFsError("EIO", error.strip() or "Git failed on the SSH executor.")
    return output
