"""Chat workspace picker for a fresh dashboard chat.

The dashboard's ``/chat`` tab used to spawn every fresh TUI in the dashboard process's own
launch directory, so from a phone or browser there was no way to say "work in ~/code/foo".
This lists the same projects and discovered repositories the Desktop sidebar shows (the
per-profile ``projects.db`` plus session-derived and scanned git roots) so the SPA can offer
a picker, and ``/api/pty?cwd=`` (``chat_ws``) honours the choice.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from hermes_cli.web_deps import late
from hermes_cli.web_routers._common import http_failure, scoped_to_thread

router = APIRouter()

_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")


def resolve_chat_cwd(raw: Optional[str]) -> Optional[str]:
    """Validate the directory for a fresh chat on its configured workspace backend."""
    text = (raw or "").strip()
    if not text:
        return None
    from tui_gateway.managed_project_queries import profile_project_access, profile_project_location

    if (access := profile_project_access()) is not None:
        project = profile_project_location(access, text)
        if not project["available"]:
            raise HTTPException(status_code=400, detail="The project folder is unavailable on the SSH executor")
        return project["cwd"]
    resolved = os.path.abspath(os.path.expanduser(text))
    if not os.path.isdir(resolved):
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {text}")
    return resolved


def _collect_workspaces(profile: Optional[str], scan: bool) -> dict:
    # The dashboard hosts the in-process gateway (``web_server`` imports ``tui_gateway.server``
    # at startup), so the sidebar's repo-discovery helpers are already bound there.
    import tui_gateway.server as gateway
    from hermes_cli import projects_db as pdb
    from tui_gateway.managed_project_queries import discovered_repo_results, profile_project_access

    access = profile_project_access()
    # Browsing never bootstraps the owner's session store: a profile whose gateway has not
    # yet initialized state.db simply contributes no session-derived roots (``persistence=None``).
    try:
        persistence = _open_session_db_for_profile(profile, read_only=True)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        persistence = None
    try:
        with pdb.connect_closing() as conn:
            policy = gateway._repo_discovery_policy()
            projects = [p.to_dict() for p in pdb.list_projects(conn)]
            if access is not None:
                repos = discovered_repo_results(conn, persistence, policy, access, scan=scan)
                return _managed_workspaces(projects, repos, policy, access)
            if scan and policy["enabled"]:
                gateway._scan_discovered_repos_remote(conn, policy)
            repos = gateway._discover_repos_payload(
                None, conn=conn, backfill=False, include_cached=policy["enabled"], persistence=persistence)
    finally:
        if persistence is not None:
            persistence.close()
    default_cwd = gateway._completion_cwd({"profile": profile} if profile else {})
    return {
        "projects": projects, "repos": repos, "default_cwd": default_cwd,
        "home": str(Path.home()), "scan_enabled": bool(policy["enabled"])}


def _managed_workspaces(projects, repos, policy, access):
    from tui_gateway.managed_project_queries import profile_project_location, session_project_facts

    facts = session_project_facts([], projects, [], access)
    for project in projects:
        for folder in project.get("folders") or []:
            folder["workspace_available"] = bool(facts.get(folder["path"], {}).get("cwd"))
    location = profile_project_location(access)
    return {"projects": projects, "repos": repos, "default_cwd": location["cwd"],
            "home": "", "scan_enabled": bool(policy["enabled"])}


@router.get("/api/chat/workspaces")
async def get_chat_workspaces(profile: Optional[str] = None, scan: bool = False):
    """Projects + discovered repos a fresh chat may start in; ``scan=1`` rescans the
    configured discovery roots on the workspace backend first."""
    with http_failure("GET /api/chat/workspaces failed", 500, "Failed to list workspaces"):
        return await scoped_to_thread(profile, lambda: _collect_workspaces(profile, scan))
