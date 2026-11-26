"""Resolve dashboard workspace file requests against the owning SSH chat."""

import json
import posixpath
from pathlib import PurePosixPath

from hermes_cli.ssh_project_probe import _under_roots, managed_project_connection
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


def _stored_terminal(session, profile):
    from gateway.session_policy import restore_policy
    from hermes_cli.web_routers.sessions import _with_db
    from persistence.core.errors import AuthorityRefusal

    def read(store):
        owner = store.bindings.lineage_owner(session["id"])
        if not store.bindings.binding_marks(owner).policy:
            return None
        return json.loads(restore_policy(store.bindings.receipt(owner).policy).terminal_json)

    try:
        return _with_db(profile, read, read_only=True)
    except AuthorityRefusal as exc:
        raise SshWorkspaceFsError("ECONN", "The saved session workspace is unavailable.") from exc


def managed_file_request(path, profile, session, home, roots, terminal):
    """Return a pinned adapter and absolute path for the current managed policy."""
    requested = path.strip()
    if not requested or (requested == "~" or requested.startswith("~/")) or "\0" in requested:
        raise SshWorkspaceFsError("EINVAL", "Invalid SSH workspace file path.")

    cwd = roots[0]
    if session is not None:
        saved = _stored_terminal(session, profile)
        if saved is not None:
            if saved.get("TERMINAL_ENV") != "ssh":
                raise SshWorkspaceFsError("EWORKSPACE", "Select an SSH project for this session before opening workspace files.")
            candidate = saved.get("TERMINAL_CWD")
        else:
            candidate = session.get("cwd")
        if candidate not in (None, "", ".", "auto", "cwd", "~"):
            cwd = candidate
    if not _under_roots(cwd, roots):
        raise SshWorkspaceFsError("EWORKSPACE", "The saved session workspace is outside the applied SSH roots.")
    requested = posixpath.normpath(posixpath.join(cwd, requested))
    if not _under_roots(requested, roots):
        raise SshWorkspaceFsError("EGRANT", "The file is outside the applied SSH workspace roots.")
    backend = managed_project_connection(terminal, str(home))
    facts = backend.inspect_projects([cwd], roots)[cwd]
    if facts.get("cwd") != cwd:
        raise SshWorkspaceFsError("EWORKSPACE", "The saved session workspace is unavailable.")
    target = backend.normalize(requested)
    if target != requested:
        raise SshWorkspaceFsError("EINVAL", "Invalid SSH workspace file path.")
    return backend, target, roots


def staged_file_candidate(path, home, cache):
    """Only explicit names in owned staging locations may reach the host resolver."""
    candidate = PurePosixPath(path)
    return candidate.is_absolute() and (
        candidate.parent == PurePosixPath(cache)
        or (candidate.parent == PurePosixPath(home) / "uploads" and candidate.name.startswith("web-"))
    )
