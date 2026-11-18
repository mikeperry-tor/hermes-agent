"""Browser Python on the terminal executor owning the selected sandbox browser."""
from dataclasses import dataclass, field
from contextlib import ExitStack, contextmanager, suppress
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import queue
import subprocess
import threading
import time
import uuid


@dataclass
class Session:
    env: object
    process: subprocess.Popen
    home: str
    workspace_root: str
    endpoint: str
    browser: dict
    lock: object = field(default_factory=threading.Lock)
    responses: queue.Queue = field(default_factory=queue.Queue)
    supervisors: dict = field(default_factory=dict)


_sessions = {}
_key_locks: dict[str, threading.Lock] = {}
# Lock hierarchy: _key_lock(key) -> _lock and session.lock -> _lock. Never acquire
# _key_lock(key) or session.lock while holding _lock, and keep slow remote probes
# and process spawns outside _lock so one cold session never stalls other keys.
_lock = threading.Lock()
_HARNESS_PROBE_TIMEOUT_S = 4.0
# Registry check_fn probes are abandoned after 10s; discovery uses one absolute budget shared
# with sandbox_host so env creation plus sequential probes cannot sum past the ceiling.
_DISCOVERY_BUDGET_S = 8.0


def _key_lock(key: str) -> threading.Lock:
    with _lock:
        return _key_locks.setdefault(key, threading.Lock())


def task_supervisor(task_id):
    """Return the registered supervisor only while a current executor session owns it."""
    from hermes_constants import hermes_home_key
    from tools.bot_desktop import runtime
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    home = hermes_home_key()
    with _lock:
        supervisor = SUPERVISOR_REGISTRY.get(task_id)
        if supervisor is not None and any(
            current.home == home and current.supervisors.get(task_id) is supervisor
            and current.process.poll() is None and runtime.owns_tool_sandbox(current.env)
            for current in _sessions.values()
        ):
            return supervisor
    return None


def close(cache_key=None, home_key=None, session_info=None, owner=None):
    from agent.memory_provider import spawn_context_thread
    from hermes_cli.process_identity import release_process_group_once_empty
    with _lock:
        keys = [key for key, value in _sessions.items()
                if (cache_key is None or key == cache_key) and
                (home_key is None or value.home == home_key) and
                (session_info is None or value.browser is session_info) and
                (owner is None or value is owner)]
        sessions = [_sessions.pop(key) for key in keys]
        supervisors = [_detach_task_supervisors(session.supervisors) for session in sessions]
    for session, owned_supervisors in zip(sessions, supervisors):
        proc = session.process
        if proc.stdin:
            # A failed flush can leave a buffered write that close retries.
            with suppress(BrokenPipeError):
                proc.stdin.close()  # Remote EOF stops its CLI group and native daemon.
        # Native daemon shutdown can drain in-flight CDP. Do not kill the SSH
        # owner early and thereby lose the executor's EOF cleanup channel.
        spawn_context_thread(_reap, name="browser-executor-reap",
                             args=(proc, release_process_group_once_empty, owned_supervisors)).start()


def _detach_task_supervisors(owners):
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    detached = []
    for task_id, supervisor in owners.items():
        if any(current.supervisors.get(task_id) is supervisor for current in _sessions.values()):
            continue
        if SUPERVISOR_REGISTRY.detach(task_id, expected=supervisor) is supervisor:
            detached.append(supervisor)
    return detached


def _reap(proc, release, supervisors):
    for supervisor in supervisors:
        with suppress(Exception):
            supervisor.stop()
    proc.wait()
    try:
        proc.stdout.close()
    finally:
        release(proc.pid, timeout=0)


