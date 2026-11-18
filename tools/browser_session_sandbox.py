"""The bot's Chromium INSIDE the terminal backend's sandbox (Bot Desktop placed there).

Same browser a human takes over in the pane: the sandbox image's Playwright Chromium on the profile the
dock's Browser icon uses, on the sandbox screen's DISPLAY. Its DevTools port is the sandbox's loopback,
unreachable from this host (Docker bridge / ssh remote), so it is forwarded over the sandbox's exec
stream; Chromium's DevTools accepts any loopback ``Host`` header, port included.
"""

from __future__ import annotations

from hashlib import sha256
import logging
import os
import shlex
import subprocess
import threading
import time
from typing import Any, NamedTuple, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_LAUNCH_TIMEOUT_S = 60.0
class _Launch(NamedTuple):
    """The sandbox, process identity, profile and endpoint this launcher owns."""
    env: Any
    pid: int
    start: Optional[str]
    profile: str
    devtools_path: Optional[str] = None


_launched: dict[str, _Launch] = {}
# Lock hierarchy: _key_lock(key) -> _cleanup_lock or _launched_lock. Never acquire
# _key_lock(key) while holding _cleanup_lock or _launched_lock.
_launched_lock = threading.Lock()
# TERM can return while DevTools still listens. Keep the exact retiring endpoint
# until a probe sees it gone or a replacement endpoint appears. Key lock owns this map.
_retiring: dict[str, tuple[Optional[str], tuple[int, str, str]]] = {}
# Per shared key: held across "is a browser running?" + launch + claim, and across the stop, so two sessions
# first using one container never both see no browser and both launch.
_key_locks: dict[str, threading.Lock] = {}
# Endpoints a liveness probe confirmed: ``cdp_url -> (id(env), sandbox port, forward failures then, when)``. A
# browser call re-probes after a connection through its forward failed, the sandbox was replaced, or the
# confirmation is older than _CONFIRMED_TTL_S (a relaunch on the same port that nothing connected to yet
# leaves no failure behind), so a healthy call spends no exec in the sandbox.
_confirmed: dict[str, tuple[int, int, int, float]] = {}
_CONFIRMED_TTL_S = 30.0
# Exit code of the liveness script when the sandbox answers "not running": no DevTools port file, or nothing
# listens on its port. Any other failure (docker/ssh transport error, timeout) says nothing about Chromium.
_GONE_RC = 3
GONE = "gone"


def _sandbox():
    from tools.bot_desktop import runtime as _bd_runtime
    return _bd_runtime.tool_sandbox()


_DEVTOOLS_PROBE = f"""
import json
import sys
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlsplit

try:
    with open(sys.argv[1], encoding="utf-8") as active:
        lines = active.read(4096).splitlines()
except FileNotFoundError:
    raise SystemExit({_GONE_RC})
if len(lines) < 2 or not lines[0].strip().isdigit():
    raise SystemExit({_GONE_RC})
port, path = int(lines[0]), lines[1].strip()
if not 0 < port < 65536 or not path.startswith("/devtools/browser/"):
    raise SystemExit({_GONE_RC})
conn = HTTPConnection("127.0.0.1", port, timeout=2)
try:
    conn.request("GET", "/json/version")
    response = conn.getresponse()
    version = json.loads(response.read(65536)) if response.status == 200 else {{}}
    endpoint = urlsplit(str(version.get("webSocketDebuggerUrl", ""))) if isinstance(version, dict) else None
except TimeoutError:
    raise SystemExit(1)
except (OSError, HTTPException, ValueError):
    raise SystemExit({_GONE_RC})
finally:
    conn.close()
if endpoint is None or endpoint.scheme not in ("ws", "wss") or endpoint.path != path:
    raise SystemExit({_GONE_RC})
print(port)
print(path)
"""


