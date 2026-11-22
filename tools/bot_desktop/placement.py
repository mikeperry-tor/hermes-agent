"""Where this profile's desktop lives: the gateway host or inside the configured terminal backend.

``bot_desktop.placement``:
  ``auto``      (default) follow the terminal backend when it is a sandbox that can host a stream
                (docker / ssh, or a plugin backend declaring ``supports_streams``); the
                gateway host when ``terminal.backend`` is local. A
                sandbox backend that CANNOT host one (modal, daytona, vercel) resolves to ``refused``:
                the user chose a sandbox for the agent's actions, so quietly running the screen, cua-driver
                and the browser on the host beside it would hand the agent a desktop outside that sandbox.
  ``terminal``  always the terminal backend; error when it cannot host one.
  ``gateway``   always the gateway host (the pre-#108914 behaviour for sandboxed users, now an explicit
                opt-in because it is the boundary-crossing shape).

``resolve()`` is pure config + backend-class reasoning: it never starts a sandbox. ``terminal_environment()``
does acquire the profile's terminal environment (creating the container if needed) because a screen cannot
exist before the sandbox does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

GATEWAY = "gateway"
TERMINAL = "terminal"
REFUSED = "refused"
_STREAM_BACKENDS = ("docker", "ssh")


@dataclass(frozen=True)
class Placement:
    where: str            # GATEWAY | TERMINAL | REFUSED
    backend: str          # terminal.backend as configured
    reason: str = ""      # human sentence when REFUSED (or why TERMINAL was not possible)


def _setting() -> str:
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly().get("bot_desktop") or {}
    value = str(cfg.get("placement") or "auto").strip().lower()
    return value if value in ("auto", TERMINAL, GATEWAY) else "auto"


def executor_only() -> bool:
    """Deployment-owned selection for the desktop and its browser routes.

    Intentionally managed-only (``/etc/hermes/config.yaml``) and omitted from user ``DEFAULT_CONFIG``.
    """
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.managed_scope import load_managed_config
    from hermes_yaml import YAMLError

    try:
        desktop = load_managed_config(strict=True).get("bot_desktop", {})
        if not isinstance(desktop, dict) or type(desktop.get("executor_only", False)) is not bool:
            raise ValueError("managed bot_desktop.executor_only must be boolean")
        return desktop.get("executor_only", False)
    except (ConfigBackendUnavailable, OSError, ValueError, YAMLError) as exc:
        raise RuntimeError("Could not validate the managed desktop policy; contact the deployment administrator") from exc


def cua_executor_ready(*, probe_timeout: float = 4.0, deadline: Optional[float] = None) -> bool:
    """Whether a managed-only terminal executor is reachable and has ``cua-driver``."""
    from tools.bot_desktop import sandbox_host

    return sandbox_host.ready_executor(extra=("cua-driver",), probe_timeout=probe_timeout,
                                       deadline=deadline) is not None


def _terminal_backend() -> str:
    from tools.terminal_tool import _get_env_config
    return str(_get_env_config().get("env_type") or "local")


def _plugin_streams(backend: str) -> bool:
    from tools.terminal_tool_config import _plugin_env_flag
    return _plugin_env_flag(backend, "supports_streams") is True


def _plugin_host_isolated() -> bool:
    # Under host isolation a plugin's environment object reaches Hermes as an opaque placeholder, so its
    # exec_prefix() is unreachable. (No bundled plugin backend declares supports_streams, so bundled
    # plugins, which stay in-process, are not a case here.)
    from hermes_cli.plugin_isolation import ISOLATION_HOST, isolation_mode
    return isolation_mode() == ISOLATION_HOST


def resolve() -> Placement:
    try:
        required = executor_only()
    except RuntimeError as exc:
        return Placement(REFUSED, "", str(exc))
    from tools.terminal_scope import TerminalPolicyUnavailable

    try:
        backend = _terminal_backend()
    except TerminalPolicyUnavailable as exc:
        return Placement(REFUSED, "", str(exc))
    if backend == "local":
        if required:
            return Placement(REFUSED, backend, "The deployment requires a desktop in the terminal executor")
        return Placement(GATEWAY, backend, "terminal.backend is local, so the terminal IS the gateway host")
    setting = TERMINAL if required else _setting()
    if setting == GATEWAY:
        return Placement(GATEWAY, backend)
    if backend in _STREAM_BACKENDS:
        return Placement(TERMINAL, backend)
    if _plugin_streams(backend):
        if not _plugin_host_isolated():
            return Placement(TERMINAL, backend)
        return Placement(REFUSED, backend, (
            f"terminal.backend is {backend}, a plugin that runs in the plugin host (plugins.isolation: host), "
            f"and the host cannot hand its sandbox's exec channel to Hermes, so the screen cannot follow it. "
            f"Set plugins.isolation: in_process, or bot_desktop.placement: gateway to run the desktop on the "
            f"gateway host."))
    reason = (f"terminal.backend is {backend}, which cannot host a screen yet, and running the desktop on the "
              f"gateway host would put the agent's screen, browser and computer_use outside the sandbox you chose "
              f"for it. Set bot_desktop.placement: gateway to allow that explicitly.")
    return Placement(REFUSED, backend, reason)


def terminal_environment(*, create: bool = True) -> Optional[Any]:
    """This profile's terminal environment object (the one ``terminal`` runs commands in). ``create=False``
    only returns an already-running one."""
    from tools import terminal_tool as tt
    task_id = tt._resolve_container_task_id(None)
    with tt._env_lock:
        env = tt._lookup_active_env(task_id, None)
    if env is not None or not create:
        return env
    # Acquire through the same planner the terminal tool uses so the container carries the same mounts,
    # limits and identity label; a trivial command warms it.
    tt.terminal_tool("true", task_id=None)
    with tt._env_lock:
        return tt._lookup_active_env(task_id, None)