@contextmanager
def _attach_task_supervisor(current, key, cdp_url, task_id, check_dispatch):
    from tools.browser_supervisor import SupervisorEndpointConflict
    from tools.browser_use_cli import _supervise_browser_command

    @contextmanager
    def admission():
        check_dispatch()
        with _lock:
            if _sessions.get(key) is not current:
                raise RuntimeError('Executor browser closed before supervisor attach')
            yield

    task_key = task_id or 'default'
    try:
        with _supervise_browser_command(cdp_url, task_id, admission=admission, local_browser=True) as supervisor:
            with _lock:
                attached = _sessions.get(key) is current
                if attached and supervisor is not None:
                    current.supervisors[task_key] = supervisor
                detached = _detach_task_supervisors({task_key: supervisor}) if not attached and supervisor else ()
            for old_supervisor in detached:
                old_supervisor.stop()
            if not attached:
                raise RuntimeError('Executor browser closed before dispatch; retry the command')
            yield supervisor
    except SupervisorEndpointConflict:
        check_dispatch()
        raise


def _validated_endpoint(key, env, info, expected=None):
    from tools.bot_desktop import runtime, sandbox_host
    from tools.environments import streams
    from tools.browser_session_lifecycle import cleanup_single_session
    from tools.browser_session_sandbox import GONE, _live_devtools, _shared_key

    if not runtime.owns_tool_sandbox(env):
        raise RuntimeError("The browser executor changed; start a browser on the recorded desktop")

    user = sandbox_host._user_for(env)
    udd = sandbox_host.browser_profile_dir(env)
    if info.get('launcher_key') != _shared_key(env, udd):
        raise RuntimeError('The browser executor changed; retry the command')
    found = _live_devtools(env, user, udd)
    if found is None:
        raise RuntimeError('Executor Chromium has no usable DevTools endpoint')
    if found is not GONE:
        port, path = found
        endpoint = f'ws://127.0.0.1:{port}{path}'
        forwarded = streams.forward_port(env, port, user=user)
        if (expected is None or endpoint == expected) and info.get('cdp_url') == f'ws://127.0.0.1:{forwarded}{path}':
            return endpoint
    cleanup_single_session(key, expected=info)
    raise RuntimeError('Executor Chromium changed during setup; retry the command')


def _session(key):
    from agent.memory_provider import spawn_context_thread
    from hermes_constants import hermes_home_key
    from hermes_cli.process_identity import own_process_groups
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    from tools.browser_session import get_session_info
    from tools.browser_session_cloud import browser_section
    from tools.browser_session_sandbox import _sandbox

    with _key_lock(key):
        info = get_session_info(key)
        if not (info.get('features') or {}).get('sandbox'):
            raise RuntimeError('The selected browser no longer belongs to a terminal sandbox')
        env, published = _sandbox()
        with _lock:
            current = _sessions.get(key)
            if current and current.process.poll() is None and current.env is env and current.browser is info:
                return current
        user = sandbox_host._user_for(env)
        endpoint = _validated_endpoint(key, env, info)
        close(cache_key=key)
        # A replacement must never share IPC with an owner still draining its
        # native shutdown after cancellation.
        digest = uuid.uuid4().hex[:20]
        home = sandbox_host.desktop_home(env)
        workspace_root = browser_section().get('executor_workspace') or home.rstrip('/') + '/.hermes/browser-use'
        if not isinstance(workspace_root, str) or not PurePosixPath(workspace_root).is_absolute():
            raise ValueError('browser.executor_workspace must be an absolute path inside the executor')
        workspace_root = workspace_root.rstrip('/')
        root = streams.sandbox_temp_dir(env).rstrip('/') + '/bh/' + digest
        child_env = {
            'HOME': home, 'PATH': '/usr/local/bin:/usr/bin:/bin',
            'LANG': 'C.UTF-8', 'BH_HOME': root, 'BH_CONFIG_DIR': root,
            'BH_RUNTIME_DIR': root + '/r', 'BH_TMP_DIR': root + '/t',
            'BH_AGENT_WORKSPACE': root, 'BU_NAME': 's' + digest,
            'BH_DOWNLOAD_DIR': workspace_root + '/downloads',
            'BU_CDP_WS': endpoint, 'BU_AUTOSPAWN': '', 'BH_REQUIRE_EXISTING_DAEMON': '1',
            'BH_TELEMETRY': '0', 'BROWSER_HARNESS_TELEMETRY': '0', 'ANONYMIZED_TELEMETRY': 'false',
            'BH_UPDATE_CHECK': '0', 'BH_RECORD': '0',
            **{k: v for k, v in published.items() if k in ('DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS')},
        }
        python = _executor_python()
        runner = Path(__file__).with_name('browser_use_executor_runner.py').read_text(encoding='utf-8-sig')
        shot = Path(__file__).with_name('browser_use_shot.py').read_text(encoding='utf-8-sig')
        program = runner + '\nmain(' + repr(shot) + ')'
        argv = ['env', '-i', *(f'{k}={v}' for k, v in child_env.items()), python, '-u', '-c', program]
        command = streams.remote_command(env, argv, user=user)
        if command is None:
            raise RuntimeError('Terminal executor stream is unavailable')
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)  # detached-spawn: ok — handed to own_process_groups below; EOF cleans the remote owner
        own_process_groups([proc.pid])
        current = Session(env, proc, hermes_home_key(), workspace_root, endpoint, info)
        spawn_context_thread(_receive, name="browser-executor-output", args=(current,)).start()
        with _lock:
            _sessions[key] = current
        return current


