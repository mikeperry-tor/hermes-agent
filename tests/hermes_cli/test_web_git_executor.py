"""Management-side Git actions run against the verified executor checkout."""

from pathlib import Path
import subprocess

import pytest

from hermes_cli import web_git
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError
from hermes_cli.web_git_executor import CURRENT, ExecutorGit


@pytest.mark.platforms("posix")
def test_executor_git_review_worktree_actions_and_filtering(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (root / ".gitattributes").write_text("*.txt filter=upper\n")
    git("config", "filter.upper.clean", "tr a-z A-Z")
    (root / "note.txt").write_text("initial executor contents\n")
    git("add", ".")
    git("commit", "-m", "Initial")
    git("branch", "other")
    (root / "note.txt").write_text("updated executor contents\n")
    (root / "new.txt").write_text("untracked executor marker\n")

    class Shell:
        def execute(self, command, **kwargs):
            proc = subprocess.run(["bash", "-c", command], input=kwargs.get("stdin_data"), capture_output=True,
                                  text=True, cwd=tmp_path, check=False)
            return {"returncode": proc.returncode, "output": proc.stdout}

    class Workspace:
        _env = Shell()

        def inspect_projects(self, paths, _roots):
            return {path: {"cwd": str(Path(path).resolve()) if Path(path).is_dir() else None} for path in paths}

    executor = ExecutorGit(Workspace(), (str(tmp_path),))
    token = CURRENT.set(executor)
    try:
        assert web_git.repo_status(str(root))["branch"] == "main"
        assert "updated executor contents" in web_git.review_diff(str(root), "note.txt", "uncommitted", None, False)
        assert "untracked executor marker" in web_git.review_diff(str(root), "new.txt", "uncommitted", None, False)
        assert web_git.review_stage(str(root), "note.txt") == {"ok": True}
        assert git("show", ":note.txt") == "UPDATED EXECUTOR CONTENTS\n"
        assert web_git.review_unstage(str(root), "note.txt") == {"ok": True}
        assert not git("diff", "--cached", "--name-only")
        git("restore", "note.txt")
        assert web_git.branch_switch(str(root), "other") == {"branch": "other"}
        worktree = web_git.worktree_add(str(root), {"name": "Review branch", "base": "main"})
        assert Path(worktree["path"]).exists()
        assert web_git.worktree_remove(str(root), worktree["path"], False) == {"removed": worktree["path"]}
        assert not Path(worktree["path"]).exists()

        unexpected = tmp_path / "different"
        unexpected.mkdir()
        staging = root / ".worktrees"
        if staging.is_dir():
            staging.rmdir()
        staging.symlink_to(unexpected, target_is_directory=True)
        with pytest.raises(SshWorkspaceFsError, match="symbolic link"):
            web_git.worktree_add(str(root), {"name": "Denied"})
        assert not git("branch", "--list", "hermes/denied")
        assert not list(unexpected.iterdir())
    finally:
        CURRENT.reset(token)
