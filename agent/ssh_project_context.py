"""Discover project instructions in the selected SSH checkout."""

from __future__ import annotations

import base64
import logging
from pathlib import PurePosixPath
import posixpath

from agent.ssh_context_references import reference_workspace
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

logger = logging.getLogger(__name__)

_HERMES_NAMES = (".hermes.md", "HERMES.md")
_AGENTS_NAMES = ("AGENTS.override.md", "AGENTS.md", "agents.md")
_CLAUDE_NAMES = ("CLAUDE.md", "claude.md")


def _selected_workspace():
    from agent.runtime_cwd import scoped_session_cwd
    from gateway.session_context import get_session_env
    from hermes_cli.main_resume_args import requested_executor_project
    from tools.terminal_scope import get_terminal_scope

    if get_session_env("HERMES_CRON_SESSION") == "1" and (cwd := scoped_session_cwd()):
        return reference_workspace(None, directory=cwd)
    scope = get_terminal_scope()
    if scope is not None:
        selection = scope.get("TERMINAL_SSH_PROJECT_SELECTED")
        previous_cwd = scope.get("TERMINAL_CWD") or ""
        if selection == "1" or (selection is None and previous_cwd.startswith("/") and previous_cwd != "/workspace"):
            return reference_workspace(scope)
    if cwd := requested_executor_project():
        return reference_workspace(None, directory=cwd)
    return None


def _candidate(workspace, path, root, limit):
    label = posixpath.relpath(str(path), workspace.cwd)
    try:
        _, size, payload = workspace.backend.reference_path(str(path), (str(root),), kind="regular", limit=limit)
    except SshWorkspaceFsError as exc:
        if exc.code in ("ENOENT", "ENOTREG", "ENOTDIR"):
            return None
        return label, path, None
    try:
        raw = base64.b64decode(payload[1], validate=True)
        if len(raw) != size or size > limit:
            logger.warning("Skipping oversized SSH project instructions at %s", path)
            return label, path, None
        return label, path, raw.decode("utf-8-sig", errors="replace").strip()
    except (TypeError, ValueError, IndexError):
        return label, path, ""


def _priority_names(workspace, directory, root, names, limit, *, first_existing=False):
    for name in names:
        candidate = _candidate(workspace, directory / name, root, limit)
        if candidate is not None:
            yield candidate
            if first_existing or candidate[2]:
                return


def _cursor_rules(workspace, cwd, root, limit):
    rules = cwd / ".cursor" / "rules"
    names = [".cursorrules"]
    try:
        _, _, entries = workspace.backend.reference_path(str(rules), (str(root),), kind="directory", limit=200)
    except SshWorkspaceFsError:
        entries = []
    names.extend(f".cursor/rules/{name}" for name, size in entries if size is not None and name.endswith(".mdc"))
    return [entry for name in names if (entry := _candidate(workspace, cwd / name, root, limit)) is not None]


def _workspace_candidates(workspace, root, limit):
    cwd = PurePosixPath(workspace.cwd)
    chain = [root, *(root.joinpath(*cwd.relative_to(root).parts[:i])
                     for i in range(1, len(cwd.relative_to(root).parts) + 1))]
    for directory in reversed(chain):
        entries = list(_priority_names(workspace, directory, root, _HERMES_NAMES, limit, first_existing=True))
        if entries:
            yield ("hermes_md", *entries[0])
            break
    for directory in chain:
        for entry in _priority_names(workspace, directory, root, _AGENTS_NAMES, limit):
            yield ("agents_md", *entry)
    for entry in _priority_names(workspace, cwd, root, _CLAUDE_NAMES, limit):
        yield ("claude_md", *entry)
    for entry in _cursor_rules(workspace, cwd, root, limit):
        yield ("cursorrules", *entry)


def executor_context_candidates(context_length, *, directory=None):
    from agent.prompt_builder import _get_context_file_max_chars
    from tui_gateway.git_probe import _GIT_TIMEOUT

    try:
        workspace = reference_workspace(None, directory=directory) if directory is not None else _selected_workspace()
        if workspace is None:
            return None, []
        facts = workspace.backend.inspect_projects([workspace.cwd], workspace.grants, git_timeout=_GIT_TIMEOUT)
        info = facts.get(workspace.cwd, {})
        root = PurePosixPath(info.get("worktree_root") or workspace.cwd)
        limit = 4 * _get_context_file_max_chars(context_length)
        return PurePosixPath(workspace.cwd), list(_workspace_candidates(workspace, root, limit))
    except SshWorkspaceFsError as exc:
        logger.warning("Could not read SSH project instructions: %s", exc)
        return None, []


def executor_coding_workspace(*, terminal=None, directory=None) -> tuple[str, str]:
    """Return the probed directory and the workspace facts shown in a coding prompt."""
    from agent.coding_context import WORKSPACE_BLOCK_HEADER, _CONTEXT_FILES, _PROJECT_MARKERS
    from tui_gateway.git_probe import _GIT_TIMEOUT

    try:
        workspace = (reference_workspace(terminal, directory=directory)
                     if directory is not None else _selected_workspace())
        if workspace is None:
            return "", ""
        info = workspace.backend.inspect_projects(
            [workspace.cwd], workspace.grants, git_timeout=_GIT_TIMEOUT).get(workspace.cwd, {})
        root = info.get("worktree_root") or workspace.cwd
        _, _, rows = workspace.backend.reference_path(root, (root,), kind="directory", limit=200)
    except SshWorkspaceFsError as exc:
        logger.warning("Could not read SSH project metadata: %s", exc)
        return "", ""
    manifests = {name for name, size in rows if size is not None}
    project = [name for name in _PROJECT_MARKERS if name not in _CONTEXT_FILES and name in manifests]
    if not project and not info.get("worktree_root"):
        return workspace.cwd, ""
    lines = [WORKSPACE_BLOCK_HEADER, f"- Root: {root}"]
    if branch := info.get("branch"):
        lines.append(f"- Branch: {branch}")
    if project:
        lines.append(f"- Project: {', '.join(project[:6])}")
    return workspace.cwd, "\n".join(lines)
