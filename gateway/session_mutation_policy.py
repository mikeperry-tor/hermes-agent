"""Prepare the policy-only session edits: session-scope reasoning and the workspace (Move to project).

Both rewrite the frozen LocalSessionPolicy the next turn runs under, re-bound like a model switch
(policy identity covers config/cwd, so its config-secret reference is re-fingerprinted).
"""
import asyncio
import json
from dataclasses import asdict, replace

from persistence.core.errors import AuthorityRefusal


def _rebind(authority, prepared, old, policy, **extra):
    from gateway.session_policy import bind_launch_key, launch_key
    from gateway.session_policy_credentials import recover_config_secrets
    secrets = recover_config_secrets(authority, old) if old.config_secret_ref else {}
    policy = bind_launch_key(authority, prepared['snapshot']['receipt']['session_id'], policy,
                             launch_key(authority, old), config_secrets=secrets)
    return dict(prepared, policy=asdict(policy), **extra)


async def prepare_reasoning(authority, live, payload, prepared):
    """``/reasoning <level>`` for this session: the level ``build_policy`` freezes at creation. ``None``
    (``/reasoning reset``) gives the level back to the profile."""
    from gateway.session_policy import pin_reasoning_effort, profile_config, restore_policy, unpin_reasoning
    old = restore_policy(prepared['snapshot']['receipt']['policy'])
    frozen = old.config()
    if payload['effort'] is not None:
        pin_reasoning_effort(frozen, payload['effort'], session=True)
    else:
        # A bypass launch never reads the profile: its reset is the built-in default.
        profile = {} if old.ignore_user_config else await asyncio.to_thread(profile_config, authority)
        unpin_reasoning(frozen, profile)
    return _rebind(authority, prepared, old, replace(old, config_json=json.dumps(frozen)))


async def prepare_workspace(authority, live, payload, prepared):
    """Move to project: execution cwd and TERMINAL_CWD move together (restore_policy's invariant)."""
    import os
    from gateway.session_policy import restore_policy
    from hermes_cli.ssh_project_probe import inspect_managed_project
    from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError
    from hermes_constants import translate_cwd_for_wsl_backend
    from tui_gateway import git_probe
    old = restore_policy(prepared['snapshot']['receipt']['policy'])
    terminal = json.loads(old.terminal_json)

    def probe():
        try:
            project = inspect_managed_project(terminal, payload['cwd'], profile_key=str(authority.profile_id),
                                              git_timeout=git_probe._GIT_TIMEOUT)
        except SshWorkspaceFsError as exc:
            raise AuthorityRefusal('invalid_params', field='cwd', detail=str(exc)) from exc
        if project is not None:
            facts, _ = project
            # CLI history scopes by the checkout. The sidebar probes the common repo separately.
            return facts['cwd'], facts.get('branch', ''), facts.get('worktree_root', ''), True
        if terminal.get('TERMINAL_SSH_PROJECT_SELECTED') in ('0', '1'):
            raise AuthorityRefusal('invalid_params', field='cwd', detail='The managed SSH workspace is unavailable.')
        cwd = os.path.abspath(os.path.expanduser(translate_cwd_for_wsl_backend(payload['cwd'])))
        if not os.path.isdir(cwd):
            raise AuthorityRefusal('invalid_params')
        return cwd, git_probe.branch(cwd), git_probe.common_repo_root(cwd), False
    cwd, branch, root, managed = await asyncio.to_thread(probe)
    terminal['TERMINAL_CWD'] = cwd
    if managed:
        terminal['TERMINAL_SSH_PROJECT_SELECTED'] = '1'
    policy = replace(old, cwd=cwd, terminal_json=json.dumps(terminal))
    return _rebind(authority, prepared, old, policy, git={'branch': branch, 'root': root})


async def prepare_edit_policy(authority, live, payload, prepared):
    """Main's ACP modes (Default / Accept Edits / Don't Ask) as the session's frozen edit approval
    policy. Only an ACP session's edits ask an editor; selecting the current policy writes nothing."""
    from gateway.session_policy import restore_policy
    old = restore_policy(prepared['snapshot']['receipt']['policy'])
    if old.source != 'acp':
        raise AuthorityRefusal('invalid_params', field='payload.policy',
                               detail='only an ACP session has an editor edit approval policy')
    if old.edit_approval_policy == payload['policy']:
        return {'status': 'noop', 'edit_approval_policy': old.edit_approval_policy}
    return _rebind(authority, prepared, old, replace(old, edit_approval_policy=payload['policy']))


PREPARE = {'reasoning': prepare_reasoning, 'workspace': prepare_workspace, 'edit_policy': prepare_edit_policy}
