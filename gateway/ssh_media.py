"""Stage managed executor artifacts for a gateway delivery."""

from __future__ import annotations

import base64
import logging
import posixpath
import re
import uuid
from typing import Optional

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError

logger = logging.getLogger(__name__)


def fetch_managed_media(path, *, cwd=None):
    """Copy a guarded executor file to the receiving profile's document cache."""
    from agent.file_safety import is_project_env_basename
    from gateway.media_fetch import _FETCH_MAX_BYTES, remote_path_is_denied
    from gateway.media_policy import media_delivery_strict
    from tools.hosted_tool_policy import host_feature_refusal
    from gateway.platforms.base import get_document_cache_dir, validate_media_delivery_path
    from hermes_cli.ssh_project_probe import managed_project_connection, _under_roots, managed_project_access
    from hermes_constants import get_hermes_home
    from tools.file_operations import ShellFileOperations
    from tools.terminal_scope import get_terminal_scope
    from utils import atomic_write_bytes

    if media_delivery_strict() or host_feature_refusal("media_delivery", "Executor media delivery"):
        return None
    raw = str(path)
    if "\0" in raw or raw.startswith("//"):
        return None
    try:
        access = managed_project_access({"TERMINAL_ENV": "ssh"})
        if access is None:
            return None
        roots, terminal = access
        selected = cwd or (get_terminal_scope() or {}).get("TERMINAL_CWD")
        if selected in (None, "", ".", "auto", "cwd", "~"):
            selected = roots[0]
        if not _under_roots(selected, roots):
            return None
        backend = managed_project_connection(terminal, str(get_hermes_home()))
        remote_home = backend.normalize("~")
        candidate = backend.normalize(raw if raw.startswith(("~", "/")) else posixpath.join(selected, raw))
        if not _under_roots(candidate, roots) or remote_path_is_denied(candidate, remote_home):
            return None
        ops = ShellFileOperations(backend._env)
        state = ops.inspect_ssh_path(candidate, protect_read=True)
        if (state.kind != "regular" or state.read_denied or state.secret_file or not _under_roots(state.path, roots)
                or remote_path_is_denied(state.path, remote_home)
                or is_project_env_basename(posixpath.basename(state.path))):
            return None
        result = ops.read_inspected_ssh_bytes(state, _FETCH_MAX_BYTES)
        if result.error or result.base64_content is None:
            return None
        data = base64.b64decode(result.base64_content, validate=True)
        if len(data) != result.file_size:
            return None
        basename = re.sub(r"[^\w.\-]", "_", posixpath.basename(state.path)) or "file"
        dest = get_document_cache_dir() / f"remote_{uuid.uuid4().hex[:12]}_{basename}"
        atomic_write_bytes(dest, data, mode=0o600)
        resolved = validate_media_delivery_path(str(dest))
        if not resolved:
            dest.unlink(missing_ok=True)
        return resolved
    except (OSError, TypeError, ValueError, SshWorkspaceFsError) as exc:
        logger.info("SSH executor media is unavailable: %s", exc)
        return None


def managed_media_delivery() -> bool:
    from hermes_cli.main_resume_args import uses_managed_executor

    return uses_managed_executor()


_MANAGED_MEDIA_TOKEN_RE = re.compile(
    r'''[`"'*_]{0,3}MEDIA:[ \t]*(?P<path>(?:~/|/|\./|\.\./)?[^\s:"'`<>]+)''',
    re.IGNORECASE,
)


def managed_media_tokens(masked, known_pattern):
    covered = tuple(match.span() for match in known_pattern.finditer(masked))
    for match in _MANAGED_MEDIA_TOKEN_RE.finditer(masked):
        if any(start <= match.start() < end for start, end in covered):
            continue
        raw = match.group("path").rstrip(",;)]}")
        if raw and raw not in (".", "..", "/", "~"):
            yield match.start(), match.start("path") + len(raw), raw


def validated_delivery_path(raw_path, session_key: str, label: str,
                             dropped: Optional[list[dict]] = None, *, approved=(), cwd=None) -> Optional[str]:
    """``validate_media_delivery_path`` plus the shared "Skipping unsafe ..." warning. A path the
    host cannot see is retried against the active remote sandbox (ssh/modal/...; #466). When
    ``dropped`` is a list, a rejected path is appended as ``{"path", "reason"}`` so the caller can
    report the drop instead of booking a delivery that never happened (#115908)."""
    from gateway.platforms.base import _existing_regular_file, _managed_media_delivery, validate_media_delivery_path
    from gateway.platforms.base_log_safety import _log_safe_path

    raw = str(raw_path)
    if _managed_media_delivery():
        safe_path = (validate_media_delivery_path(raw, session_key=session_key) if raw in approved
                     else fetch_managed_media(raw, cwd=cwd))
        reason = "executor file unavailable or denied"
    else:
        safe_path = validate_media_delivery_path(raw, session_key=session_key)
        if not safe_path:
            from gateway.media_fetch import fetch_remote_media
            safe_path = fetch_remote_media(raw)
        reason = (("not found on this host" if not _existing_regular_file(raw) else "denied by the delivery policy")
                  if not safe_path else "")
    if not safe_path:
        logger.warning("Skipping %s (%s): %s", label, reason, _log_safe_path(raw))
        if dropped is not None:
            dropped.append({"path": raw, "reason": reason})
    return safe_path