def _live_devtools(env, user, udd: str):
    """One exec verifies Chromium's reported browser path matches the profile's endpoint file.
    GONE means the recorded endpoint is missing or invalid. None leaves transport errors and
    response timeouts inconclusive."""
    from tools.environments import streams
    try:
        proc = streams.run_in(env, ["python3", "-c", _DEVTOOLS_PROBE, f"{udd}/DevToolsActivePort"],
                              user=user, timeout=10)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.debug("sandbox browser liveness probe failed: %s", exc)
        return None
    if proc.returncode == _GONE_RC:
        return GONE
    lines = proc.stdout.decode("utf-8", "replace").splitlines() if proc.returncode == 0 else []
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/devtools/browser/"):
        return int(lines[0].strip()), lines[1].strip()
    logger.debug("sandbox browser liveness probe inconclusive (exit %s)", proc.returncode)
    return None


def _shared_key(env, udd: str) -> str:
    """The launcher key every session on this container's Chromium shares. Keyed by the container, never by
    profile: profiles on one ``terminal.docker_shared_container_key`` share one browser, and each
    ``container_persistent: false`` session has its own. The env object is the container's identity in this
    process (its port forwards keep it alive, so the id is never reused while a session names it)."""
    return f"sandbox:{id(env):x}:{udd}"


# After stripping ``pid (comm) `` from /proc/<pid>/stat, positional field $4 is
# ``sid`` (proving setsid session leadership) and ${20} is ``starttime`` (clock
# ticks since boot, /proc/<pid>/stat field 22). Empty when /proc is absent.
_START_TIME_OF_P = 's=$(cat /proc/"$p"/stat 2>/dev/null) && s=${s##*")"} && set -- $s && printf %s "${20}"'
# Exit code of the stop script when the pid is gone or no longer the browser this process launched.
_NOT_OURS_RC = 4
_LAUNCH_OWNER_NONCE = uuid4().hex
_LAUNCH_OWNER_ARG = "--hermes-chromium-launch-owner="


def _launch_owner_stamp(profile: str) -> str:
    # Forked workers share imported modules but have distinct launch authority.
    return sha256(f"{_LAUNCH_OWNER_NONCE}:{os.getpid()}:{profile}".encode()).hexdigest()


_RECOVER_LAUNCH = r"""
profile=$1
owner=$2
match_launch() {
    p=$1
    case "$p" in ""|*[!0-9]*) return 1 ;; esac
    [ "$p" != "$$" ] || return 1
    # Chromium may rewrite argv as one string. Normalize the NUL separators
    # and check the opaque marker as a complete flag in either representation.
    args=$(tr '\000' ' ' < "/proc/$p/cmdline" 2>/dev/null) || return 1
    case " $args " in *" $owner "*) ;; *) return 1 ;; esac
    case " $args " in *" --user-data-dir=$profile "*) ;; *) return 1 ;; esac
    stat=$(cat "/proc/$p/stat" 2>/dev/null) || return 1
    # After stripping "pid (comm) ", $4 is sid (setsid leader) and ${20} is starttime.
    set -- ${stat##*) }
    [ "$4" = "$p" ] && [ -n "${20}" ] || return 1
    printf '%s %s\n' "$p" "${20}"
}
singleton="$1/SingletonLock"
if lock=$(readlink -- "$singleton") && launch=$(match_launch "${lock##*-}") &&
        [ "$(readlink -- "$singleton")" = "$lock" ]; then
    printf '%s\n' "$launch"
    exit 0
fi
# Before the endpoint is ready, a detached process can already be running
# with this stamp while SingletonLock is absent or still names a dead process.
if [ "$3" = pending ]; then
    for file in /proc/[0-9]*/cmdline; do
        [ -r "$file" ] || continue
        pid=${file#/proc/}
        if match_launch "${pid%/cmdline}"; then exit 0; fi
    done
fi
exit 4
"""


def _recover_launch(env, user, profile: str, devtools_path: str | None = None) -> _Launch | None:
    """Recover this process's launch if a transport lost the detached PID reply."""
    from tools.environments import streams

    owner = _LAUNCH_OWNER_ARG + _launch_owner_stamp(profile)
    state = "pending" if devtools_path is None else "ready"
    proc = streams.run_in(env, ["bash", "-c", _RECOVER_LAUNCH, "--", profile, owner, state],
                          user=user, timeout=10)
    if proc.returncode == _NOT_OURS_RC:
        return None
    words = proc.stdout.decode("utf-8", "replace").strip().split()
    if proc.returncode != 0 or len(words) != 2 or not all(word.isdigit() for word in words):
        raise RuntimeError("Could not verify the sandbox Chromium launcher; retry the browser command")
    return _Launch(env, int(words[0]), words[1], profile, devtools_path)


