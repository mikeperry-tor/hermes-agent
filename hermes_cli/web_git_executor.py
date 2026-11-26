"""Scoped executor I/O for authenticated dashboard Git operations."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import posixpath
from pathlib import PurePosixPath

from hermes_cli.ssh_project_probe import managed_project_connection, _under_roots, managed_project_access
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

CURRENT: ContextVar[ExecutorGit | None] = ContextVar("web_executor_git", default=None)
_MAX_GIT_OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ExecutorGit:
    workspace: object
    roots: tuple[str, ...]

    def file_ops(self):
        from tools.file_operations import ShellFileOperations
        return ShellFileOperations(self.workspace._env)

    def run(self, cwd, args, timeout, *, write=False):
        from hermes_cli.ssh_project_git import run_executor_git
        return run_executor_git(self.file_ops(), cwd, self.roots, args, _MAX_GIT_OUTPUT_BYTES,
                                write=write, timeout=timeout)

    def file(self, cwd, raw, *, missing=True):
        from hermes_cli.web_routers.files import _is_sensitive_path

        target = posixpath.normpath(posixpath.join(cwd, raw))
        if not _under_roots(target, self.roots) or _is_sensitive_path(PurePosixPath(target)):
            raise SshWorkspaceFsError("EACCES", "The Git path is outside the permitted executor files.")
        state = self.file_ops().inspect_ssh_path(target, protect_read=True)
        if (state.read_denied or not _under_roots(state.path, self.roots) or _is_sensitive_path(PurePosixPath(state.path))
                or state.kind not in (("absent", "regular") if missing else ("regular",))):
            raise SshWorkspaceFsError("EACCES", "Expected an admitted regular executor file.")
        return state

    def directory(self, path):
        if not _under_roots(path, self.roots):
            return False
        result = self.workspace.inspect_projects([path], self.roots)[path]
        return result.get("cwd") == path

    def checkout_target(self, path):
        if not _under_roots(path, self.roots):
            raise SshWorkspaceFsError("EACCES", "The checkout is outside the applied SSH workspace roots.")
        state = self.file_ops().inspect_ssh_path(path)
        if state.path != path:
            raise SshWorkspaceFsError("EACCES", "The checkout path resolves through a symbolic link.")
        return state.kind


def executor_git(cwd: str):
    """Get the pinned adapter and canonical directory for this request."""
    from hermes_constants import get_hermes_home

    access = managed_project_access({"TERMINAL_ENV": "ssh"})
    if access is None:
        raise SshWorkspaceFsError("ECONN", "Managed executor settings are unavailable.")
    roots, terminal = access
    if not _under_roots(cwd, roots):
        raise SshWorkspaceFsError("EACCES", "The Git directory is outside the applied SSH workspace roots.")
    backend = managed_project_connection(terminal, str(get_hermes_home()))
    facts = backend.inspect_projects([cwd], roots)[cwd]
    directory = facts.get("cwd")
    if not directory:
        raise SshWorkspaceFsError("ENOENT", "The Git directory is unavailable on the SSH executor.")
    # Keep every operation in the most specific share containing this checkout.
    grant = (max((root for root in roots if _under_roots(directory, (root,))), key=len),)
    return ExecutorGit(backend, grant), directory
