"""Inspect directories and Git metadata on a managed SSH workspace."""

import json
import posixpath

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


_PROJECT_PROBE_DEFS = r"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def allowed(path, roots):
    return any(path == root or path.startswith(root + '/') for root in roots)


def git_output(cwd, ceiling, arguments, timeout):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1'}
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_DISCOVERY_ACROSS_FILESYSTEM'):
        env.pop(name, None)
    env['GIT_CEILING_DIRECTORIES'] = os.path.dirname(ceiling)
    try:
        result = subprocess.run(
            ['git', '-C', cwd, *arguments], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, env=env, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return os.fsdecode(result.stdout).rstrip('\n') if result.returncode == 0 else ''


def git_metadata_path(path, roots, prefix=b''):
    path = path.resolve(strict=True)
    if not allowed(str(path), roots) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('Invalid Git metadata path')
    with path.open('rb') as handle:
        value = handle.read(4097)
    if len(value) > 4096 or not value.startswith(prefix):
        raise ValueError('Invalid Git metadata path')
    return os.fsdecode(value[len(prefix):].strip())


def git_directory(directory, roots):
    for candidate in (Path(directory), *Path(directory).parents):
        if not allowed(str(candidate), roots):
            break
        marker = candidate / '.git'
        if not os.path.lexists(marker):
            continue
        git_dir = marker.resolve(strict=True)
        if not allowed(str(git_dir), roots):
            raise ValueError('Git metadata is outside the applied roots')
        if not git_dir.is_dir():
            git_dir = (candidate / git_metadata_path(marker, roots, b'gitdir: ')).resolve(strict=True)
        if not allowed(str(git_dir), roots) or not git_dir.is_dir():
            raise ValueError('Git metadata is unavailable')
        common_file = git_dir / 'commondir'
        common = git_dir
        if os.path.lexists(common_file):
            common = (git_dir / git_metadata_path(common_file, roots)).resolve(strict=True)
            if not allowed(str(common), roots) or not common.is_dir():
                raise ValueError('Common Git metadata is unavailable')
        return str(candidate), common
    return None


def inspect_project(path, roots, git_timeout):
    if not allowed(path, roots):
        return {'error': 'The project is outside the applied SSH workspace roots.'}
    try:
        directory = str(Path(path).resolve(strict=True))
        if directory != directory.strip() or not allowed(directory, roots):
            return {'error': 'The project resolves outside the applied SSH workspace roots.'}
        if not stat.S_ISDIR(os.stat(directory).st_mode) or not os.access(directory, os.X_OK):
            return {'error': 'The directory is unavailable on the SSH executor.'}
    except (OSError, RuntimeError):
        return {'error': 'The directory is unavailable on the SSH executor.'}
    project = {'cwd': directory}
    if git_timeout is None:
        return project
    try:
        location = git_directory(directory, roots)
    except (OSError, RuntimeError, ValueError):
        return {**project, 'git_unavailable': True}
    if location is None:
        return project
    root, common_dir = location
    if git_output(directory, root, ('rev-parse', '--show-toplevel'), git_timeout) != root:
        return {**project, 'git_unavailable': True}
    common = str(common_dir.parent) if common_dir.name == '.git' else root
    if not allowed(common, roots):
        common = root
    branch = git_output(directory, root, ('branch', '--show-current'), git_timeout)
    if not branch:
        branch = git_output(directory, root, ('rev-parse', '--short', 'HEAD'), git_timeout)
    return {**project, 'repo_root': common, 'worktree_root': root, 'branch': branch}


def discover_projects(request, roots):
    repositories = set()
    complete = True

    def inaccessible(_error):
        nonlocal complete
        complete = False

    for start in request['scan']:
        if not allowed(start, roots) or not os.path.isdir(start) or os.path.realpath(start) != start:
            complete = False
            continue
        for directory, children, files in os.walk(start, followlinks=False, onerror=inaccessible):
            if allowed(directory, request['exclude']):
                children[:] = []
                continue
            if '.git' in children or '.git' in files:
                facts = inspect_project(directory, roots, request['git_timeout'])
                if facts.get('repo_root'):
                    repositories.add(directory)
                elif facts.get('git_unavailable'):
                    complete = False
                children[:] = []
            elif len(Path(directory).relative_to(start).parts) >= 3:
                children[:] = []
            else:
                children[:] = [child for child in children if not child.startswith('.') and child != 'node_modules']
            if len(repositories) >= 500:
                return {'repos': sorted(repositories), 'complete': False}
    return {'repos': sorted(repositories), 'complete': complete}


def list_project_directory(directory, roots, hidden):
    entries = []
    if not allowed(directory, roots):
        # Show only the configured branches leading to actual mounts.
        prefix = directory.rstrip('/') + '/'
        names = {root[len(prefix):].split('/', 1)[0] for root in roots if root.startswith(prefix)}
        if not names:
            return {'entries': [], 'error': 'EACCES'}
        entries = [{'name': name, 'path': prefix + name, 'isDirectory': True}
                   for name in names if name not in hidden]
    else:
        project = inspect_project(directory, roots, None)
        if 'cwd' not in project:
            return {'entries': [], 'error': 'ENOENT'}
        try:
            with os.scandir(project['cwd']) as children:
                for child in children:
                    if child.name not in hidden and not child.is_symlink():
                        entries.append({'name': child.name, 'path': child.path, 'isDirectory': child.is_dir()})
        except OSError:
            return {'entries': [], 'error': 'EACCES'}
    entries.sort(key=lambda entry: (not entry['isDirectory'], entry['name'].lower(), entry['name']))
    return {'entries': entries}


def inspect_projects(request):
    roots = tuple(root for root in request['roots'] if os.path.isdir(root) and os.path.realpath(root) == root)
    if 'list' in request:
        return list_project_directory(request['list'], roots, request['hidden'])
    if 'scan' in request:
        return discover_projects(request, roots)
    return {path: inspect_project(path, roots, request['git_timeout']) for path in request['paths']}
"""


_PROJECT_PROBE = _PROJECT_PROBE_DEFS + "\nprint(json.dumps(inspect_projects(json.load(sys.stdin))))\n"


def _absolute_project(path) -> bool:
    return (isinstance(path, str) and path.startswith("/") and not path.startswith("//")
            and path == path.strip() and "\0" not in path and posixpath.normpath(path) == path)


def _under_roots(path, roots) -> bool:
    return _absolute_project(path) and any(path == root or path.startswith(root + "/") for root in roots)


def _valid_project_facts(info, roots) -> bool:
    if not isinstance(info, dict):
        return False
    if info.keys() == {"error"}:
        return isinstance(info["error"], str) and bool(info["error"])
    if info.keys() == {"cwd"}:
        return _under_roots(info["cwd"], roots)
    if info.keys() == {"cwd", "git_unavailable"}:
        return _under_roots(info["cwd"], roots) and info["git_unavailable"] is True
    return (info.keys() == {"cwd", "repo_root", "worktree_root", "branch"}
            and all(_under_roots(info[field], roots) for field in ("cwd", "repo_root", "worktree_root"))
            and _under_roots(info["cwd"], (info["worktree_root"],))
            and isinstance(info["branch"], str))


def inspect_projects(ops, paths, roots, *, git_timeout: float | None = None) -> dict[str, dict]:
    """Return executor facts for one set of absolute project paths."""
    paths = tuple(paths)
    if any(not _absolute_project(path) for path in paths):
        raise SshWorkspaceFsError("EINVAL", "An absolute executor project path is required.")
    requested = tuple(dict.fromkeys(paths))
    if not requested:
        return {}
    request = json.dumps({"paths": requested, "roots": roots, "git_timeout": git_timeout})
    response = _probe_projects(ops, request)
    if not isinstance(response, dict) or response.keys() != set(requested):
        raise SshWorkspaceFsError("EIO", "The SSH executor returned incomplete project information.")
    if any(not _valid_project_facts(info, roots) for info in response.values()):
        raise SshWorkspaceFsError("EIO", "The SSH executor returned invalid project information.")
    return response


def list_project_directory(ops, path, roots, hidden):
    if not _absolute_project(path):
        raise SshWorkspaceFsError("EINVAL", "An absolute executor project path is required.")
    request = json.dumps({"list": path, "roots": roots, "hidden": sorted(hidden)})
    result = _probe_projects(ops, request)
    if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
        raise SshWorkspaceFsError("EIO", "The SSH executor returned an invalid directory listing.")
    return result


def _probe_projects(ops, request):
    try:
        return json.loads(ops._framed_ssh_file_output(_PROJECT_PROBE, stdin_data=request))
    except (OSError, ValueError) as exc:
        raise SshWorkspaceFsError("ECONN", "Could not inspect the SSH workspace.") from exc


def managed_project_access(terminal: dict, *, managed: dict | None = None) -> tuple[tuple[str, ...], dict] | None:
    """Read the grants and endpoint from one administrator policy snapshot."""
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.managed_scope import apply_managed_overlay, load_managed_config
    import hermes_yaml as yaml
    from tools.file_tools_paths import managed_ssh_workspace_roots

    try:
        if managed is None:
            managed = load_managed_config(strict=True)
        roots = managed_ssh_workspace_roots(managed)
    except (ConfigBackendUnavailable, OSError, ValueError, yaml.YAMLError) as exc:
        raise SshWorkspaceFsError("ECONN", "Could not read the managed SSH workspace settings.") from exc
    if roots is None:
        return None
    settings = apply_managed_overlay({}, managed_snapshot={"terminal": managed["terminal"]})["terminal"]
    if settings.get("backend") != "ssh" or str(terminal.get("TERMINAL_ENV", "")).strip().lower() != "ssh":
        raise SshWorkspaceFsError("ECONN", "The managed SSH workspace requires a pinned SSH backend.")
    fields = ("ssh_host", "ssh_user", "ssh_port", "ssh_key", "ssh_config")
    if any(not isinstance(settings.get(name), (str, int)) or not str(settings[name]).strip()
           for name in fields):
        raise SshWorkspaceFsError("ECONN", "The managed SSH workspace requires a pinned endpoint and SSH configuration.")
    pinned = {**terminal, **{"TERMINAL_" + name.upper(): str(settings[name]) for name in fields}}
    return roots, pinned


def discover_projects(ops, roots, scan_roots, exclusions, *, git_timeout: float) -> tuple[list[str], bool]:
    """Run the managed repository scan in one executor request."""
    request = json.dumps({"scan": scan_roots, "roots": roots, "exclude": exclusions, "git_timeout": git_timeout})
    response = _probe_projects(ops, request)
    if not isinstance(response, dict) or response.keys() != {"repos", "complete"}:
        raise SshWorkspaceFsError("EIO", "The SSH executor returned invalid repository information.")
    repos, complete = response["repos"], response["complete"]
    if (type(complete) is not bool or not isinstance(repos, list) or len(repos) > 500
            or any(not _under_roots(repo, roots) or not _under_roots(repo, scan_roots)
                   or _under_roots(repo, exclusions) for repo in repos)):
        raise SshWorkspaceFsError("EIO", "The SSH executor returned invalid repository information.")
    return repos, complete


def managed_project_connection(terminal: dict, profile_key: str):
    """Connect using the endpoint returned by managed_project_access."""
    from hermes_cli.ssh_workspace_fs import get_ssh_workspace_fs

    config = {
        name: terminal.get("TERMINAL_" + name.upper())
        for name in ("ssh_host", "ssh_user", "ssh_port", "ssh_key", "ssh_config")
    }
    config.update(backend="ssh", cwd="/")
    try:
        # A stale ControlMaster can keep waiting after its executor disappears. Each project
        # probe uses OpenSSH's connection limit and rechecks the pinned host on a fresh connection.
        return get_ssh_workspace_fs(f"managed-project:{profile_key}", config, fresh_connections=True)
    except (TypeError, ValueError) as exc:
        raise SshWorkspaceFsError("ECONN", "The SSH workspace endpoint is invalid.") from exc


def inspect_managed_projects(access, paths, *, profile_key: str, git_timeout: float | None = None) -> dict:
    """Batch the admitted paths for a metadata request. Missing entries are unavailable."""
    roots, terminal = access
    candidates = [path for path in dict.fromkeys(paths) if _under_roots(path, roots)]
    if not candidates:
        return {}
    workspace = managed_project_connection(terminal, profile_key)
    return workspace.inspect_projects(candidates, roots, git_timeout=git_timeout)


def scan_managed_projects(access, scan_roots, exclusions, *, profile_key: str, git_timeout: float):
    roots, terminal = access
    if not scan_roots:
        return [], bool(roots)
    workspace = managed_project_connection(terminal, profile_key)
    return workspace.discover_projects(roots, scan_roots, exclusions, git_timeout=git_timeout)


def inspect_managed_project(terminal: dict, path: str | None, *, profile_key: str,
                            git_timeout: float | None = None) -> tuple[dict, dict] | None:
    """Resolve one managed project and return the terminal policy used to inspect it."""
    access = managed_project_access(terminal)
    if access is None:
        return None
    roots, terminal = access
    requested = roots[0] if path is None else path
    if not _under_roots(requested, roots):
        raise SshWorkspaceFsError("EINVAL", "The project is outside the applied SSH workspace roots.")
    workspace = managed_project_connection(terminal, profile_key)
    facts = workspace.inspect_projects([requested], roots, git_timeout=git_timeout)[requested]
    if error := facts.get("error"):
        raise SshWorkspaceFsError("EINVAL", error)
    terminal["TERMINAL_CWD"] = facts["cwd"]
    return facts, terminal
