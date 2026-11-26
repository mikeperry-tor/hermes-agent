"""Managed cron script validation and Linux descendant cleanup."""

import os
import threading
import time

import pytest

from cron import executor_script


@pytest.fixture
def executor(monkeypatch, tmp_path):
    root = tmp_path / "cron" / "team" / "scripts"
    root.mkdir(parents=True)

    class Transport:
        def ssh_command(self):
            return ["/bin/sh", "-c"]

    monkeypatch.setattr(executor_script, "_script_endpoint", lambda: (Transport(), {}, None, (str(tmp_path),)))
    monkeypatch.setattr(executor_script, "scripts_root", lambda *_: str(root))
    return root


@pytest.mark.platforms("linux")
def test_remote_script_picks_executor_workdir_and_reports_status(executor, tmp_path):
    script = executor / "note.sh"
    script.write_text('printf "%s" "$PWD"; printf diagnostic >&2; exit 6\n')
    checkout = tmp_path / "project"
    checkout.mkdir()

    ok, result = executor_script.run_executor_script(script.name, str(checkout), None, 5)
    assert not ok
    stdout, stderr, status = result
    assert (stdout, stderr, status) == (str(checkout), "diagnostic", 6)

    foreign = tmp_path / "foreign.sh"
    foreign.write_text("exit 0")
    (executor / "escape.sh").symlink_to(foreign)
    ok, result = executor_script.run_executor_script("escape.sh", None, None, 5)
    assert not ok and str(executor) in result


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("cancel", [False, True])
def test_executor_timeout_and_cancel_kill_detached_descendants(executor, tmp_path, cancel):
    script = executor / "wait.sh"
    marker = tmp_path / "descendant"
    script.write_text(
        'setsid /bin/sh -c \'echo "$$" > "$1"; exec sleep 60\' runner '
        f'"{marker}" &\nwait\n'
    )
    stopped = threading.Event()

    def request_cancel():
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        stopped.set()

    controller = threading.Thread(target=request_cancel)
    if cancel:
        controller.start()
    try:
        ok, result = executor_script.run_executor_script(script.name, None, None, 5 if cancel else 1, stopped)
    finally:
        if cancel:
            controller.join()
    assert not ok
    assert ("cancelled" if cancel else "timed out") in result
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_scripts_root_maps_named_profile_and_rejects_unconfigured_base(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_constants.profile_name_for_home", lambda home: "team" if home.name == "team" else "default")
    assert executor_script.scripts_root({"cron": {"ssh_scripts_base": "/home/runner/.hermes/cron"}}, tmp_path / "team") == (
        "/home/runner/.hermes/cron/team/scripts"
    )
    assert executor_script.scripts_root({"cron": {"ssh_scripts_base": "/home/runner/.hermes/cron"}}, tmp_path / "default") == (
        "/home/runner/.hermes/cron/default/scripts"
    )
    with pytest.raises(ValueError, match="scripts base directory"):
        executor_script.scripts_root({}, tmp_path / "team")


def test_executor_workdir_validates_over_ssh_and_fails_closed(monkeypatch, tmp_path):
    from cron import executor_workspace
    from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

    monkeypatch.setattr("hermes_cli.main_resume_args.uses_managed_executor", lambda: True)
    monkeypatch.setattr("tools.terminal_scope.build_profile_terminal_scope", lambda _home: {"TERMINAL_ENV": "ssh"})
    monkeypatch.setattr(
        "hermes_cli.ssh_project_probe.inspect_managed_project",
        lambda _terminal, path, profile_key=None: ({"cwd": "/mnt/proj"}, {"TERMINAL_ENV": "ssh", "TERMINAL_CWD": "/mnt/proj"})
        if path == "/mnt/proj" else (_ for _ in ()).throw(SshWorkspaceFsError("EACCES", "outside roots")),
    )

    assert executor_workspace.executor_workdir("/mnt/proj", home=tmp_path) == (
        "/mnt/proj",
        {"TERMINAL_ENV": "ssh", "TERMINAL_CWD": "/mnt/proj"},
    )
    with pytest.raises(ValueError, match="unavailable on the SSH executor"):
        executor_workspace.executor_workdir("/etc", home=tmp_path)
