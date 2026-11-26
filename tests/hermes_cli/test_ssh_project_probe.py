"""Managed project metadata comes from the executor and the applied roots."""

import io
import subprocess
import sys

import pytest

from hermes_cli.ssh_project_probe import inspect_projects
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


@pytest.mark.platforms("posix")
def test_ssh_projects_validate_paths_and_refresh_git_metadata(tmp_path, monkeypatch, capsys):
    tmp_path = tmp_path.resolve()
    project = tmp_path / "project"
    branch_folder = project / "checkout"
    folder = project / "manual folder"
    branch_folder.mkdir(parents=True)
    folder.mkdir()
    outside = tmp_path / "project-sibling"
    outside.mkdir()
    alias = project / "outside-alias"
    alias.symlink_to(outside, target_is_directory=True)
    unavailable = project / "removed"
    subprocess.run(["git", "-C", str(branch_folder), "init", "--initial-branch=executor-main", "-q"], check=True)
    foreign_worktree = project / "foreign-worktree"
    subprocess.run(["git", "-C", str(outside), "init", "--initial-branch=foreign", "-q"], check=True)
    foreign_worktree.mkdir()
    (foreign_worktree / ".git").write_text(f"gitdir: {outside / '.git'}\n")

    class Executor:
        @staticmethod
        def _framed_ssh_file_output(snippet, *, stdin_data):
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_data))
            exec(compile(snippet, "<ssh executor>", "exec"), {})
            return capsys.readouterr().out

    def inspect(*paths):
        return inspect_projects(Executor(), map(str, paths), (str(project),), git_timeout=1.5)

    facts = inspect(branch_folder, folder, outside, alias, unavailable, foreign_worktree)
    assert facts[str(branch_folder)] == {
        "cwd": str(branch_folder), "repo_root": str(branch_folder),
        "worktree_root": str(branch_folder), "branch": "executor-main",
    }
    assert facts[str(folder)] == {"cwd": str(folder)}
    assert facts[str(foreign_worktree)] == {"cwd": str(foreign_worktree), "git_unavailable": True}
    with monkeypatch.context() as env:
        env.setenv("GIT_DIR", str(branch_folder / ".git"))
        env.setenv("GIT_WORK_TREE", str(branch_folder))
        assert inspect(folder)[str(folder)] == {"cwd": str(folder)}

    assert "outside" in facts[str(outside)]["error"]
    assert "resolves outside" in facts[str(alias)]["error"]
    assert "unavailable" in facts[str(unavailable)]["error"]

    subprocess.run(["git", "-C", str(branch_folder), "symbolic-ref", "HEAD", "refs/heads/remapped"], check=True)
    assert inspect(branch_folder)[str(branch_folder)]["branch"] == "remapped"

    with pytest.raises(SshWorkspaceFsError) as invalid:
        inspect("local-relative")
    assert invalid.value.code == "EINVAL"


def test_ssh_project_inspection_refuses_a_transport_failure():
    class Unavailable:
        @staticmethod
        def _framed_ssh_file_output(*_args, **_kwargs):
            raise ValueError("missing SSH response framing")

    with pytest.raises(SshWorkspaceFsError) as stopped:
        inspect_projects(Unavailable(), ["/workspace"], ("/workspace",))
    assert stopped.value.code == "ECONN"


@pytest.mark.platforms("posix")
def test_discovery_scans_only_the_granted_depth_and_exclusions(tmp_path, monkeypatch, capsys):
    from hermes_cli.ssh_project_probe import discover_projects

    root = tmp_path.resolve() / "shared"
    found = root / "one" / "two" / "repo"
    outside = root.with_name("shared-extra")
    excluded = root / "ignored"
    retired = root / "retired"
    for path in (found, outside, excluded, root / ".hidden", root / "a" / "b" / "c" / "too-deep"):
        path.mkdir(parents=True)
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (root / "alias").symlink_to(outside, target_is_directory=True)

    class Executor:
        @staticmethod
        def _framed_ssh_file_output(snippet, *, stdin_data):
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_data))
            exec(compile(snippet, "<ssh executor>", "exec"), {})
            return capsys.readouterr().out

    roots = (str(root),)
    repos, complete = discover_projects(Executor(), roots, roots, (str(excluded),), git_timeout=1.5)
    assert repos == [str(found)] and complete
    repos, complete = discover_projects(Executor(), roots, (*roots, str(retired)), (), git_timeout=1.5)
    assert repos == [str(excluded), str(found)] and not complete


@pytest.mark.platforms("posix")
def test_picker_lists_only_applied_branches_and_canonical_children(tmp_path, monkeypatch, capsys):
    from hermes_cli.ssh_project_probe import list_project_directory

    parent = tmp_path.resolve()
    root = parent / "nested" / "project"
    root.mkdir(parents=True)
    foreign = root.with_name("project-extra")
    foreign.mkdir()
    (root / "alias").symlink_to(foreign, target_is_directory=True)
    literal = root / "valid project\\"
    literal.mkdir()

    class Executor:
        @staticmethod
        def _framed_ssh_file_output(snippet, *, stdin_data):
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_data))
            exec(compile(snippet, "<ssh executor>", "exec"), {})
            return capsys.readouterr().out

    def listed(path):
        return list_project_directory(Executor(), str(path), (str(root),), set())

    assert listed(parent)["entries"] == [{"name": "nested", "path": str(root.parent), "isDirectory": True}]
    assert listed(root.parent)["entries"] == [{"name": "project", "path": str(root), "isDirectory": True}]
    assert listed(root)["entries"] == [{"name": literal.name, "path": str(literal), "isDirectory": True}]
    assert listed(foreign) == {"entries": [], "error": "EACCES"}
    assert not listed(root / "alias")["entries"]