_MATCH_START = f'p=$1; t=$({_START_TIME_OF_P}); [ -n "$t" ] && [ "$t" = "$2" ] || exit {_NOT_OURS_RC}; '
_MATCH_SINGLETON = (
    f'lock=$(readlink -- "$3/SingletonLock") || exit {_NOT_OURS_RC}; '
    f'case "$lock" in *-"$p") ;; *) exit {_NOT_OURS_RC};; esac; '
)
# Before profile initialization the original argv can prove the profile. A
# running Chromium can flatten its title; the native lock still names its PID.
_STOP_SCRIPT = (
    _MATCH_START
    + 'ok=; while IFS= read -r -d "" a; do [ "$a" = "--user-data-dir=$3" ] && ok=1; done < /proc/"$p"/cmdline; '
    + 'if [ -z "$ok" ]; then ' + _MATCH_SINGLETON + 'fi; kill -TERM "$p"'
)


def _owns_discovered_browser(launch: _Launch, user) -> bool:
    """Check the recorded process and Chromium's native profile singleton."""
    from tools.environments import streams

    if launch.start is None:
        return False
    command = ["bash", "-c", _MATCH_START + _MATCH_SINGLETON, "hermes-sandbox-browser-owner",
               str(launch.pid), launch.start, launch.profile]
    proc = streams.run_in(launch.env, command, user=user, timeout=10)
    if proc.returncode not in (0, _NOT_OURS_RC):
        raise RuntimeError("The sandbox did not confirm Chromium's profile owner; retry the browser command")
    return proc.returncode == 0


def _start_chromium(env, user, exe: str, udd: str, child_env: dict, *, owner: str | None = None) -> tuple[int, Optional[str]]:
    """Launch detached in the sandbox; returns its sandbox pid and start time (None when unreadable)."""
    from tools.bot_desktop.browser import SHARED_PROFILE_ARGS
    from tools.browser_launcher import CHROMIUM_SANDBOX_BYPASS_ARGS, build_chromium_argv, chromium_sandbox_required
    from tools.environments import streams
    # Preserve container defaults unless the operator requires Chromium sandboxing.
    # Host sandbox detection cannot tell us whether remote user namespaces work.
    # Deduplicate only our injected standalone flags so two-token flags in user
    # browser.chromium_args (e.g. ["--flag-a", "0", "--flag-b", "0"]) keep their values.
    extra = list(dict.fromkeys((*SHARED_PROFILE_ARGS,
                                *(() if chromium_sandbox_required() else CHROMIUM_SANDBOX_BYPASS_ARGS))))
    argv = build_chromium_argv(exe, udd, packaged=False, headed=True, sandbox_bypass=False, extra_args=extra)
    if owner is not None:
        owner_arg = _LAUNCH_OWNER_ARG + owner
        if owner_arg not in argv:
            argv.insert(-1, owner_arg)
    # setsid execs in place (a background child of a non-interactive shell leads no process group), so $! is
    # Chromium itself, and its start time is fixed from the fork on.
    # Do not delete DevToolsActivePort before launch: Chromium owns that file, and
    # the dock Browser icon may publish a live endpoint while this launch starts.
    script = (f"mkdir -p {shlex.quote(udd)} && "
              f"(setsid {shlex.join(argv)} >/dev/null 2>&1 & p=$!; echo \"$p $({_START_TIME_OF_P})\")")
    proc = streams.run_in(env, ["bash", "-c", script], child_env=child_env, user=user, timeout=30)
    last = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] if proc.returncode == 0 else []
    words = last[0].split() if last else []
    if not words or not words[0].isdigit():
        raise RuntimeError(f"Chromium could not be started in the sandbox: {proc.stderr.decode('utf-8', 'replace')[-500:]}")
    start = words[1] if len(words) > 1 and words[1].isdigit() else None
    return int(words[0]), start


def _wait_for_port(env, user, udd: str) -> tuple[int, str]:
    deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline:
        found = _live_devtools(env, user, udd)
        if found is not None and found is not GONE:
            return found
        time.sleep(0.5)
    raise RuntimeError("Chromium in the sandbox did not expose a DevTools endpoint in time")


