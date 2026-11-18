"""Adopt an operator's prompt refresh request on a continuing session's next turn.

A continuing session replays its stored system prompt verbatim so the provider prefix cache stays
warm, which also means SOUL.md, skills and project-context edits never reach it before ``/new``.
``hermes sessions refresh-prompts`` records a refresh epoch in the profile's state.db. At the start
of the session's next turn this module compares that epoch with the one stamped when the session's
prompt was built (``model_config``) and, when the prompt is older, rebuilds it once through the
normal builder: history, session id and the tools[] pin are kept, ``on_session_start`` does not
fire, and the new bytes plus the epoch are persisted so every later turn restores them verbatim.
One prefix-cache break per request.

No request recorded means the stored prompt is reused exactly as before: the only extra work is
one ``state_meta`` point read. Nothing watches files and nothing changes mid-turn.
"""

from __future__ import annotations

import logging
from typing import Any
from persistence.conversation.sessions import PROMPT_MANAGED_TOOLS_KEY as MANAGED_TOOLS_KEY

logger = logging.getLogger(__name__)

# model_config key: the refresh epoch the session's stored prompt already satisfies.
SESSION_EPOCH_KEY = "prompt_refresh_epoch"


def with_prompt_policy(model_config: Any, policy: list[str] | str | None) -> Any:
    """Carry the producing policy when the native row owner writes the prompt."""
    return model_config if policy is None else {**(model_config or {}), MANAGED_TOOLS_KEY: policy}


def build_prompt_with_policy(agent: Any, system_message: Any) -> tuple[str, list[str] | str]:
    """Render against one tool snapshot, retrying if a concurrent refresh changed it."""
    from tools.mcp_tool_agent import _agent_tools_lock
    from toolsets import managed_tool_snapshot_key

    while True:
        with _agent_tools_lock:
            names = getattr(agent, "valid_tool_names", None)
            policy = managed_tool_snapshot_key(agent)
        prompt = agent._build_system_prompt(system_message)
        with _agent_tools_lock:
            if names is getattr(agent, "valid_tool_names", None) and policy == managed_tool_snapshot_key(agent):
                return prompt, policy


def write_saved_system_prompt(agent: Any, prompt: str, policy: list[str] | str | None) -> None:
    """Persist prompt bytes and their producing policy together in the owning store."""
    from agent.agent_init_session import agent_interface

    store = agent_interface(agent)
    writer = store.sessions if store is not None else agent._session_db
    if policy is None:
        writer.update_system_prompt(agent.session_id, prompt)
    else:
        writer.update_system_prompt(agent.session_id, prompt, managed_tools=policy)


def requested_epoch(agent: Any) -> int:
    """Newest refresh epoch recorded for the agent's session; 0 without a request (or a store that
    keeps none, such as a managed worker's facade)."""
    from agent.agent_init_session import agent_interface

    store = agent_interface(agent)
    reader = (store.sessions.prompt_refresh_epoch if store is not None
              else getattr(getattr(agent, "_session_db", None), "prompt_refresh_epoch", None))
    session_id = getattr(agent, "session_id", None)
    if reader is None or not session_id:
        return 0
    try:
        epoch = reader(session_id)
    except Exception:
        logger.warning("Prompt refresh epoch read failed (session=%s); reusing the stored prompt.",
                       session_id, exc_info=True)
        return 0
    return epoch if isinstance(epoch, int) else 0


def _applied_epoch(session_row: Any) -> int:
    from persistence.conversation.sessions import parse_model_config

    value = parse_model_config((session_row or {}).get("model_config")).get(SESSION_EPOCH_KEY)
    return value if isinstance(value, int) else 0


def stored_prompt_refresh_epoch(agent: Any, session_row: Any) -> int:
    """The epoch to rebuild the stored prompt at, or 0 when the stored prompt is current."""
    epoch = requested_epoch(agent)
    if epoch and _applied_epoch(session_row) < epoch:
        return epoch
    agent._prompt_refresh_epoch = epoch
    return 0


