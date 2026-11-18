"""Deployment tool denials, a hosted tool allowlist, and hosted host-state feature gates.

In a hosted scope nothing tenant-supplied may execute on this host or touch its filesystem, and no
agent may read or write state this host keeps for its profile. Two decisions enforce that. Each is
made in one function and is deny-by-default, so a new tool or feature stays off in a hosted scope
until someone shows it acts only through the agent's sandbox:

* :func:`tool_refusal` — a tool runs in a hosted scope only if it is on
  :data:`HOSTED_TOOL_ALLOWLIST`, the tools whose every effect goes through the agent's own sandbox
  (``tools.environments.sandbox_scope``). Everything else (memory, recall, todo, vision, browser, web,
  MCP, connectors, catalog, delegation, skills, cron, ...) is refused before its handler. The agent
  loop asks once per call, in ``agent.tool_executor._dispatch_authorized_once``: the one function every
  sequential, concurrent and batched call goes through, whatever executes it (inline agent tools,
  memory-provider and context-engine tools, the registry, connectors). The execution primitives also
  ask, for callers outside the loop: ``ToolRegistry.dispatch`` (execute_code RPC, the session kernel,
  the MCP bridge, plugin ``dispatch_tool``), the inline-executor table and connector dispatch.
* :func:`host_feature_enabled` — the agent features outside tool dispatch that read or write this
  host's state for the profile, or run here (:data:`HOST_FEATURES`). Every one is off in a hosted
  scope; each site asks this one function. They come back per agent through storage ports.

A local install retains its features. An optional managed tools.disabled list denies named tools
across discovery and dispatch without opting into the hosted allowlist. Profiles cannot relax it.
"""

from __future__ import annotations

from typing import Callable, Optional

CURRENT_MANAGED_POLICY = object()
MANAGED_TOOL_POLICY_ERROR = "Managed tool policy could not be read or validated. Contact the deployment administrator."

# Tools whose every effect goes through the agent's ScopedEnvironment and nothing on this host:
#   terminal        commands run by env.execute in the sandbox; background spawns via spawn_via_env,
#                   their records stamped with the sandbox key.
#   process_manage  acts only on the bound agent's own records (sandbox_visible); polls via its env.
#   execute_code    always the remote kernel / per-call script in the sandbox, never the host kernel;
#                   its RPC tool calls re-enter ToolRegistry.dispatch, so they meet this allowlist too.
#   read_file, write_file, patch, search_files
#                   ShellFileOperations over the sandbox env: no host fast path, a lexical path policy,
#                   no host document extraction; an oversized result spills into the sandbox.
HOSTED_TOOL_ALLOWLIST = frozenset({
    "terminal", "process_manage", "execute_code", "read_file", "write_file", "patch", "search_files",
})

# Agent features outside tool dispatch that read or write this host's profile state, or run here.
HOST_FEATURES = frozenset({
    "context_files",              # AGENTS.md / CLAUDE.md / .cursorrules / SOUL.md in the system prompt
    "subdirectory_hints",         # AGENTS.md found beside the paths a tool call names
    "context_references",         # @file / @diff / @url expansion of the user's message
    "workspace_snapshot",         # git and project facts for the system prompt
    "backend_probe",              # a throwaway environment built to describe the backend
    "builtin_memory",             # MEMORY.md / USER.md and external memory providers
    "session_recall",             # the profile's state.db opened for recall
    "background_review",          # the post-turn memory/skill review fork
    "trajectory_export",          # trajectory JSONL in the working directory
    "file_mutation_bookkeeping",  # host stat of a failed write's target
    "checkpoints",                # shadow-git checkpoints and rollback of the working directory
    "media_processing",           # images/video decoded, converted and cached on this host
    "skills",                     # the profile's skill files: index, preload, auto-load, slash commands
    "skill_inline_shell",         # !`cmd` blocks in a skill body
    "cron_scripts",               # a cron job's pre-run script
    "shell_hooks",                # operator shell hooks: a host process fed the event payload
    "tool_host_artifacts",        # files an allowed tool keeps here about a call: terminal output
                                  # spill, saved blocked scripts, the verification ledger
    "mcp_stdio",                  # a profile MCP server started as a host process
    "media_delivery",             # a reply attachment named by host path
    "transcription",              # audio read and speech engines run here
    "delegation",                 # child agents started outside a tool call (/review)
})


