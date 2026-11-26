"""POST /api/fs/write-text keeps the guards the spot editor's reads apply (#95306).

The endpoint is the save half of read-text, and it serves remote dashboard sessions too. It used to
write any resolvable path: a credential store read-text refuses, a file outside a locked
managed-files root, or ``~/.ssh/authorized_keys``.
"""

import ctypes
import io
import os
import sys

import pytest

from hermes_cli import web_server
from hermes_cli.ssh_workspace_fs import SshWorkspaceFs
from hermes_cli.web_routers import files as file_routes

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client():
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        test_client.close()
        if previous is None:
            if hasattr(web_server.app.state, "auth_required"):
                delattr(web_server.app.state, "auth_required")
        else:
            web_server.app.state.auth_required = previous


def _save(client, path, content="tampered", **params):
    return client.post("/api/fs/write-text", params=params, json={"path": str(path), "content": content})


@pytest.mark.parametrize("relative", [".env", ".ENV", ".env.local", "auth.json", "config.yaml", "mcp-tokens/github.json"])
def test_write_text_refuses_the_files_read_text_refuses(client, tmp_path, relative):
    existing = tmp_path / "existing" / relative
    existing.parent.mkdir(parents=True)
    existing.write_text("original")
    created = tmp_path / "created" / relative
    created.parent.mkdir(parents=True)

    assert client.get("/api/fs/read-text", params={"path": str(existing)}).status_code == 403
    assert _save(client, existing).status_code == 403
    assert existing.read_text() == "original"
    assert _save(client, created).status_code == 403
    assert not created.exists()
    assert not [p for p in existing.parent.iterdir() if p.name != existing.name]  # no staged temp left


@pytest.mark.platforms("posix")
def test_write_text_judges_a_link_by_the_file_it_names(client, tmp_path):
    store = tmp_path / "home" / "auth.json"
    store.parent.mkdir()
    store.write_text("{}")
    link = tmp_path / "notes.md"
    link.symlink_to(store)

    assert _save(client, link).status_code == 403
    assert store.read_text() == "{}"
    assert link.is_symlink()


def test_write_text_refuses_the_agents_write_denied_credentials(client, tmp_path, monkeypatch):
    home = tmp_path / "user"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    keys = home / ".ssh" / "authorized_keys"

    assert _save(client, keys, "ssh-ed25519 AAAA attacker").status_code == 403
    assert not keys.exists()


