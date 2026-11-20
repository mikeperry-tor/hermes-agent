"""One executor-owned browser-harness session; EOF stops its CLI and daemon."""
import contextlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import stat
import subprocess
import sys
import threading
import time


_CHILD = """
import io, json, sys, types
request = json.load(sys.stdin)
shot = types.ModuleType("hermes_browser_shot")
exec(compile(request['shot'], '<hermes-browser-shot>', 'exec'), vars(shot))
import browser_harness.helpers as helpers
shot.install(helpers, activate="never")
helpers.cdp("Browser.setDownloadBehavior", behavior="allow",
            downloadPath=__import__("os").environ["BH_DOWNLOAD_DIR"])
import browser_harness.run as harness
harness.__name__ = "__main__"
sys.modules["__main__"] = harness
exec(compile(request['setup'], '<hermes-browser-setup>', 'exec'), vars(harness))
sys.stdin = io.StringIO(request['code'])
harness.main()
"""


def _printed_screenshot(stdout, workspace, started):
    """Recognize a printed path in the model's cwd, including spaces."""
    for line in reversed(stdout.splitlines()):
        # The helper result may occupy the entire line. Keep the existing
        # absolute-path scan for callers who print it alongside other text.
        matches = re.findall(r"""(/[^\s"']+?\.(?:png|jpe?g|webp))""", line, re.IGNORECASE)
        if line.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            matches.append(line)
        for candidate in reversed(matches):
            path = Path(workspace) / candidate
            try:
                info = path.stat()
            except (OSError, ValueError):
                continue
            if stat.S_ISREG(info.st_mode) and info.st_mtime >= started - 1:
                return str(path)
    return None


def main(shot_source):
    # Called through env -i, before importing the harness (which reads workspace .env).
    for key in ('BH_HOME', 'BH_RUNTIME_DIR', 'BH_TMP_DIR', 'BH_AGENT_WORKSPACE', 'BH_DOWNLOAD_DIR'):
        Path(os.environ[key]).mkdir(mode=0o700, parents=True, exist_ok=True)
    from browser_harness import admin
    requests = queue.Queue()
    closed = threading.Event()

    def receive():
        try:
            for line in sys.stdin:
                requests.put(json.loads(line))
        finally:
            closed.set()
            requests.put(None)

    threading.Thread(target=receive, daemon=True).start()  # health: allow HX012 -- remote stdin reader has no Hermes imports or profile scope
    proc = None
    try:
        while not closed.is_set():
            request = requests.get()
            if request is None:
                break
            started = time.time()
            try:
                admin.ensure_daemon()
                if closed.is_set():
                    raise RuntimeError('Browser session closed during startup; model code was not launched')
                workspace = request['workspace']
                Path(workspace).mkdir(mode=0o700, parents=True, exist_ok=True)
                command = [sys.executable, '-c', _CHILD]
                proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, start_new_session=True,  # detached-spawn: ok — awaited; EOF/timeout kills this exact POSIX group
                                        cwd=workspace, env={**os.environ, 'BH_AGENT_WORKSPACE': workspace})
                deadline = time.monotonic() + request['timeout']
                incoming = json.dumps({**request, 'shot': shot_source})
                while True:
                    try:
                        stdout, stderr = proc.communicate(input=incoming, timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        incoming = None
                        if closed.is_set() or time.monotonic() >= deadline:
                            os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — executed only inside POSIX terminal sandboxes
                            proc.wait()  # health: allow HX006 -- SIGKILL already sent to this child group; reap only, never drain inherited pipes
                            proc.stdout.close()
                            proc.stderr.close()
                            raise TimeoutError('Browser execution interrupted or timed out; already-issued '
                                               'browser actions may have completed. Do not replay automatically.')
                if closed.is_set():
                    raise RuntimeError('Browser session closed; already-issued actions may have completed. '
                                       'Do not replay automatically.')
                result = dict(returncode=proc.returncode, stdout=stdout, stderr=stderr)
                from browser_harness.helpers import current_tab
                with contextlib.suppress(OSError, RuntimeError):
                    result['target_id'] = current_tab().get('targetId')
                # These are executor paths. The gateway must fetch, never open them locally.
                if screenshot := _printed_screenshot(stdout, workspace, started):
                    result['screenshot'] = screenshot
            except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
                result = {'error': str(error)}
                # A timeout ends this identity rather than reusing uncertain in-flight CDP.
                closed.set()
            finally:
                proc = None
            print(json.dumps(result), flush=True)
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — executed only inside POSIX terminal sandboxes
            proc.wait()  # health: allow HX006 -- SIGKILL already sent to this child group; reap only, never drain inherited pipes
        admin.restart_daemon()
