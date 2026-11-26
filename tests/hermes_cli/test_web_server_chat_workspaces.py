"""Dashboard chat workspace picker: ``GET /api/chat/workspaces`` + ``/api/pty?cwd=``.

A fresh dashboard chat can be aimed at a host directory (the same projects/repos the
Desktop sidebar lists); the pick reaches the TUI as ``HERMES_CWD``/``HERMES_TUI_CWD`` and a
dead path fails closed instead of silently landing in the dashboard's launch dir.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from hermes_cli import web_server, web_server_chat
from hermes_cli.web_routers import chat_workspaces

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
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


def test_workspaces_lists_projects_and_discovered_repos(client, tmp_path):
    """Explicit project folders AND the discovery cache both surface; the payload names the
    default cwd a chat lands in when nothing is picked."""
    from hermes_cli import projects_db as pdb

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    repo_dir = tmp_path / "scanned"
    repo_dir.mkdir()
    with pdb.connect_closing() as conn:
        pdb.create_project(conn, name="Demo", folders=[str(proj_dir)])
        pdb.record_discovered_repos(conn, [(str(repo_dir), "scanned")], replace=True, policy_key="k")

    body = client.get("/api/chat/workspaces").json()

    assert [p["name"] for p in body["projects"]] == ["Demo"]
    assert body["projects"][0]["folders"][0]["path"] == str(proj_dir)
    assert str(repo_dir) in {r["root"] for r in body["repos"]}
    assert Path(body["default_cwd"]).is_dir()
    assert body["home"] == str(Path.home())


def test_resolve_chat_cwd_fails_closed_and_reaches_the_tui_env(monkeypatch, tmp_path):
    """A picked directory becomes HERMES_CWD + HERMES_TUI_CWD on the PTY child; a missing one is a
    400 (never the launch-dir fallback); unset means no override."""
    import hermes_cli.main_tui_launch as tui_launch

    monkeypatch.setattr(
        tui_launch, "_make_tui_argv", lambda *_a, **_k: (["node", "fake-tui.js"], tmp_path))
    monkeypatch.setattr(tui_launch, "_apply_tui_python_env", lambda _env: None)

    assert chat_workspaces.resolve_chat_cwd("") is None
    with pytest.raises(HTTPException) as exc:
        chat_workspaces.resolve_chat_cwd(str(tmp_path / "gone"))
    assert exc.value.status_code == 400

    picked = chat_workspaces.resolve_chat_cwd(str(tmp_path))
    _argv, _cwd, env = web_server_chat._resolve_chat_argv(workspace_cwd=picked)
    assert env["HERMES_CWD"] == str(tmp_path)
    assert env["HERMES_TUI_CWD"] == str(tmp_path)

    _argv, _cwd, env = web_server_chat._resolve_chat_argv()
    assert "HERMES_TUI_CWD" not in env


def test_managed_pty_passes_the_executor_selection_as_data(monkeypatch, tmp_path):
    from hermes_cli import main_resume_args, main_tui_launch
    from tui_gateway import managed_project_queries

    selected = "/workspace/only on executor"
    access = (("/workspace",), {})
    monkeypatch.setattr(managed_project_queries, "profile_project_access", lambda: access)
    monkeypatch.setattr(managed_project_queries, "current_project_facts", lambda _access, _paths: {
        selected: {"cwd": selected, "branch": "executor"},
    })
    monkeypatch.setattr(main_resume_args, "uses_managed_executor", lambda: True)
    monkeypatch.setattr(main_tui_launch, "_make_tui_argv", lambda *_a, **_k: (["node", "fake-tui.js"], tmp_path))
    monkeypatch.setenv("HERMES_CWD", str(tmp_path))

    assert chat_workspaces.resolve_chat_cwd(selected) == selected
    with pytest.raises(HTTPException, match="unavailable"):
        chat_workspaces.resolve_chat_cwd(str(tmp_path))
    _argv, cwd, env = web_server_chat._resolve_chat_argv(workspace_cwd=selected)

    assert cwd == str(tmp_path) or cwd == tmp_path
    assert env["HERMES_CWD"] == str(tmp_path)
    assert env["HERMES_TUI_CWD"] == selected
    assert env["HERMES_TUI_CWD_EXPLICIT"] == "1"
