"""Session-owned references issued by the managed file attachment handler."""

import hashlib
import re
from pathlib import Path

_PREFIX = "staged:"
_NAME = re.compile(r"[0-9a-f]{32}-.+")


def attachment_directory(home: Path, session_id: str) -> Path:
    return home / "attachments" / hashlib.sha256(session_id.encode()).hexdigest()


def attachment_reference(path: Path) -> str:
    return _PREFIX + path.name


def resolve_staged_reference(target, session_id):
    """Resolve a handle only in its owning session's attachment directory."""
    from hermes_constants import get_hermes_home

    if not target.startswith(_PREFIX):
        return None
    name = target[len(_PREFIX):]
    if not session_id or not _NAME.fullmatch(name) or "/" in name or '\\' in name:
        return None
    home = get_hermes_home()
    path = attachment_directory(home, session_id) / name
    try:
        if not path.is_symlink() and path.resolve(strict=True) == attachment_directory(home.resolve(), session_id) / name:
            return path
    except (OSError, RuntimeError):
        pass
    return None


def expand_staged_reference(ref, session_id, limit):
    """Expand an issued attachment handle in this session's own staging directory."""
    if ref.kind != "file" or not ref.target.startswith(_PREFIX):
        return None

    from dataclasses import replace
    from agent.context_references import _expand_path_reference

    path = resolve_staged_reference(ref.target, session_id)
    if path is None:
        return f"{ref.raw}: staged attachment is unavailable", None
    return _expand_path_reference(replace(ref, target=str(path)), path.parent, allowed_root=path.parent,
                                  max_inline_tokens=limit)