def _executor_python():
    from tools.browser_session_cloud import browser_section

    python = browser_section().get('executor_python', 'python3')
    if not isinstance(python, str) or not python:
        raise ValueError('browser.executor_python must name the executor Python with browser-harness installed')
    return python


def _probe_harness(env, python: str, *, probe_timeout: float = _HARNESS_PROBE_TIMEOUT_S,
                   deadline=None) -> bool:
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    cache = getattr(env, '_bd_browser_harness', None)
    if isinstance(cache, dict) and python in cache:
        return cache[python]
    user = sandbox_host._user_for(env, timeout=sandbox_host.probe_budget(deadline, probe_timeout))
    # One round-trip: bash supplies the account HOME and the interpreter path, so cold executors
    # do not pay separate user/home/import probes against the discovery budget.
    probe = streams.run_in(
        env, ['bash', '-c', 'PATH=/usr/local/bin:/usr/bin:/bin:$PATH exec "$0" -c '
              '"import browser_harness.run; import browser_harness.admin"', python],
        user=user, timeout=sandbox_host.probe_budget(deadline, probe_timeout))
    ok = probe.returncode == 0
    if isinstance(cache, dict):
        cache[python] = ok
    else:
        with suppress(AttributeError, TypeError):
            env._bd_browser_harness = {python: ok}
    return ok


def available(*, require_ready=False, env=None, probe_timeout: float = _HARNESS_PROBE_TIMEOUT_S,
              deadline=None):
    from tools.bot_desktop import placement, sandbox_host

    try:
        if deadline is None:
            deadline = time.monotonic() + _DISCOVERY_BUDGET_S
        python = _executor_python()
        if env is None:
            env = (sandbox_host.ready_cua_executor(probe_timeout=probe_timeout, deadline=deadline)
                   if require_ready else placement.terminal_environment(create=False))
        if env is None:
            return not require_ready
        return _probe_harness(env, python, probe_timeout=probe_timeout, deadline=deadline)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False


def _receive(session):
    try:
        for line in session.process.stdout:
            session.responses.put(line)
    finally:
        session.responses.put(None)