def test_write_text_stays_inside_a_locked_files_root(client, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("HERMES_DASHBOARD_FILES_ROOT", str(managed))

    refused = _save(client, outside / "run.sh", "#!/bin/sh\n")
    saved = _save(client, managed / "notes.md", "kept")

    assert refused.status_code == 403
    assert not (outside / "run.sh").exists()
    assert saved.status_code == 200, saved.text
    assert (managed / "notes.md").read_text() == "kept"


def test_write_text_never_hands_a_credential_path_to_the_ssh_workspace(client, monkeypatch):
    """The guard judges the path the SSH adapter writes, so padding it with whitespace (which the
    adapter strips) or ``..`` still lands on the refusal."""
    sent = []

    class SshMachine:
        cwd = "/srv/app"
        timeout = 30
        _remote_home = "/home/dev"

        def execute(self, command, **_kwargs):
            sent.append(command)
            return {"returncode": 0, "output": ""}

    monkeypatch.setattr(file_routes, "_fs_backend", lambda profile=None: SshWorkspaceFs(SshMachine()))

    for path in (
        "/srv/app/.env",
        "/srv/app/sub/../mcp-tokens/x.json",
        "/srv/app/config.yaml",
        "/srv/app/.env ",
        " /srv/app/auth.json\n",
        "/srv/app/.env\t",
        "mcp-tokens/x.json ",
        # ``..`` exposes whitespace a first normalization left inside a component.
        "/srv/app/.env /x/..",
        "/srv/app/.env\t/a/..",
        "/srv/app/auth.json\n/b/..",
        "/srv/app/config.yaml\x1f/q/..",
    ):
        response = _save(client, path, profile="remote-dev")
        assert response.status_code == 403, (repr(path), response.text)
    assert sent == []
    saved = _save(client, "/srv/app/README.md ", profile="remote-dev")
    assert (saved.status_code, saved.json()["path"]) == (200, "/srv/app/README.md")
    assert len(sent) == 1


@pytest.mark.platforms("linux")
def test_managed_ssh_editor_uses_the_shared_publisher(client, tmp_path, monkeypatch, capsys):
    from tools.file_operations import ShellFileOperations
    from tools.file_operations_ssh_acl import SSH_PROJECT_ACL

    project = tmp_path / "project"
    project.mkdir()
    sibling = tmp_path / "project-sibling"
    sibling.mkdir()
    retired = tmp_path / "retired"
    retired.symlink_to(sibling, target_is_directory=True)
    executor_home = tmp_path / "executor-home"
    executor_home.mkdir()
    monkeypatch.setenv("HOME", str(executor_home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    configured = {"terminal": {
        "backend": "ssh", "ssh_host": "executor", "ssh_user": "runner", "ssh_port": 22,
        "ssh_key": "/key", "ssh_config": "/known-hosts", "ssh_workspace_roots": [str(project), str(retired)],
    }}
    monkeypatch.setattr("hermes_cli.managed_scope.load_managed_config", lambda **_: configured)

    class SshMachine:
        cwd = str(project)

    monkeypatch.setattr(
        "hermes_cli.web_routers.session_files.managed_project_connection",
        lambda *_: SshWorkspaceFs(SshMachine()),
    )
    acl = {"ctypes": ctypes, "WriteError": RuntimeError}
    exec(SSH_PROJECT_ACL, acl)
    try:
        libacl = acl["acl_library"]()
    except RuntimeError:
        pytest.skip("libacl is unavailable")

    def execute(_ops, snippet, *, stdin_data=None):
        source = io.TextIOWrapper(io.BytesIO((stdin_data or "").encode("ascii")))
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", source)
            env = {}
            exec(compile(snippet, "<ssh executor>", "exec"), env)
        return capsys.readouterr().out

    monkeypatch.setattr(ShellFileOperations, "_framed_ssh_file_output", execute)
    executable = project / "run.sh"
    executable.write_text("first")
    executable.chmod(0o755)
    with executable.open("rb") as initial:
        initial_acl = acl["acl_rows"](libacl, initial.fileno())
    assert _save(client, executable, "second").status_code == 200
    assert executable.read_text() == "second" and executable.stat().st_mode & 0o777 == 0o755
    with executable.open("rb") as updated:
        assert acl["acl_rows"](libacl, updated.fileno()) == initial_acl
    assert _save(client, project / "new.txt", "third").status_code == 200
    assert (project / "new.txt").read_text() == "third"
    forbidden = sibling / "ungranted.txt"
    assert _save(client, forbidden).status_code == 403 and not forbidden.exists()
    assert _save(client, retired / forbidden.name).status_code == 403
    assert not forbidden.exists()
    missing = project / "missing-parent" / "new.txt"
    assert _save(client, missing).status_code == 404 and not missing.parent.exists()
    assert _save(client, project).status_code == 400
    credential = project / ".env.local"
    credential.write_text("existing")
    alias = project / "notes.txt"
    alias.symlink_to(credential)
    assert _save(client, alias).status_code == 403 and credential.read_text() == "existing"

    def race(ops, snippet, *, stdin_data=None):
        if "\nprint(json.dumps(publish(" in snippet:
            executable.write_text("outside editor")
        return execute(ops, snippet, stdin_data=stdin_data)

    monkeypatch.setattr(ShellFileOperations, "_framed_ssh_file_output", race)
    assert _save(client, executable, "retry later").status_code == 409
    assert executable.read_text() == "outside editor"

    def change_before_snapshot(ops, snippet, *, stdin_data=None):
        if "\nprint(json.dumps(write_digest(" in snippet:
            executable.write_text("changed before snapshot")
        return execute(ops, snippet, stdin_data=stdin_data)

    monkeypatch.setattr(ShellFileOperations, "_framed_ssh_file_output", change_before_snapshot)
    assert _save(client, executable).status_code == 409
    assert executable.read_text() == "changed before snapshot"


def test_write_text_still_saves_an_ordinary_file(client, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "notes.md"
    target.write_text("old")

    response = _save(client, target, "new")

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "path": str(target), "byteSize": 3}
    assert target.read_text() == "new"
    assert os.listdir(project) == ["notes.md"]


def test_write_text_judges_a_named_profile_save_by_that_profiles_protected_state(client, tmp_path, monkeypatch):
    """``?profile=work`` saves under work's scope: its ``state.db`` and ``sessions/`` are protected
    exactly as the launch profile's are, not just ``<root>/state.db``."""
    root = tmp_path / ".hermes"
    work = root / "profiles" / "work"
    (work / "sessions").mkdir(parents=True)
    (work / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_DASHBOARD_FILES_ROOT", raising=False)
    state_db = work / "state.db"
    state_db.write_text("original")
    transcript = work / "sessions" / "s1.jsonl"

    assert _save(client, state_db, profile="work").status_code == 403
    assert state_db.read_text() == "original"
    assert _save(client, transcript, profile="work").status_code == 403
    assert not transcript.exists()
    notes = work / "notes.md"
    saved = _save(client, notes, "kept", profile="work")
    assert saved.status_code == 200, saved.text
    assert notes.read_text() == "kept"


def test_write_text_refuses_the_approval_gated_ssh_config(client, tmp_path, monkeypatch):
    """The file tools gate ~/.ssh/config on an approval prompt; the editor save has none."""
    home = tmp_path / "user"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config = home / ".ssh" / "config"

    assert _save(client, config, "Host *\n  ProxyCommand sh -c id\n").status_code == 403
    assert not config.exists()


@pytest.mark.parametrize("path", [
    r"\??\UNC\example.invalid\share\notes.txt",
    r"\\?\UNC\example.invalid\share\notes.txt",
    r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\notes.txt",
    r"\\.\PhysicalDrive0",
    r" \\?\UNC\example.invalid\share\notes.txt",
])
def test_write_text_refuses_nt_namespaces_before_any_path_resolution(client, monkeypatch, path):
    """Raw NT namespace input must be rejected before a Windows filesystem path is touched."""
    def unexpected_fs_access(*_args, **_kwargs):
        raise AssertionError("NT namespace input reached filesystem path inspection")

    monkeypatch.setattr(file_routes, "_remote_fs_backend", unexpected_fs_access)
    response = _save(client, path)
    assert response.status_code == 403, response.text


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("url", [
    "file:////%3F/UNC/example.invalid/share/notes.txt",
    "file:////./PhysicalDrive0",
])
def test_write_text_refuses_a_file_url_naming_an_nt_namespace(client, monkeypatch, url):
    """On Windows these URLs decode to NT namespace paths: judge the decoded path, not the URL."""
    def unexpected_fs_access(*_args, **_kwargs):
        raise AssertionError("NT namespace input reached filesystem path inspection")

    monkeypatch.setattr(file_routes, "_remote_fs_backend", unexpected_fs_access)
    response = _save(client, url)
    assert response.status_code == 403, response.text


@pytest.mark.platforms("posix")
def test_project_picker_creates_a_folder_only_on_the_selected_ssh_backend(client, tmp_path, monkeypatch, capsys):
    from tools.file_operations import ShellFileOperations

    executor_home = tmp_path / "executor-home"
    hermes_home = executor_home / ".hermes"
    profile_dir = hermes_home / "profiles" / "team-dev"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(executor_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    remote = tmp_path.resolve() / "project"
    remote.mkdir()
    sibling = remote.with_name("project-sibling")
    sibling.mkdir()
    (remote / "alias").symlink_to(sibling, target_is_directory=True)
    (remote / "existing").write_text("keep")
    configured = {
        "terminal": {
            "backend": "ssh",
            "ssh_host": "executor",
            "ssh_user": "runner",
            "ssh_port": 2222,
            "ssh_key": "/etc/hermes/ssh_key",
            "ssh_config": "/etc/hermes/ssh_config",
            "ssh_workspace_roots": [str(remote)],
        }
    }
    monkeypatch.setattr("tools.file_tools_paths.load_managed_config", lambda **_: configured)
    monkeypatch.setattr("hermes_cli.managed_scope.load_managed_config", lambda **_: configured)
    terminal_scope = {"TERMINAL_ENV": "ssh"}
    monkeypatch.setattr("tools.terminal_scope.build_profile_terminal_scope", lambda *_a, **_k: dict(terminal_scope))

    class SshMachine:
        cwd = str(remote)

    def connection(_terminal, home):
        assert os.path.basename(home) == "team-dev"
        return SshWorkspaceFs(SshMachine())

    def execute(_ops, snippet):
        exec(compile(snippet, "<ssh executor>", "exec"), {})
        return capsys.readouterr().out

    monkeypatch.setattr("hermes_cli.ssh_project_probe.managed_project_connection", connection)
    monkeypatch.setattr(ShellFileOperations, "_framed_ssh_file_output", execute)

    def create(path):
        return client.post("/api/fs/mkdir", params={"profile": "team-dev"}, json={"path": str(path)})

    created = remote / "new project"
    assert create(created).json() == {"ok": True, "path": str(created)}
    assert create(created).status_code == 200
    assert created.is_dir()
    assert create(remote / "existing").status_code == 409
    assert create(remote / ".env").status_code == 403
    assert create(sibling / "new").status_code == 403
    assert create(remote / "alias" / "new").status_code == 400
    assert not (sibling / "new").exists()
    assert create(remote / "missing" / "new").status_code == 400
    assert not (remote / "missing").exists()

    def stopped(_ops, _snippet):
        raise ValueError("missing SSH response framing")

    monkeypatch.setattr(ShellFileOperations, "_framed_ssh_file_output", stopped)
    assert create(remote / "offline").status_code == 503
    assert not (remote / "offline").exists()

    # Removing the backend or losing the applied policy cannot select a local writer.
    terminal_scope.clear()
    assert create(remote / "no-backend").status_code == 503
    assert not (remote / "no-backend").exists()
    terminal_scope["TERMINAL_ENV"] = "ssh"
    configured["terminal"]["ssh_workspace_roots"] = []
    refused = create(remote / "no-roots")
    assert refused.status_code == 503
    assert "SSH workspace is unavailable" in refused.json()["detail"]
    assert not (remote / "no-roots").exists()