def _key_lock(key: str) -> threading.Lock:
    with _launched_lock:
        return _key_locks.setdefault(key, threading.Lock())


def _spawn_owned(env, user, udd: str, key: str, published: dict) -> int:
    """Launch the sandbox Chromium and own it under ``key`` from the moment it exists, before its port is
    up, so stop_sandbox_browser can always stop it; returns its sandbox pid."""
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    exe = sandbox_host.chromium_executable(env)
    if not exe:
        raise RuntimeError("the sandbox image has no Chromium to run the browser")
    child_env = {**published, "TMPDIR": streams.sandbox_temp_dir(env)}
    pid, start = _start_chromium(env, user, exe, udd, child_env, owner=_launch_owner_stamp(udd))
    with _launched_lock:
        _launched[key] = _Launch(env, pid, start, udd)
    return pid


def create_sandbox_session(cache_key: str) -> dict[str, Any]:
    """Session on the sandbox Chromium (attach to the running one, else launch it)."""
    from tools.bot_desktop import sandbox_host
    from tools.browser_session import _cleanup_lock, _pending_sandbox_attachments, _session_record
    from tools.environments import streams
    env, published = _sandbox()
    user = sandbox_host._user_for(env)
    udd = sandbox_host.browser_profile_dir(env)
    key = _shared_key(env, udd)
    with _key_lock(key):
        found = _live_devtools(env, user, udd)
        _check_retiring_endpoint(key, found, env)
        if found is None:
            # An unresponsive probe proves nothing about the active desktop.
            raise RuntimeError("the terminal backend's sandbox did not answer whether its browser is running; "
                               "try again")
        with _launched_lock:
            existing = _launched.get(key)
        if (existing is not None and (found is GONE or existing.devtools_path != found[1])
                and _owns_discovered_browser(existing, user)):
            # A missing or changed endpoint cannot transfer ownership of a live profile.
            raise RuntimeError("The existing Chromium still owns the profile but its DevTools endpoint "
                               "is unavailable; close its browser session and retry")
        spawned: Optional[int] = None
        try:
            if found is GONE:
                recovered = _recover_launch(env, user, udd)
                if recovered is None:
                    spawned = _spawn_owned(env, user, udd, key, published)
                else:
                    # Its profile is already locked. A second launch would be
                    # forwarded to that process and could open another window.
                    spawned = recovered.pid
                    with _launched_lock:
                        _launched[key] = recovered
                found = _wait_for_port(env, user, udd)
                with _launched_lock:
                    candidate = _launched[key]
                if _owns_discovered_browser(candidate, user):
                    with _launched_lock:
                        _launched[key] = candidate._replace(devtools_path=found[1])
            with _launched_lock:
                launched = _launched.get(key)
            if launched is None or launched.devtools_path != found[1]:
                # A transport may have lost the PID reply after detaching.
                # A dock browser has no stamp from this launcher and stays borrowed.
                launched = _recover_launch(env, user, udd, found[1])
                if launched is not None and _live_devtools(env, user, udd) != found:
                    raise RuntimeError("Chromium changed while recovering its launcher; retry the browser command")
                with _launched_lock:
                    if launched is None:
                        _launched.pop(key, None)
                    else:
                        _launched[key] = launched
            local_port = streams.forward_port(env, found[0], user=user)
            info = _session_record("sb", f"ws://127.0.0.1:{local_port}{found[1]}", {"local": True, "sandbox": True})
            _confirm(info["cdp_url"], env, found[0])
            info.update(shared_profile=True, launcher_key=key)
            if launched is not None:
                info["sandbox_launch"] = launched[1:3]
                with _cleanup_lock:
                    _pending_sandbox_attachments[info["session_name"]] = (key, info["sandbox_launch"])
        except BaseException:
            if spawned is not None:  # a timeout, a failed forward or an interrupt must not leave it running unowned
                _stop_launched(key)
            raise
    logger.info("Created sandbox browser session %s for %s", info["session_name"], cache_key)
    return info