def stored_prompt_policy_changed(agent: Any, session_row: Any) -> bool:
    """Compare the policy that produced stored guidance with the current tool snapshot."""
    from persistence.conversation.sessions import parse_model_config
    from toolsets import managed_tool_snapshot_key

    saved = parse_model_config((session_row or {}).get("model_config")).get(MANAGED_TOOLS_KEY, [])
    agent._prompt_managed_tools = saved
    return saved != managed_tool_snapshot_key(agent)


def stamp_prompt_epoch(agent: Any, epoch: int, *, producing_policy: list[str] | str | None = None) -> None:
    """Record that the agent's freshly built prompt satisfies ``epoch`` (read BEFORE the build, so a
    request that lands mid-build still fires on the next turn)."""
    from toolsets import managed_tool_snapshot_key

    policy = managed_tool_snapshot_key(agent) if producing_policy is None else producing_policy
    if not epoch or _persist_stamp(agent, {SESSION_EPOCH_KEY: epoch}):
        agent._prompt_refresh_epoch = epoch
        agent._prompt_managed_tools = policy


def _persist_stamp(agent: Any, fields: dict) -> bool:
    from agent.agent_init_session import agent_interface

    db = getattr(agent, "_session_db", None)
    if not db:
        return True
    store = agent_interface(agent)
    try:
        (store.sessions.patch_model_config if store is not None
         else db.patch_session_model_config)(agent.session_id, fields)
    except Exception:
        logger.warning("Could not record prompt refresh metadata (session=%s); the refresh may re-fire "
                       "next turn.", agent.session_id, exc_info=True)
        return False
    return True


def rebuild_for_refresh(agent: Any, system_message: Any, conversation_history: Any, epoch: int,
                        *, policy_changed: bool = False) -> None:
    """Rebuild the session's prompt from current sources and persist it with ``epoch``."""
    from agent.prompt_builder import clear_skills_system_prompt_cache
    from agent.surface_switch import stage_surface_switch_note
    from hermes_cli.observability.shared_metrics_efficiency import record_cache_break

    logger.info("Rebuilding system prompt for session %s after %s (one-time prefix-cache break).",
                agent.session_id, "a managed tool policy change" if policy_changed else f"operator request {epoch}")
    # Same boundary compaction uses: memory reloads from disk, plugin sections re-render.
    agent._invalidate_system_prompt()
    agent._auto_load_skills_resolved = False  # pinned skills (skills.auto_load) re-read too
    # As in the Bot Chat capability refresh: build the index from disk, not a cached render.
    clear_skills_system_prompt_cache(clear_snapshot=True)
    agent._cached_system_prompt, producing_policy = build_prompt_with_policy(agent, system_message)
    record_cache_break(agent, "system_prompt_rebuild")
    stage_surface_switch_note(agent, agent._cached_system_prompt, conversation_history)
    db = getattr(agent, "_session_db", None)
    if db:
        try:
            policy = producing_policy if producing_policy or getattr(agent, "_prompt_managed_tools", []) else None
            write_saved_system_prompt(agent, agent._cached_system_prompt, policy)
        except Exception:
            logger.warning("Could not persist the refreshed system prompt (session=%s); the refresh "
                           "re-fires next turn.", agent.session_id, exc_info=True)
            return
    stamp_prompt_epoch(agent, epoch, producing_policy=producing_policy)


def refresh_warm_prompt(agent: Any, system_message: Any, conversation_history: Any) -> None:
    """An agent kept warm across turns (gateway agent cache, TUI/Desktop) adopts a refresh request
    recorded after its prompt was set. Agents whose prompt did not come from the restore/build path
    (seeded maintenance agents, forks) carry no epoch and are left alone."""
    seen = getattr(agent, "_prompt_refresh_epoch", None)
    if agent._cached_system_prompt is None or not isinstance(seen, int):
        return
    from toolsets import managed_tool_snapshot_key

    policy_changed = getattr(agent, "_prompt_managed_tools", []) != managed_tool_snapshot_key(agent)
    if not conversation_history and not policy_changed:
        return
    epoch = requested_epoch(agent)
    if epoch > seen or policy_changed:
        rebuild_for_refresh(agent, system_message, conversation_history, epoch, policy_changed=policy_changed)
