"""Locate and inspect explicitly trusted project skills on the SSH executor."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

logger = logging.getLogger(__name__)


def _selected_project_skill_roots():
    from agent.skill_utils import PROJECT_SKILLS_SUBDIRS, _config_str_list, _skills_cfg_get
    from agent.ssh_project_context import _selected_workspace
    from tui_gateway.git_probe import _GIT_TIMEOUT

    if _skills_cfg_get("project_discovery") is False:
        return None, ()
    trusted = set(_config_str_list(_skills_cfg_get("trusted_project_dirs")))
    if not trusted:
        return None, ()
    workspace = _selected_workspace()
    if workspace is None:
        return None, ()
    project = workspace.backend.inspect_projects(
        [workspace.cwd], workspace.grants, git_timeout=_GIT_TIMEOUT)[workspace.cwd]
    root = project.get("worktree_root") or project.get("cwd")
    if root not in trusted:
        return None, ()
    return workspace, tuple(PurePosixPath(root) / name for name in PROJECT_SKILLS_SUBDIRS)


def _read_project_skill_file(workspace, path, root, limit):
    _, size, payload = workspace.backend.reference_path(
        str(path), (str(root),), kind="regular", limit=limit)
    data = base64.b64decode(payload[1], validate=True)
    if size > limit or len(data) != size:
        raise SshWorkspaceFsError("EFBIG", f"Skill file is too large: {path.name}")
    return data


def _snapshot_skill(workspace, directory, allowed, staging):
    from tools.skills_guard import MAX_FILE_COUNT, MAX_SINGLE_FILE_KB, MAX_TOTAL_SIZE_KB

    pending = [PurePosixPath(".")]
    count, total = 0, 0
    while pending:
        relative = pending.pop()
        path = directory / relative
        _, _, entries = workspace.backend.reference_path(
            str(path), (str(allowed),), kind="directory", limit=MAX_FILE_COUNT + 1, include_hidden=True)
        for name, size in entries:
            if not name or "/" in name or name in (".", ".."):
                raise ValueError("Invalid executor skill entry.")
            entry = relative / name
            count += 1
            if count > MAX_FILE_COUNT:
                raise ValueError("Executor skill exceeds the scanner's bundle limits.")
            if size is None:
                pending.append(entry)
                continue
            data = _read_project_skill_file(workspace, directory / entry, allowed, MAX_SINGLE_FILE_KB * 1024)
            total += len(data)
            if total > MAX_TOTAL_SIZE_KB * 1024:
                raise ValueError("Executor skill exceeds the scanner's bundle limits.")
            staged = staging / entry
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)


def _prepare_project_skill(workspace, directory, allowed, name, file_path=None):
    from tools.skills_guard import scan_skill
    from tools.skills_tool import _skill_linked_files
    from tools.skills_tool_plugin import _read_skill_text, _serve_skill_file

    with TemporaryDirectory(prefix="hermes-project-skill-") as temp:
        snapshot = Path(temp) / directory.name
        snapshot.mkdir()
        _snapshot_skill(workspace, directory, allowed, snapshot)
        if scan_skill(snapshot, source="project-local").verdict == "dangerous":
            raise ValueError(f"Project skill {name!r} is quarantined by the security scan.")
        support = (json.loads(_serve_skill_file(snapshot, file_path, name, list_available=True))
                   if file_path is not None else None)
        return {
            "directory": directory,
            "content": _read_skill_text(snapshot / "SKILL.md"),
            "linked": _skill_linked_files(snapshot),
            "support": support,
            "load_path": directory.relative_to(allowed).as_posix(),
        }


_MISSING_SKILL_PATH = ("ENOENT", "ENOTDIR", "ENOTREG")


def _project_skill_metadata(workspace, roots):
    from agent.skill_utils import parse_frontmatter
    from tools.skills_guard import MAX_SINGLE_FILE_KB

    for root in roots:
        directories = [root]
        while directories:
            directory = directories.pop()
            try:
                _, _, entries = workspace.backend.reference_path(
                    str(directory), (str(root),), kind="directory", limit=500)
            except SshWorkspaceFsError as exc:
                if exc.code in _MISSING_SKILL_PATH:
                    continue
                raise
            if any(name == "SKILL.md" and size is not None for name, size in entries):
                path = directory / "SKILL.md"
                content = _read_project_skill_file(workspace, path, root, MAX_SINGLE_FILE_KB * 1024)
                metadata, _ = parse_frontmatter(content.decode("utf-8-sig", errors="replace"))
                yield root, path, metadata
                continue
            directories.extend(directory / name for name, size in reversed(entries)
                               if size is None and name not in ("scripts", "assets", "references", "templates"))


def project_skill_metadata():
    """Return trusted, scanned skill metadata for model and operator selectors."""
    from hermes_cli.main_resume_args import uses_managed_executor
    from tools.hosted_tool_policy import host_feature_refusal

    if not uses_managed_executor() or host_feature_refusal("skills", "Project skills"):
        return []
    try:
        workspace, roots = _selected_project_skill_roots()
        rows = []
        for root, file, metadata in _project_skill_metadata(workspace, roots):
            try:
                _prepare_project_skill(workspace, file.parent, root, file.parent.name)
            except (OSError, ValueError, SshWorkspaceFsError) as exc:
                logger.debug("Skipping executor skill %s: %s", file, exc)
                continue
            rows.append((root, file, metadata))
        return rows
    except (OSError, ValueError, SshWorkspaceFsError) as exc:
        logger.debug("Executor project skills unavailable: %s", exc)
        return []


def load_project_skill(name, file_path=None):
    """Resolve exact, directory, and declared names within trusted project roots."""
    workspace, roots = _selected_project_skill_roots()
    candidates = []
    for root, path, metadata in _project_skill_metadata(workspace, roots):
        directory = path.parent
        relative = directory.relative_to(root).as_posix()
        if name in (relative, directory.name, metadata.get("name")):
            candidates.append((directory, root))
    if len(candidates) > 1:
        raise ValueError(f"Multiple executor project skills match {name!r}. Use the exact relative skill directory.")
    if not candidates:
        return None
    directory, root = candidates[0]
    return _prepare_project_skill(workspace, directory, root, name, file_path)


def project_skill_allows_shell(directory):
    """Disable inline commands in remote community installs or unreadable locks."""
    from tools.skills_guard import MAX_SINGLE_FILE_KB

    workspace, roots = _selected_project_skill_roots()
    target = PurePosixPath(directory)
    root = next((root for root in roots if target.is_relative_to(root)), None)
    if root is None:
        return False
    for parent in target.parents:
        if parent.name != "skills" or not parent.is_relative_to(root):
            continue
        try:
            _, _, payload = workspace.backend.reference_path(
                str(parent / ".hub/lock.json"), (str(root),), kind="regular",
                limit=MAX_SINGLE_FILE_KB * 1024, complete=True)
            entries = json.loads(base64.b64decode(payload[1], validate=True))["installed"]
            if not isinstance(entries, dict):
                return False
            for entry in entries.values():
                if isinstance(entry, dict) and entry.get("trust_level") == "community":
                    rel = entry.get("install_path")
                    if isinstance(rel, str) and rel and target.is_relative_to(parent / rel):
                        return False
        except SshWorkspaceFsError as exc:
            if exc.code in ("ENOENT", "ENOTDIR"):
                continue
            return False
        except (ValueError, TypeError, KeyError, IndexError):
            return False
    return True
