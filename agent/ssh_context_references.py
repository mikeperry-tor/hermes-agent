"""Expand chat references against a pinned executor workspace."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import mimetypes
from pathlib import PurePosixPath
import posixpath

from hermes_cli.ssh_project_probe import managed_project_connection, _under_roots, managed_project_access
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

_FOLDER_LIMIT = 200


@dataclass(frozen=True)
class ReferenceWorkspace:
    cwd: str
    backend: object
    grants: tuple[str, ...] = ()


def reference_workspace(terminal_policy: dict | None, *, directory: str | None = None) -> ReferenceWorkspace | None:
    from hermes_cli.main_resume_args import uses_managed_executor
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import TerminalPolicyUnavailable, build_profile_terminal_scope, get_terminal_scope

    if not uses_managed_executor():
        return None
    home = get_hermes_home()
    if terminal_policy is None:
        terminal_policy = get_terminal_scope()
    if terminal_policy is None:
        try:
            terminal_policy = build_profile_terminal_scope(home)
        except TerminalPolicyUnavailable as exc:
            raise SshWorkspaceFsError("ECONN", "Could not resolve the SSH workspace profile.") from exc
    access = managed_project_access(terminal_policy)
    if access is None:
        raise SshWorkspaceFsError("ECONN", "The managed SSH workspace settings changed.")
    roots, terminal = access
    cwd = directory or terminal.get("TERMINAL_CWD")
    if cwd in (None, "", ".", "auto", "cwd", "~"):
        cwd = roots[0]
    grants = [root for root in roots if _under_roots(cwd, (root,))]
    if not grants:
        raise SshWorkspaceFsError("EINVAL", "The selected project is outside the applied SSH workspace roots.")
    grant = (max(grants, key=len),)
    backend = managed_project_connection(terminal, str(home))
    facts = backend.inspect_projects([cwd], grant)[cwd]
    if facts.get("cwd") != cwd:
        raise SshWorkspaceFsError("ENOENT", "The selected project is unavailable on the SSH executor.")
    return ReferenceWorkspace(cwd, backend, grant)


def _file_block(ref, target, size, payload, tokens, limit):
    from agent.context_references import (
        _TEXT_EXTENSIONS, _binary_reference_block, _format_file_reference, _oversized_text_reference_block,
    )
    from agent.model_metadata import CHARS_PER_TOKEN

    path = PurePosixPath(target)
    sample, raw = (base64.b64decode(part, validate=True) for part in payload)
    mime = mimetypes.guess_type(path.name)[0]
    if b"\0" in sample or (mime and not mime.startswith("text/") and not path.name.endswith(_TEXT_EXTENSIONS)):
        return None, _binary_reference_block(ref, path, remote_size=size)
    if len(raw) > limit:
        return None, _oversized_text_reference_block(ref, path, len(raw) // CHARS_PER_TOKEN, remote_size=size)
    encoding = "utf-8-sig" if ref.line_start in (None, 1) else "utf-8"
    return _format_file_reference(ref, path, raw.decode(encoding), tokens, remote_size=size)


def _folder_block(ref, target, cwd, rows):
    from agent.model_metadata import estimate_tokens_rough

    folder = PurePosixPath(target).relative_to(cwd)
    lines = [f"{folder}/"]
    for relative, size in rows:
        entry = PurePosixPath(relative)
        indent = "  " * (len(entry.parts) - 1)
        detail = "/" if size is None else f" ({size} bytes)"
        lines.append(f"{indent}- {entry.name}{detail}")
    if len(rows) == _FOLDER_LIMIT:
        lines.append("- ...")
    listing = "\n".join(lines)
    return None, f"📁 {ref.raw} ({estimate_tokens_rough(listing)} tokens)\n{listing}"


def completion_entries(workspace, directory, prefix="", *, include_hidden=False, recursive=False, limit=_FOLDER_LIMIT):
    try:
        _, _, rows = workspace.backend.reference_path(
            directory, (workspace.cwd,), kind="directory", limit=limit,
            prefix=prefix, include_hidden=include_hidden, recursive=recursive)
    except SshWorkspaceFsError:
        return []
    return [(name, size is None) for name, size in rows]


def expand_workspace_reference(ref, workspace, max_inline_tokens, attachment_session_id=None):
    from agent.context_references import _GIT_REFERENCE_ARGS, _MAX_QUIET_OUTPUT_BYTES, _git_reference_block
    from agent.model_metadata import CHARS_PER_TOKEN

    from agent.ssh_staged_references import expand_staged_reference

    if (result := expand_staged_reference(ref, attachment_session_id, max_inline_tokens)) is not None:
        return result
    if isinstance(workspace, SshWorkspaceFsError):
        raise workspace
    if ref.kind not in ("file", "folder"):
        arguments = _GIT_REFERENCE_ARGS[ref.kind](ref)
        output = workspace.backend.reference_git(workspace.cwd, workspace.grants, arguments, _MAX_QUIET_OUTPUT_BYTES)
        return _git_reference_block("git " + " ".join(arguments), output)
    if ref.target.startswith("~"):
        return f"{ref.raw}: use a path within the selected SSH workspace", None
    target = posixpath.normpath(posixpath.join(workspace.cwd, ref.target))
    if ref.kind == "folder":
        path, _, payload = workspace.backend.reference_path(
            target, (workspace.cwd,), kind="directory", limit=_FOLDER_LIMIT, recursive=True)
        return _folder_block(ref, path, workspace.cwd, payload)
    limit = max_inline_tokens * CHARS_PER_TOKEN
    path, size, payload = workspace.backend.reference_path(
        target, (workspace.cwd,), kind="regular", start=ref.line_start, end=ref.line_end, limit=limit)
    return _file_block(ref, path, size, payload, max_inline_tokens, limit)
