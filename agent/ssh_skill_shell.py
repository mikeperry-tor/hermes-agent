"""Place managed skill support files and inline commands on the SSH executor."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


def managed_skill_workspace(skill_dir: Path, *, project: bool = False):
    """Resolve the skill's executor directory using its actual delivery mapping."""
    from agent.ssh_context_references import reference_workspace
    from tools.credential_files import _skill_dir_roots

    workspace = reference_workspace(None)
    if workspace is None:
        raise SshWorkspaceFsError("ECONN", "The managed SSH workspace is unavailable.")
    backend = workspace.backend
    if project:
        target = str(skill_dir)
        facts = backend.inspect_projects([target], workspace.grants)[target]
        if facts.get("cwd") != target:
            raise SshWorkspaceFsError("ENOENT", "The project skill directory is unavailable on the SSH executor.")
        return backend, target
    home = backend.normalize("~")
    local = skill_dir.resolve()
    for own, target_root in _skill_dir_roots(f"{home}/.hermes"):
        relative = local.relative_to(own.resolve()) if local.is_relative_to(own.resolve()) else None
        if relative is not None:
            return backend, str(PurePosixPath(target_root) / relative.as_posix())
    raise SshWorkspaceFsError("EINVAL", "This skill has no managed SSH directory.")


def run_managed_inline_shell(backend, command: str, cwd: str, timeout: int) -> str:
    """Run a single bounded foreground snippet in a validated skill directory."""
    from agent.compression_marker import elide
    from agent.redact import redact_sensitive_text
    from agent.skill_preprocessing import _INLINE_SHELL_MAX_OUTPUT

    timeout = max(1, int(timeout))
    result = backend.run_skill_shell(command, cwd, timeout)
    code = result.get("returncode")
    output = redact_sensitive_text(str(result.get("output") or "").rstrip("\n"))
    if code == 124:
        return f"[inline-shell timeout after {timeout}s: {redact_sensitive_text(command)}]"
    if code != 0:
        return elide(f"[inline-shell exit {code}] {output}".rstrip(), _INLINE_SHELL_MAX_OUTPUT)
    return elide(output, _INLINE_SHELL_MAX_OUTPUT)


def skill_execution_directory(skill_dir):
    """Expose the mapped path in managed skill instructions when it is available."""
    try:
        _, directory = managed_skill_workspace(skill_dir)
        return directory
    except (OSError, ValueError, SshWorkspaceFsError):
        return None
