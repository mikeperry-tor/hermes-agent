"""Read project and repository facts from a profile's managed SSH executor."""

from hermes_cli.ssh_project_probe import _absolute_project, _under_roots, inspect_managed_projects, managed_project_access
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


def managed_project_config_snapshot() -> tuple[bool, dict | None]:
    """Return ``(managed_active, snapshot)`` from one strict managed config read.

    Returns ``(False, None)`` when unmanaged, ``(True, managed)`` when managed SSH
    workspace roots are active, and ``(True, None)`` when managed config fails to
    load or validate so callers fail closed without resolving profile directories.
    """
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.managed_scope import load_managed_config
    import hermes_yaml as yaml
    from tools.file_tools_paths import managed_ssh_workspace_roots

    try:
        managed = load_managed_config(strict=True)
        if managed_ssh_workspace_roots(managed) is None:
            return False, None
        return True, managed
    except (ConfigBackendUnavailable, OSError, ValueError, yaml.YAMLError):
        return True, None


def profile_project_access(*, managed: dict | None = None):
    """Return None for unmanaged profiles and an empty grant when managed settings fail."""
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import TerminalPolicyUnavailable, build_profile_terminal_scope

    if managed is None:
        active, managed = managed_project_config_snapshot()
        if not active:
            return None
        if managed is None:
            return (), {}
    try:
        return managed_project_access(build_profile_terminal_scope(get_hermes_home()), managed=managed) or ((), {})
    except (SshWorkspaceFsError, TerminalPolicyUnavailable, ValueError):
        return (), {}


def current_project_facts(access, paths, *, git=True) -> dict:
    from hermes_constants import get_hermes_home
    from tui_gateway.git_probe import _GIT_TIMEOUT

    try:
        return inspect_managed_projects(access, paths, profile_key=str(get_hermes_home()), git_timeout=_GIT_TIMEOUT if git else None)
    except SshWorkspaceFsError:
        return {}


def profile_project_location(access, path: str = "") -> dict:
    roots, terminal = access
    default = terminal.get("TERMINAL_CWD", "")
    cwd = path or (default if default.startswith("/") else next(iter(roots), ""))
    info = current_project_facts(access, [cwd]).get(cwd, {})
    return {"cwd": info.get("cwd", cwd), "branch": info.get("branch", ""), "available": bool(info.get("cwd"))}


def validated_project_folders(folders, primary):
    """Validate proposed folders before changing the profile's project record."""
    if not (folders or primary):
        return folders, primary
    access = profile_project_access()
    if access is None:
        return folders, primary
    if not isinstance(folders, (list, tuple)) or any(not _absolute_project(path) for path in folders):
        raise ValueError("Project folders must be absolute executor paths.")
    if primary and not _absolute_project(primary):
        raise ValueError("The primary project folder must be an absolute executor path.")
    paths = [*folders, *([primary] if primary else [])]
    facts = current_project_facts(access, paths, git=False)
    if any(not facts.get(path, {}).get("cwd") for path in paths):
        raise ValueError("The project folder is unavailable on the SSH executor.")
    return [facts[path]["cwd"] for path in folders], facts[primary]["cwd"] if primary else primary


def apply_saved_executor_cwds(sessions, bindings):
    """Use the workspace in a canonical receipt for older session rows."""
    from gateway.session_policy import display_workspace_cwd, restore_policy
    from persistence.core.errors import AuthorityRefusal

    owners = {segment: entry.owner for entry in bindings.list_lineages()
              for segment in (entry.owner, *entry.lineage)}
    cwds = {}
    for session in sessions:
        owner = owners.get(session["id"])
        if owner is None:
            continue
        if owner not in cwds:
            try:
                policy = restore_policy(bindings.receipt(owner).policy)
                cwds[owner] = display_workspace_cwd(policy)
            except AuthorityRefusal:
                cwds[owner] = None
        if cwds[owner]:
            session["cwd"] = cwds[owner]


