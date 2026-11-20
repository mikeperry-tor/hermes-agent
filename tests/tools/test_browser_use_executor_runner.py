"""Real executor-owner subprocess cleanup, including inherited pipes after cancellation."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from tests.waiting import wait_until


@pytest.fixture
def runner(tmp_path):
    package = tmp_path / 'browser_harness'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'admin.py').write_text(
        'def ensure_daemon(): pass\n'
        'def restart_daemon():\n'
        ' import os,pathlib; pathlib.Path(os.environ["BH_HOME"],"cleaned").touch()\n')
    (package / 'helpers.py').write_text(
        'def cdp(*args, **kwargs): return {}\n'
        'def current_tab(): return {"targetId": "fixture-tab"}\n')
    (package / 'run.py').write_text(
        'def main():\n'
        ' import sys; exec(compile(sys.stdin.read(),"<stdin>","exec"),globals())\n')
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(Path.cwd())]))
    for key in ('BH_HOME', 'BH_RUNTIME_DIR', 'BH_TMP_DIR', 'BH_AGENT_WORKSPACE', 'BH_DOWNLOAD_DIR'):
        env[key] = str(tmp_path / key)
    proc = subprocess.Popen([sys.executable, '-u', '-c',
        'from tools.browser_use_executor_runner import main; main("def install(*a, **k): pass")'],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    results = queue.Queue()
    def collect():
        for line in proc.stdout:
            results.put(line)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        yield proc, results, tmp_path
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
        reader.join(timeout=10)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('stop', ['timeout', 'eof'])
def test_interruption_reaps_cli_without_draining_detached_descendant_pipes(runner, stop):
    proc, results, root = runner
    pidfile = root / 'descendant'
    stopfile = root / 'stop-descendant'
    descendant_code = (f'import pathlib,threading; stop=pathlib.Path({str(stopfile)!r}); event=threading.Event()\n'
                       'while not stop.exists(): event.wait(0.01)')
    code = (
        'import subprocess,sys,pathlib,threading\n'
        f'child=subprocess.Popen([sys.executable,"-c",{descendant_code!r}],start_new_session=True)\n'
        f'pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n'
        'threading.Event().wait()\n')
    proc.stdin.write(json.dumps({'code': code, 'setup': '', 'workspace': str(root / 'task'), 'timeout': 1 if stop == 'timeout' else 600}) + '\n')
    proc.stdin.flush()
    descendant = None
    try:
        assert wait_until(pidfile.exists, timeout=10)
        descendant = int(pidfile.read_text())
        if stop == 'eof':
            proc.stdin.close()
        response = json.loads(results.get(timeout=10))
        assert 'Do not replay automatically' in response['error']
        proc.wait(timeout=10)
        assert (root / 'BH_HOME' / 'cleaned').exists()
        # The unrelated session is still alive; receiving the result did not
        # depend on its inherited stdout/stderr reaching EOF.
        os.kill(descendant, 0)
    finally:
        # The detached fixture owns its exit, even after reparenting to init.
        stopfile.touch()


def test_runner_keeps_model_main_namespace_and_reports_model_tracebacks(runner):
    proc, results, root = runner
    code = 'import sys\nassert vars(sys.modules["__main__"]) is globals()\nraise RuntimeError("model-trace")'
    proc.stdin.write(json.dumps({'code': code, 'setup': '', 'workspace': str(root / 'task'), 'timeout': 600}) + '\n')
    proc.stdin.flush()
    result = json.loads(results.get(timeout=10))
    assert result['returncode'] != 0
    assert 'line 3' in result['stderr'] and 'model-trace' in result['stderr']
    assert result['target_id'] == 'fixture-tab'


def test_runner_uses_the_requested_task_workspace_on_one_transport(runner):
    proc, results, root = runner
    code = ('import os\n'
            'from pathlib import Path\n'
            'assert Path.cwd() == Path(os.environ["BH_AGENT_WORKSPACE"])\n'
            'marker = Path("scratch")\n'
            'print(marker.read_text() if marker.exists() else "empty")\n'
            'marker.write_text(Path.cwd().name)\n')
    for task, expected in (('first', 'empty'), ('second', 'empty'), ('first', 'first')):
        proc.stdin.write(json.dumps({'code': code, 'setup': '', 'workspace': str(root / task), 'timeout': 600}) + '\n')
        proc.stdin.flush()
        result = json.loads(results.get(timeout=10))
        assert result['returncode'] == 0, result
        assert result['stdout'].strip() == expected


@pytest.mark.parametrize('filename', ['relative.png', 'relative path.png', 'absolute path.png'])
def test_runner_attaches_screenshots_printed_from_the_model_workspace(runner, filename):
    proc, results, root = runner
    workspace = root / 'task'
    path = str(root / filename) if filename.startswith('absolute') else filename
    code = f'from pathlib import Path\nPath({path!r}).touch()\nprint({path!r})'
    request = {'code': code, 'setup': '', 'workspace': str(workspace), 'timeout': 10}
    proc.stdin.write(json.dumps(request) + '\n')
    proc.stdin.flush()
    result = json.loads(results.get(timeout=10))
    assert result['screenshot'] == str(workspace / path), result
