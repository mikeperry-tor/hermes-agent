"""Keep terminal working directories on the owning chat's routing entry."""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_METADATA_KEY = "terminal_cwd"
_SSH_METADATA_KEY = "terminal_ssh_cwd"


def _backend(policy) -> str:
    """The backend this turn's terminal runs on: a local session's frozen policy, else (a messaging
    turn, which has none) the profile's live terminal config the tool itself reads."""
    if policy is not None:
        backend = json.loads(policy.terminal_json).get("TERMINAL_ENV")
    else:
        # Only the scope-aware backend name: the full tool config also parses timeouts, ports and
        # Docker JSON, and a typo there must not fail a turn that never runs a command.
        from tools.terminal_tool import _ensure_terminal_env_bridged
        from tools.terminal_tool_config import _tenv
        _ensure_terminal_env_bridged()
        backend = _tenv("TERMINAL_ENV", "local")
    return str(backend or "local").strip().lower()


def _local_backend(policy) -> bool:
    return _backend(policy) == "local"


def _ssh_binding(policy):
    terminal = json.loads(policy.terminal_json)
    cwd = terminal.get("TERMINAL_CWD")
    return cwd if cwd not in (None, "", ".", "~", "auto", "cwd") else None


def _managed_ssh(policy) -> bool:
    if policy is None:
        return False
    from hermes_cli.managed_operator_shell import OperatorShellUnavailable, placement

    try:
        selected = placement()
    except OperatorShellUnavailable:
        return False
    return selected is not None and selected.terminal.get("backend") == "ssh"


def _routing_entry(runner, session_key):
    """The chat's routing entry, or None when this runner keeps none for it (a one-shot or API
    runner without a routing store; the turn runner reads ``_entries`` the same way)."""
    entry = getattr(getattr(runner, "session_store", None), "_entries", {}).get(session_key)
    return entry if isinstance(getattr(entry, "metadata", None), dict) else None


# Which conversation (routing entry, by its creation stamp) each chat's in-process cwd record
# belongs to. ``/new`` publishes a fresh entry: the record left by the old conversation is dropped at
# that conversation's next turn, inside the turn's own profile scope that qualifies the record's key.
_record_owner: dict[str, object] = {}


def _saved_ssh_cwd(entry, binding):
    saved = entry.metadata.get(_SSH_METADATA_KEY) if entry else None
    if isinstance(saved, dict) and saved.get("binding") == binding:
        cwd = saved.get("cwd")
        if isinstance(cwd, str) and cwd.startswith("/") and "\x00" not in cwd:
            return cwd
    return None


def operator_ssh_cwd(runner, session_key, policy) -> str | None:
    """Use the session's recorded SSH cwd or its configured executor directory."""
    from hermes_cli.managed_operator_shell import OperatorShellUnavailable
    from tools.terminal_tool import _qualify_task_key, get_session_cwd

    if policy is None or _backend(policy) != "ssh":
        raise OperatorShellUnavailable("The session has no SSH terminal binding.")
    binding = _ssh_binding(policy)
    entry = _routing_entry(runner, session_key)
    token = (entry.created_at, _SSH_METADATA_KEY, binding) if entry else None
    owner = _qualify_task_key(session_key)
    if _record_owner.get(owner, token) == token and (cwd := get_session_cwd(session_key)):
        return cwd
    return _saved_ssh_cwd(entry, binding) or binding


def restore_turn_cwd(runner, session_key: str | None, policy) -> object | None:
    """Seed the chat's cwd record from its routing entry when this process has none yet, after
    dropping one an earlier conversation on this chat left. Returns the conversation token
    :func:`persist_turn_cwd` saves against."""
    entry = _routing_entry(runner, session_key) if session_key else None
    if entry is None:
        return None
    backend = _backend(policy)
    if backend == "local":
        field, binding = _METADATA_KEY, None
    elif backend == "ssh" and _managed_ssh(policy):
        field, binding = _SSH_METADATA_KEY, _ssh_binding(policy)
    else:
        return None
    from tools.terminal_tool import _qualify_task_key, clear_session_cwd, get_session_cwd, record_session_cwd
    owner, token = _qualify_task_key(session_key), (entry.created_at, field, binding)
    if _record_owner.get(owner, token) != token:
        clear_session_cwd(session_key)
    _record_owner[owner] = token
    if get_session_cwd(session_key) is None:
        if backend == "ssh":
            # The SSH backend checks existence when it runs a command.
            if saved := _saved_ssh_cwd(entry, binding):
                record_session_cwd(session_key, saved)
        elif isinstance((saved := entry.metadata.get(_METADATA_KEY)), str) and os.path.isabs(saved) and os.path.isdir(saved):
            record_session_cwd(session_key, saved)
    return token


def persist_turn_cwd(runner, session_key: str | None, token: object | None) -> None:
    """Write the chat's recorded cwd onto its routing entry when it changed. Only onto the entry the
    turn started on: a ``/new`` during the turn published a fresh one, which starts in ``terminal.cwd``."""
    if token is None:
        return
    created, field, binding = token
    entry = _routing_entry(runner, session_key)
    if entry is None or entry.created_at != created:
        return
    from tools.terminal_tool import get_session_cwd
    cwd = get_session_cwd(session_key)
    if not cwd:
        return
    saved = {"cwd": cwd, "binding": binding} if field == _SSH_METADATA_KEY else cwd
    if entry.metadata.get(field) == saved:
        return
    try:
        runner.session_store._update_entry(  # the conversation check and the write under one lock
            session_key, lambda e: e.created_at == created and not e.metadata.update({field: saved}))
    except Exception as exc:  # health: allow BLE001 -- runs in the turn's finally: a failed save must not fail the finished turn
        logger.warning("Could not save the terminal cwd of %s: %s", session_key, exc)


def forget_turn_cwd(runner, session_key: str) -> None:
    """Drop the chat's saved cwd along with its record (a workspace move): the next turn must start in
    the new binding's directory, not restore the old one."""
    entry = _routing_entry(runner, session_key)
    if entry is None:
        return
    try:  # the in-memory entry drops it before the save, so this process never restores it
        for field in (_METADATA_KEY, _SSH_METADATA_KEY):
            if entry.metadata.get(field) is not None:
                runner.session_store.set_session_metadata(session_key, field, None)
    except Exception as exc:  # health: allow BLE001 -- runs after the policy commit: a failed save must not strand its publication
        logger.warning("Could not clear the saved terminal cwd of %s: %s", session_key, exc)
