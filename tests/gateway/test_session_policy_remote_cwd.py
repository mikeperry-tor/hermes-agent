"""A remote or container terminal backend never runs in the client's host launch folder.

Ink sends its launch folder as every session's ``cwd``; that path exists on this host only. ssh and
the sandboxes keep their own configured cwd (or their default) unless the user picked the folder, and
docker with ``docker_mount_cwd_to_workspace`` still mounts it at ``/workspace``.
"""
import json
from dataclasses import asdict, replace

import pytest

LAUNCH = object()  # the client's host launch folder


@pytest.mark.parametrize("terminal,params,expected", [
    ({"backend": "ssh", "cwd": "/srv/app"}, {}, ("/srv/app", None)),
    ({"backend": "ssh"}, {}, ("~", None)),
    ({"backend": "docker", "cwd": "/workspace/app"}, {}, ("/workspace/app", None)),
    ({"backend": "docker", "docker_mount_cwd_to_workspace": True}, {}, ("/workspace", LAUNCH)),
    ({"backend": "local"}, {}, (LAUNCH, None)),
    ({"backend": "ssh", "cwd": "/srv/app"}, {"cwd_explicit": True}, (LAUNCH, None)),
])
def test_terminal_cwd_follows_the_backend(tmp_path, monkeypatch, terminal, params, expected):
    from gateway.session_policy import build_policy, policy_scope, restore_policy
    from tools.terminal_tool import _get_env_config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"terminal": terminal}), encoding="utf-8")  # JSON is YAML
    monkeypatch.setenv("HERMES_HOME", str(home))
    launch = tmp_path / "project"
    launch.mkdir()

    policy = build_policy({"source": "tui", "cwd": str(launch), "model": "m", **params}, {})
    with policy_scope(restore_policy(asdict(policy))):
        config = _get_env_config()
    assert (config["cwd"], config.get("host_cwd")) == tuple(str(launch) if v is LAUNCH else v for v in expected)


@pytest.mark.asyncio
async def test_managed_session_create_move_and_readiness_use_executor_metadata(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gateway import session_local, session_mutation_policy
    from gateway.session_policy import build_policy, display_workspace_cwd, workspace_ready, workspace_unavailable_result
    from gateway.session_policy_refresh import _terminal
    from persistence.core.errors import AuthorityRefusal
    from persistence.sqlite.authority._local import _row_cwd

    root = "/mnt/projects/shared"
    first = root + "/initial"
    second = root + "/moved"
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(json.dumps({
        "terminal": {"backend": "ssh", "ssh_host": "executor", "ssh_user": "runner", "ssh_port": 2222,
                     "ssh_key": "/test/identity", "ssh_config": "/test/ssh_config",
                     "ssh_workspace_roots": [root]},
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    available = {first, second, root}

    class Workspace:
        def inspect_projects(self, paths, roots, *, git_timeout=None):
            assert roots == (root,)
            return {
                path: {"cwd": path, "repo_root": root, "worktree_root": path, "branch": "executor-branch"}
                if path in available
                else {"error": "The directory is unavailable on the SSH executor."}
                for path in paths
            }

    def dial(_profile, config, *, fresh_connections):
        assert fresh_connections
        assert config["ssh_host"] == "executor"
        assert config["ssh_config"] == "/test/ssh_config"
        assert config["ssh_key"] == "/test/identity"
        return Workspace()

    monkeypatch.setattr("hermes_cli.ssh_workspace_fs.get_ssh_workspace_fs", dial)
    authority = SimpleNamespace(profile_id=str(home), _require_admission_open=lambda: None)
    actor = SimpleNamespace(profile_id=str(home), capabilities={"session:create"}, subject="test")
    store = SimpleNamespace(sessions=SimpleNamespace(get=lambda _: None))
    monkeypatch.setattr(session_local, "authority_persistence", lambda _: store)

    async def create(params):
        selected = await session_local.prepare_local_workspace(authority, actor, params)
        return build_policy(params, {}, private_secrets={}, executor_project=selected)

    args = {"source": "tui", "cwd": first, "cwd_explicit": True, "model": "m", "row_cwd": second}
    policy = await create(args)
    assert policy.cwd == first and json.loads(policy.terminal_json)["TERMINAL_CWD"] == first
    assert _row_cwd(asdict(policy)) == first
    assert json.loads(policy.terminal_json)["TERMINAL_SSH_KEY"] is None
    assert workspace_ready(policy)
    default = await create({"source": "tui", "cwd": str(tmp_path), "cwd_explicit": False, "model": "m"})
    assert default.cwd == root
    classic = await create({"source": "cli", "cwd": str(tmp_path), "model": "m"})
    assert classic.cwd == root

    # A saved pre-upgrade chat can have separate historical and execution directories.
    legacy = build_policy({"source": "tui", "cwd": str(tmp_path), "cwd_explicit": False, "model": "m"}, {}, private_secrets={})
    legacy = replace(legacy, terminal_json=json.dumps({"TERMINAL_ENV": "ssh", "TERMINAL_CWD": first}))
    refreshed_terminal = _terminal(legacy, {"TERMINAL_ENV": "ssh", "TERMINAL_CWD": second}, ())
    assert legacy.cwd == str(tmp_path) and refreshed_terminal["TERMINAL_CWD"] == first
    assert display_workspace_cwd(legacy) == first
    assert first in workspace_unavailable_result(legacy)["final_response"]

    monkeypatch.setattr(session_mutation_policy, "_rebind",
                        lambda authority, prepared, old, policy, **extra: (policy, extra["git"]))
    prepared = {"snapshot": {"receipt": {"policy": asdict(policy)}}}
    moved, git = await session_mutation_policy.prepare_workspace(authority, None, {"cwd": second}, prepared)
    assert moved.cwd == second and json.loads(moved.terminal_json)["TERMINAL_CWD"] == second
    assert git == {"root": second, "branch": "executor-branch"}
    assert _terminal(moved, {"TERMINAL_ENV": "ssh"}, ())["TERMINAL_CWD"] == second

    from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError
    with pytest.raises(SshWorkspaceFsError, match="pinned SSH backend"):
        _terminal(moved, {"TERMINAL_ENV": "local"}, ())

    available.remove(second)
    assert not workspace_ready(moved)
    assert workspace_ready(policy)
    with pytest.raises(AuthorityRefusal, match="invalid_params"):
        await session_mutation_policy.prepare_workspace(authority, None, {"cwd": str(tmp_path)}, prepared)
    assert policy.cwd == first

    def offline(*_args, **_kwargs):
        pytest.fail("An exact retry must not probe the SSH workspace")

    store.sessions.get = lambda _: object()
    store.bindings = SimpleNamespace(receipt=lambda _: SimpleNamespace(policy=asdict(policy)))
    monkeypatch.setattr("hermes_cli.ssh_workspace_fs.get_ssh_workspace_fs", offline)
    assert await session_local.prepare_local_workspace(authority, actor, args) == (first, {})

    # A same-spelled Hermes directory cannot revive a retained managed receipt.
    (managed / "config.yaml").write_text('{"terminal": {"backend": "ssh"}}')
    host_decoy = replace(policy, cwd=str(tmp_path))
    assert not workspace_ready(host_decoy)
    with pytest.raises(AuthorityRefusal) as error:
        await session_mutation_policy.prepare_workspace(authority, None, {"cwd": str(tmp_path)}, prepared)
    assert error.value.detail == "The managed SSH workspace is unavailable."