class HostedToolRefused(PermissionError):
    """A hosted scope asked for something that would run or read on this host."""


def _hosted() -> bool:
    from tools.environments.sandbox_scope import hosted_scope
    return hosted_scope() is not None


def _message(what: str, why: str) -> str:
    return (f"{what} is not available to a hosted agent: {why}. A hosted agent's tools are "
            f"{', '.join(sorted(HOSTED_TOOL_ALLOWLIST))}, all run in its own sandbox.")


def managed_disabled_tools() -> Optional[frozenset[str]]:
    """Managed-only tool names; None means policy is unreadable/invalid, not unrestricted.

    Intentionally managed-only (``/etc/hermes/config.yaml``) and omitted from user ``DEFAULT_CONFIG``.
    Native managed scope owns location, file caching and config-backend authority; existing profile
    ``disabled_toolsets`` can narrow selection but cannot undo this ceiling.
    """
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.managed_scope import load_managed_config
    from hermes_yaml import YAMLError
    try:
        config = load_managed_config(strict=True)
        options = config.get("tools", {})
        if not isinstance(options, dict):
            raise ValueError("managed tools must be a mapping")
        names = options.get("disabled", [])
        if not isinstance(names, list) or any(not isinstance(name, str) or not name or name != name.strip() for name in names):
            raise ValueError("managed tools.disabled must be a list of nonempty tool names")
        return frozenset(names)
    except (ConfigBackendUnavailable, OSError, ValueError, YAMLError):
        return None


def _tool_refusal(tool_name: str, disabled: Optional[frozenset[str]]) -> Optional[str]:
    if _hosted() and tool_name not in HOSTED_TOOL_ALLOWLIST:
        return _message(tool_name, "only tools that act solely through the agent's sandbox are enabled")
    if disabled is None:
        return f"Tool execution is unavailable. {MANAGED_TOOL_POLICY_ERROR}"
    if tool_name in disabled:
        return "This tool is disabled by managed deployment policy."
    return None


def tool_refusal(tool_name: str) -> Optional[str]:
    """Authorize dispatch against current deployment policy."""
    return _tool_refusal(tool_name, managed_disabled_tools())


def discovery_tool_refusal(disabled=CURRENT_MANAGED_POLICY) -> Callable[[str], Optional[str]]:
    """Use one managed policy for a discovery operation and every catalog it builds."""
    if disabled is CURRENT_MANAGED_POLICY:
        disabled = managed_disabled_tools()
    return lambda name: _tool_refusal(name, disabled)


def discovery_tool_filter(disabled=CURRENT_MANAGED_POLICY) -> Callable[[str], bool]:
    refusal = discovery_tool_refusal(disabled)
    return lambda name: refusal(name) is None


def host_feature_enabled(feature: str) -> bool:
    """Whether *feature* (one of :data:`HOST_FEATURES`) may use this host's state now: always outside
    a hosted scope, never inside one."""
    if feature not in HOST_FEATURES:
        raise KeyError(f"unknown host feature {feature!r}")
    return not _hosted()


def host_feature_refusal(feature: str, what: str) -> Optional[str]:
    """:func:`host_feature_enabled` as a message for *what*: the refusal in a hosted scope, else ``None``."""
    if host_feature_enabled(feature):
        return None
    return _message(what, f"it uses this host's {feature.replace('_', ' ')}")


def require_host_feature(feature: str, what: str) -> None:
    """Raise :class:`HostedToolRefused` when *feature* is off (a hosted scope); a no-op otherwise."""
    if refusal := host_feature_refusal(feature, what):
        raise HostedToolRefused(refusal)
