"""``hermes chat`` session-argument steps split out of ``hermes_cli.main``: ``--in DIR``, ``--resume
@claude/@codex`` imports, and recreating a removed ``--tui -w`` worktree a resumed session needs (N8)."""

import os
import posixpath
import sys
from contextvars import ContextVar


_executor_project = ContextVar("cli_executor_project", default="")


def requested_executor_project() -> str:
    return _executor_project.get()


def uses_managed_executor() -> bool:
    from tools.file_tools_paths import managed_ssh_workspace_roots

    try:
        return managed_ssh_workspace_roots() is not None
    except ValueError:
        # The owner diagnoses a missing or unreadable grant when it admits work.
        return True


def managed_executor_workspace(path: str | None = None, *, use_profile: bool = True) -> str | None:
    """Select the saved-session lookup key using the executor's Git root when available."""
    from hermes_cli.ssh_project_probe import inspect_managed_project
    from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import TerminalPolicyUnavailable, build_profile_terminal_scope, default_terminal_scope
    from tui_gateway.git_probe import _GIT_TIMEOUT

    home = get_hermes_home()
    requested = path or requested_executor_project()
    try:
        terminal = build_profile_terminal_scope(home) if use_profile else default_terminal_scope()
    except TerminalPolicyUnavailable:
        return requested or None
    target = requested or terminal.get("TERMINAL_CWD")
    cwd = target if target and target.startswith("/") else None
    try:
        project = inspect_managed_project(terminal, cwd, profile_key=str(home), git_timeout=_GIT_TIMEOUT)
    except SshWorkspaceFsError:
        return cwd  # Use the saved directory to find history during an outage.
    if project is None:
        return None
    facts, _ = project
    return facts.get("worktree_root") or facts["cwd"]


def apply_in_dir(args) -> None:
    """--in DIR: chdir first so workspace-scoped lookups key off DIR; pins the session there."""
    _executor_project.set("")
    in_dir = getattr(args, "in_dir", None)
    if not in_dir:
        return
    if uses_managed_executor():
        if not in_dir.startswith("/") or in_dir.startswith("//"):
            print("Error: --in requires an absolute executor directory.", file=sys.stderr)
            sys.exit(1)
        _executor_project.set(posixpath.normpath(in_dir))
        args.no_restore_cwd = True
        return
    # Git Bash / MSYS hands us POSIX-style paths (`--in ~` → `/c/Users/x`);
    # translate drive-root spellings to native Windows form. No-op elsewhere.
    from tools.environments.local import _msys_to_windows_path

    _target_dir = os.path.abspath(os.path.expanduser(_msys_to_windows_path(in_dir)))
    if not os.path.isdir(_target_dir):
        print(f"Error: --in directory not found: {in_dir}")
        sys.exit(1)
    try:
        os.chdir(_target_dir)
    except OSError as e:
        print(f"Error: cannot enter --in directory {in_dir}: {e}")
        sys.exit(1)
    # Every cwd consumer (resolve_agent_cwd -> Codex app-server thread cwd, the
    # terminal tool, context-file discovery) prefers TERMINAL_CWD over the process
    # cwd, so a value inherited from a parent surface, the shell or .env outlives
    # this chdir and re-homes the session in the old directory (#106220). Refresh
    # it. An unset variable stays unset: the backends then derive from the new
    # process cwd (local exports it at cli import, docker mounts it, ssh and
    # container backends keep their own remote/sandbox default).
    if os.environ.get("TERMINAL_CWD", "").strip():
        os.environ["TERMINAL_CWD"] = _target_dir
    args.no_restore_cwd = True


def import_foreign_resume(args) -> None:
    """--resume @claude / @codex: import a foreign session and resume it."""
    _resume_foreign = getattr(args, "resume", None)
    if not (isinstance(_resume_foreign, str) and _resume_foreign.strip().lower() in ("@claude", "@codex")):
        return
    from hermes_cli.foreign_sessions import import_foreign_session, pick_foreign_session

    _picked = pick_foreign_session(_resume_foreign.strip().lower().lstrip("@"))
    if _picked is None:
        sys.exit(1)
    try:
        _imported_id = import_foreign_session(_picked.source, _picked.path)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"✓ Imported as {_imported_id} — resuming it now.")
    print(f"  (later: hermes --resume {_imported_id})")
    args.resume = _imported_id


def restore_resumed_workspace(args, session_db) -> None:
    """Restore the local launcher's directory or leave an SSH resume to its owner."""
    worktree = getattr(args, "worktree", False)
    if uses_managed_executor():
        if worktree:
            print("Error: --worktree is unavailable for an executor project.", file=sys.stderr)
            sys.exit(1)
        return
    recreate_resumed_tui_worktree(args)
    if not getattr(args, "resume", None) or getattr(args, "no_restore_cwd", False) or worktree:
        return
    with session_db() as db:
        session = db.persistence.sessions.get(args.resume)
        cwd = ((session.cwd if session else None) or "").strip()
        if cwd and not os.path.isdir(cwd):
            print(f"⚠ session's recorded dir is gone ({cwd}); staying in {os.getcwd()}")
        elif cwd and os.path.realpath(cwd) != os.path.realpath(os.getcwd()):
            os.chdir(cwd)
            print(f"↪ restored workspace dir: {cwd}")


def recreate_resumed_tui_worktree(args) -> None:
    """A resumed session whose frozen cwd is a ``-w`` / ``--tui -w`` tree that quit removed gets that
    tree back before attaching, or the owner refuses every turn ``workspace_unavailable`` (N8)."""
    if not getattr(args, "resume", None) or getattr(args, "worktree", False):
        return
    from hermes_cli.session_worktree import restore
    tree = restore(args.resume)
    if tree:
        print(f"↪ recreated the session's worktree: {tree}")