def session_project_facts(sessions, projects, discovered, access) -> dict:
    """Probe every candidate once, and project current facts onto the existing rows."""
    paths = [session.get("cwd") or session.get("git_repo_root") for session in sessions]
    paths.extend(folder.get("path") for project in projects for folder in project.get("folders") or [])
    paths.extend(entry.get("root") for entry in discovered)
    facts = current_project_facts(access, paths)
    for session in sessions:
        path = session.get("cwd") or session.get("git_repo_root")
        info = facts.get(path, {})
        session["workspace_available"] = bool(path and info.get("cwd") == path)
        if not session["workspace_available"]:
            continue
        session["git_repo_root"] = info.get("repo_root") or ""
        session["git_branch"] = info.get("branch") or ""
    return facts


def build_managed_tree(sessions, projects, discovered, access, preview_limit, hydrate) -> dict:
    from tui_gateway.project_tree import build_executor_tree

    facts = session_project_facts(sessions, projects, discovered, access)
    present = {value for info in facts.values() for name in ("cwd", "repo_root", "worktree_root")
               if (value := info.get(name))}
    present.update(path for path, info in facts.items() if info.get("cwd"))
    verified = [entry for entry in discovered if _is_discovered_worktree(entry["root"], facts)]
    tree = build_executor_tree(projects, sessions, verified, facts.get, preview_limit=preview_limit, hydrate=hydrate,
                      exists=present.__contains__)
    for project in tree["projects"]:
        _mark_workspace(project, present)
        for repo in project["repos"]:
            _mark_workspace(repo, present)
            for lane in repo["groups"]:
                _mark_workspace(lane, present)
    return tree


def _mark_workspace(node: dict, present: set[str]) -> None:
    if path := node.get("path"):
        node["workspace_available"] = path in present


def _is_discovered_worktree(path: str, facts: dict) -> bool:
    info = facts.get(path, {})
    return info.get("cwd") == path == info.get("worktree_root")


def discovery_scope(policy, access):
    roots, _ = access
    configured = policy["roots"] or roots
    scan_roots = tuple(dict.fromkeys(
        admitted if _under_roots(admitted, (requested,)) else requested
        for requested in configured if _absolute_project(requested)
        for admitted in roots if _under_roots(requested, (admitted,)) or _under_roots(admitted, (requested,))
    ))
    exclusions = tuple(path for path in policy["exclude_paths"] if _absolute_project(path))
    return scan_roots, exclusions


def eligible_cached_repos(conn, policy, access):
    if not policy["enabled"]:
        return []
    from hermes_cli.projects_db import list_discovered_repos

    scan_roots, exclusions = discovery_scope(policy, access)
    return [entry for entry in list_discovered_repos(conn)
            if _under_roots(entry["root"], scan_roots) and not _under_roots(entry["root"], exclusions)]


def update_discovered_repos(conn, policy, access):
    from hermes_cli.projects_db import record_discovered_repos
    from hermes_cli.ssh_project_probe import scan_managed_projects
    from hermes_constants import get_hermes_home
    from tui_gateway.git_probe import _GIT_TIMEOUT

    scan_roots, exclusions = discovery_scope(policy, access)
    try:
        paths, complete = scan_managed_projects(
            access, scan_roots, exclusions, profile_key=str(get_hermes_home()), git_timeout=_GIT_TIMEOUT)
    except SshWorkspaceFsError:
        return
    record_discovered_repos(conn, ((path, None) for path in paths), replace=complete)


def discovered_repo_results(conn, persistence, policy, access, *, scan=False):
    from posixpath import basename

    if scan and policy["enabled"]:
        update_discovered_repos(conn, policy, access)
    cached = eligible_cached_repos(conn, policy, access)
    rows = list(persistence.sessions.distinct_session_cwds()) if persistence is not None else []
    facts = current_project_facts(access, [row.cwd for row in rows] + [entry["root"] for entry in cached])
    repos = {}
    for entry in cached:
        path = entry["root"]
        if _is_discovered_worktree(path, facts):
            repos[path] = {"root": path, "label": entry["label"] or basename(path), "sessions": 0, "last_active": 0.0}
    for row in rows:
        root = facts.get(row.cwd, {}).get("repo_root")
        if not root:
            continue
        repo = repos.setdefault(root, {"root": root, "label": basename(root), "sessions": 0, "last_active": 0.0})
        repo["sessions"] += int(row.sessions or 0)
        repo["last_active"] = max(repo["last_active"], float(row.last_active or 0))
    return sorted(repos.values(), key=lambda repo: repo["last_active"], reverse=True)