def _check_retiring_endpoint(key: str, found, env) -> None:
    retiring = _retiring.get(key)
    if retiring is None:
        return
    path, launch = retiring
    endpoint = found[1] if isinstance(found, tuple) else None
    # The previous stop may have lost its connection. A different endpoint
    # cannot confirm that the exact retired launch has released the profile.
    still_owned = _signal_launch((env, *launch))
    if still_owned or (path is not None and endpoint == path):
        raise RuntimeError("The previous Chromium is still shutting down; retry the browser command")
    if found is not None:
        _retiring.pop(key, None)


def _confirm(cdp_url: str, env, port: int) -> None:
    from tools.environments import streams
    _confirmed[cdp_url] = (id(env), port, streams.forward_failures(env, port), time.monotonic())


def sandbox_browser_alive(session_info: dict[str, Any]) -> bool:
    """The sandbox Chromium still serves this session's endpoint through the current port forward: False once
    the sandbox answers that it died, was relaunched (new DevTools path) or the sandbox was replaced (new
    forward). Re-probed only after a connection through the forward failed or the last confirmation expired;
    a probe that cannot reach the sandbox keeps the session."""
    from tools.bot_desktop import runtime as _bd_runtime, sandbox_host
    from tools.environments import streams
    env = _bd_runtime._owned_sandbox_env(reattach=False)
    if env is None:
        return False
    cdp_url = session_info.get("cdp_url") or ""
    confirmed = _confirmed.get(cdp_url)
    if (confirmed and confirmed[0] == id(env) and streams.forward_failures(env, confirmed[1]) == confirmed[2]
            and time.monotonic() - confirmed[3] < _CONFIRMED_TTL_S):
        return True
    try:
        user = sandbox_host._user_for(env)
        udd = sandbox_host.browser_profile_dir(env)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.debug("sandbox browser liveness probe failed: %s", exc)
        return True
    found = _live_devtools(env, user, udd)
    if found is None:
        return True
    if found is GONE or cdp_url != f"ws://127.0.0.1:{streams.forward_port(env, found[0], user=user)}{found[1]}":
        _confirmed.pop(cdp_url, None)
        return False
    _confirm(cdp_url, env, found[0])
    return True


def stop_sandbox_browser(session_info: dict[str, Any]) -> None:
    """Release this record and stop only its own Chromium when it has no other users."""
    from tools.browser_session import _active_sessions, _cleanup_lock, _pending_sandbox_attachments

    key = session_info.get("launcher_key") or ""
    launch = session_info.get("sandbox_launch")
    with _key_lock(key):
        with _cleanup_lock:
            _pending_sandbox_attachments.pop(session_info.get("session_name"), None)
            if (key, launch) in _pending_sandbox_attachments.values() or any(
                info is not session_info and info.get("launcher_key") == key
                and info.get("sandbox_launch") == launch for info in _active_sessions.values()
            ):
                return
        with _launched_lock:
            launched = _launched.get(key)
        if launched is None or launched[1:3] != launch:
            return
        _confirmed.pop(session_info.get("cdp_url") or "", None)
        _stop_launched(key)


def _stop_launched(key: str) -> None:
    with _launched_lock:
        launched = _launched.pop(key, None)
    if launched is not None:
        if launched.start is not None:
            _retiring[key] = (launched.devtools_path, (launched.pid, launched.start, launched.profile))
        _signal_launch(launched[:4])


def _signal_launch(launched: tuple[Any, int, Optional[str], str]) -> bool:
    """Signal the exact launch. False only when it cannot still own this retirement."""
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams
    env, pid, start, udd = launched
    if start is None:
        logger.warning("browser: leaving sandbox Chromium pid %s running: its start time could not be read at "
                       "launch, so the pid cannot be proven to still be that browser", pid)
        return False
    argv = ["bash", "-c", _STOP_SCRIPT, "hermes-sandbox-browser-stop", str(int(pid)), start, udd]
    try:
        proc = streams.run_in(env, argv, user=sandbox_host._user_for(env), timeout=15)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.debug("sandbox browser stop for pid %s failed: %s", pid, exc)
        return True
    if proc.returncode == _NOT_OURS_RC:
        logger.info("browser: not signalling sandbox pid %s: it is gone or no longer the Chromium launched on %s",
                    pid, udd)
        return False
    if proc.returncode != 0:
        logger.debug("sandbox browser stop for pid %s exited %s", pid, proc.returncode)
    return True
