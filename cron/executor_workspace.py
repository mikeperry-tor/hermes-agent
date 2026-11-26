"""Validate a job's project directory on the managed SSH executor."""

from __future__ import annotations

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


def executor_workdir(workdir: str, *, home=None) -> tuple[str, dict] | None:
    """Resolve an explicit workdir from the owning profile's managed SSH policy."""
    from hermes_cli.main_resume_args import uses_managed_executor
    from hermes_cli.ssh_project_probe import inspect_managed_project
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import TerminalPolicyUnavailable, build_profile_terminal_scope

    if not uses_managed_executor():
        return None
    profile_home = home or get_hermes_home()
    try:
        terminal = build_profile_terminal_scope(profile_home)
        project = inspect_managed_project(terminal, workdir, profile_key=str(profile_home))
    except (TerminalPolicyUnavailable, SshWorkspaceFsError) as exc:
        raise ValueError(f"The cron workdir {workdir!r} is unavailable on the SSH executor. {exc}") from exc
    if project is None:
        raise ValueError("The managed SSH executor settings changed. Retry the cron operation.")
    facts, pinned = project
    return facts["cwd"], pinned


def executor_policy():
    """Get the applied grants and endpoint and the current owning profile."""
    from hermes_cli.managed_scope import load_managed_config
    from hermes_cli.ssh_project_probe import managed_project_access
    from hermes_constants import get_hermes_home

    managed = load_managed_config(strict=True)
    access = managed_project_access({"TERMINAL_ENV": "ssh"}, managed=managed)
    if access is None:
        raise SshWorkspaceFsError("ECONN", "The managed SSH executor settings changed.")
    return managed, *access, get_hermes_home()
