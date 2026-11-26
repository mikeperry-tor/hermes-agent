"""Keep a managed child bound to the executor project it starts from."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath

from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


@dataclass(frozen=True)
class ChildProject:
    cwd: str
    selected: bool


def resolve_child_project(parent, continued: str | None = None, selected_before: bool | None = None) -> ChildProject | None:
    from agent.ssh_context_references import reference_workspace
    from hermes_cli.main_resume_args import uses_managed_executor
    from tools.environments.sandbox_scope import hosted_scope
    from tools.terminal_scope import get_terminal_scope
    from tools.terminal_tool import get_session_cwd

    if hosted_scope() is not None or not uses_managed_executor():
        return None
    task_id = getattr(parent, "_current_task_id", None)
    recorded = get_session_cwd(task_id) if task_id else None
    scope = get_terminal_scope() or {}
    try:
        workspace = reference_workspace(None, directory=continued or recorded)
    except SshWorkspaceFsError as exc:
        raise ValueError(str(exc)) from exc
    if workspace is None:
        raise ValueError("The SSH executor workspace is unavailable.")
    selected = (selected_before if continued and selected_before is not None else
                workspace.cwd != "/workspace" or (not continued and scope.get("TERMINAL_SSH_PROJECT_SELECTED") == "1"))
    return ChildProject(workspace.cwd, selected)


@contextmanager
def child_turn_scope(child):
    from tools.delegate_tool_routes import child_profile_scope

    with child_profile_scope(child):
        project = getattr(child, "_delegate_executor_project", None)
        if not isinstance(project, ChildProject):
            yield
        else:
            with _executor_scope(project):
                yield


@contextmanager
def _executor_scope(project: ChildProject):
    from agent.runtime_cwd import reset_session_cwd, set_session_cwd
    from agent.runtime_scope import bind_terminal_policy
    from hermes_cli.ssh_project_probe import inspect_managed_project
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import build_profile_terminal_scope, get_terminal_scope, reset_terminal_scope

    home = get_hermes_home()
    supplied = get_terminal_scope()
    if supplied is None:
        supplied = build_profile_terminal_scope(home)
    verified = inspect_managed_project(supplied, project.cwd, profile_key=str(home))
    if verified is None or verified[0]["cwd"] != project.cwd:
        raise SshWorkspaceFsError("ENOENT", "The saved child project is unavailable on the SSH executor.")
    terminal = verified[1]
    terminal["TERMINAL_SSH_PROJECT_SELECTED"] = "1" if project.selected else "0"
    terminal_token = bind_terminal_policy(terminal, profile_home=home)
    cwd_token = set_session_cwd(project.cwd)
    try:
        yield
    finally:
        reset_session_cwd(cwd_token)
        reset_terminal_scope(terminal_token)


def managed_child_images(goal, paths, urls, data_urls, *, native, session_id):
    from agent.image_routing import _image_bytes_to_data_url
    from tools.image_source import ImageResolutionError, read_managed_media_path

    hints = []
    attached = []
    for path in paths:
        try:
            image, resolved = read_managed_media_path(path, session_id=session_id)
        except ImageResolutionError:
            continue
        if native:
            url = _image_bytes_to_data_url(image.data, image.mime, PurePosixPath(resolved))
            if not url:
                continue
            attached.append(url)
        hints.append(f"[Image attached at: {resolved}]")
    hints.extend(f"[Image attached: {url}]" for url in urls)
    caption = "\n\n".join((goal, "\n".join(hints))) if hints else goal
    if native and (attached or urls or data_urls):
        return [{"type": "text", "text": caption}, *(
            {"type": "image_url", "image_url": {"url": url}} for url in (*attached, *urls, *data_urls)
        )]
    if hints:
        return caption + "\nUse vision_analyze to inspect these images."
    return goal