def execute(key, code, session='', timeout_s=300, task_id=None):
    from agent.redact import redact_sensitive_text
    from tools.registry import tool_error, tool_result
    from tools import browser_use_cli as native
    from tools.browser_session import run_fenced_dispatch
    from tools.browser_session_lifecycle import update_session_activity
    from tools.interrupt import is_interrupted
    try:
        if not isinstance(code, str) or not code.strip():
            raise RuntimeError('No Python code provided')
        if session and not native._SESSION_RE.fullmatch(session):
            raise RuntimeError('Invalid session name: use 1-64 letters, digits, dashes or underscores')

        def dispatch(check_lease):
            if is_interrupted():
                raise RuntimeError('Browser execution interrupted before launch')
            current = _session(key)
            identity = json.dumps([current.home, task_id or 'default']).encode()
            workspace = current.workspace_root + '/' + sha256(identity).hexdigest()[:20]

            def check_dispatch():
                if is_interrupted():
                    raise RuntimeError('Browser execution interrupted before launch')
                check_lease()

            with current.lock, ExitStack() as command:
                proc = current.process
                check_dispatch()
                supervisor = None
                if cdp_url := current.browser.get('cdp_url'):
                    _validated_endpoint(key, current.env, current.browser, current.endpoint)
                    check_dispatch()
                    supervisor = command.enter_context(
                        _attach_task_supervisor(current, key, cdp_url, task_id, check_dispatch))
                _validated_endpoint(key, current.env, current.browser, current.endpoint)
                check_dispatch()
                try:
                    proc.stdin.write((json.dumps({'code': code, 'setup': native._OWN_TAB_PREAMBLE if session else '',
                                                  'workspace': workspace,
                                                  'timeout': native._clamp_timeout(timeout_s)}) + '\n').encode())
                    proc.stdin.flush()
                    if supervisor is not None:
                        supervisor.set_driven_target(None)
                    # The reader thread owns buffered stdout; the owner can
                    # still observe cancellation while a response is pending.
                    touched = 0.0
                    while True:
                        if time.monotonic() - touched >= 1:
                            from tools.bot_desktop import runtime
                            update_session_activity(key)
                            runtime.touch_activity()
                            touched = time.monotonic()
                        if is_interrupted():
                            raise RuntimeError('Browser execution interrupted; already-issued actions may have '
                                               'completed. Do not replay automatically.')
                        try:
                            data = current.responses.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        if data is None:
                            raise RuntimeError('Browser executor session ended; check its connection and the '
                                               'browser-harness installation. Action outcome may be unknown. '
                                               'Do not replay automatically.')
                        break
                    response = json.loads(data)
                    if supervisor is not None:
                        supervisor.set_driven_target(response.get('target_id'))
                    if 'error' in response:
                        raise RuntimeError(response['error'])
                    result = {'success': response['returncode'] == 0, 'exit_code': response['returncode'],
                              'output': redact_sensitive_text(response['stdout'], force=True),
                              'workspace': workspace, 'downloads': current.workspace_root + '/downloads'}
                    result.update(native._take_real_profile_note(native._backend_cache_key(task_id, session)))
                    if session:
                        result['session'] = session
                    if response.get('stderr'):
                        result['stderr'] = redact_sensitive_text(response['stderr'], force=True)[:native._STDERR_CAP_CHARS]
                    if response.get('screenshot'):
                        from tools.image_source import _MAX_INGEST_BYTES
                        from hermes_constants import get_hermes_home
                        target = Path(get_hermes_home()) / 'cache' / 'browser-use' / 'screenshots'
                        target.mkdir(parents=True, exist_ok=True)
                        target = target / (uuid.uuid4().hex + '.png')
                        try:
                            current.env.fetch_file(response['screenshot'], target, max_bytes=_MAX_INGEST_BYTES)
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                        result['screenshot_path'] = str(target)
                        attached = native._native_screenshot_result(result, str(target))
                        if attached is not None:
                            return attached
                    return result
                except BaseException:
                    close(cache_key=key, owner=current)
                    raise

        result = run_fenced_dispatch(dispatch)
        return result if result.get('_multimodal') else tool_result(result)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return tool_error(redact_sensitive_text(str(error), force=True))
