"""Terminal run envs resolve the routed profile's scope under multiplex (#142798).

A host gateway keeps the environment of whatever session started it. Before the fix,
``_make_run_env`` built a served profile's terminal commands from that env alone: the
routed profile's own ``.env`` names only reached the child when the launch process also
defined the name or it was a declared passthrough, and a launch-residue value for the
same name won. The builder now overlays the bound secret scope first — the same
semantics as ``build_subprocess_env(strip_launch_profile=True)``."""

import os

from tools.environments.local import _make_run_env
from tools.process_registry import ProcessRegistry

# A Hermes-managed provider credential name, built at runtime so no credential-shaped
# literal sits in the source: the test only needs the NAME to be blocklisted.
_PROVIDER_KEY = "OPENAI_" + "API_KEY"


def _multiplex(monkeypatch, scope):
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    monkeypatch.setattr("agent.secret_scope.current_secret_scope", lambda: scope)


def test_served_profile_commands_see_their_own_scope(monkeypatch):
    monkeypatch.setenv("MYAPP_AGENT_ID", "from-p1")  # what started the host gateway
    _multiplex(monkeypatch, {"MYAPP_AGENT_ID": "from-p2"})
    env = _make_run_env({})
    assert env["MYAPP_AGENT_ID"] == "from-p2"


def test_single_profile_keeps_the_process_env(monkeypatch):
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: False)
    monkeypatch.setattr("agent.secret_scope.current_secret_scope", lambda: {"MYAPP_AGENT_ID": "from-p2"})
    monkeypatch.setenv("MYAPP_AGENT_ID", "from-p1")
    env = _make_run_env({})
    assert env["MYAPP_AGENT_ID"] == "from-p1"


def test_backend_env_still_beats_the_routed_scope(monkeypatch):
    monkeypatch.setenv("MYAPP_AGENT_ID", "from-p1")
    _multiplex(monkeypatch, {"MYAPP_AGENT_ID": "from-p2"})
    env = _make_run_env({"MYAPP_AGENT_ID": "backend-declared"})
    assert env["MYAPP_AGENT_ID"] == "backend-declared"


def test_scope_values_pass_the_same_credential_scrub(monkeypatch):
    _multiplex(monkeypatch, {_PROVIDER_KEY: "routed-profile-scrub-probe"})
    monkeypatch.setenv(_PROVIDER_KEY, "launch-profile-scrub-probe")
    env = _make_run_env({})
    # The overlay rides along only names the scrub already allows: a provider credential
    # stays out whether it came from the launch env or the routed profile's scope.
    assert _PROVIDER_KEY not in env


def test_first_party_context_values_keep_the_process_value(monkeypatch):
    monkeypatch.setenv("BUZZ_MANAGED_AGENT", "1")  # the Buzz context entitles BUZZ_* names
    monkeypatch.setenv("BUZZ_WORKSPACE_ID", "from-context")
    _multiplex(monkeypatch, {"BUZZ_WORKSPACE_ID": "from-routed-dotenv"})
    env = _make_run_env({})
    assert env["BUZZ_WORKSPACE_ID"] == "from-context"


def test_managed_policy_keys_beat_the_routed_scope(monkeypatch):
    monkeypatch.setenv("ORG_POLICY_FLAG", "policy-value")
    monkeypatch.setattr(
        "hermes_cli.env_loader.managed_env_claims", lambda: frozenset({"ORG_POLICY_FLAG"}))
    _multiplex(monkeypatch, {"ORG_POLICY_FLAG": "routed-user-value"})
    env = _make_run_env({})
    assert env["ORG_POLICY_FLAG"] == "policy-value"


def test_background_commands_get_the_same_routed_base(monkeypatch, tmp_path):
    """``terminal(background=True)`` spawns through ProcessRegistry, not ``_make_run_env``: it must
    drop the launch profile's ``.env`` residue and see the served profile's own values too."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    monkeypatch.setenv("MYAPP_AGENT_ID", "from-p1")
    monkeypatch.setenv("LAUNCH_DOTENV_NAME", "launch-residue")
    monkeypatch.setattr("hermes_cli.env_loader.launch_dotenv_keys", lambda: ["LAUNCH_DOTENV_NAME"])
    _multiplex(monkeypatch, {"MYAPP_AGENT_ID": "from-p2"})
    token = set_hermes_home_override(str(tmp_path / "profiles" / "p2"))
    try:
        env = ProcessRegistry._spawn_env({})
    finally:
        reset_hermes_home_override(token)
    assert env["MYAPP_AGENT_ID"] == "from-p2"
    assert "LAUNCH_DOTENV_NAME" not in env


def test_routed_children_expand_managed_values_for_the_target_and_drop_retired_values(monkeypatch, tmp_path):
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.env_loader import _apply_managed_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.environments.local import build_subprocess_env, served_profile_child_env

    managed = tmp_path / "managed"
    managed.mkdir()
    policy = managed / ".env"
    declarations = (
        "ORG_TEMPLATE=${PRIVATE_INPUT}\nORG_CWD=${TERMINAL_CWD:-unset}\n"
        "ORG_ALIAS=${HERMES_API_TIMEOUT:-unset}\n"
    )
    policy.write_text(declarations + "ORG_RETIRED=launch-only\nHERMES_API_TIMEOUT=${PRIVATE_INPUT}\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    for name in ("ORG_TEMPLATE", "ORG_CWD", "ORG_ALIAS", "ORG_RETIRED", "HERMES_API_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRIVATE_INPUT", "launch")
    monkeypatch.setenv("TERMINAL_CWD", "/launch")
    _apply_managed_env()
    assert os.environ["ORG_TEMPLATE"] == "launch"
    assert os.environ["ORG_CWD"] == "/launch"
    assert os.environ["HERMES_API_TIMEOUT"] == "launch"
    policy.write_text(declarations)
    target = tmp_path / "profiles" / "ops"
    target.mkdir(parents=True)
    (target / ".env").write_text("PRIVATE_INPUT=routed\nORG_TEMPLATE=profile\n")
    scope = build_profile_secret_scope(target)
    assert scope["ORG_CWD"] == scope["ORG_ALIAS"] == "unset"
    _multiplex(monkeypatch, {**scope, "ORG_TEMPLATE": "profile"})
    token = set_hermes_home_override(str(target))
    try:
        for env in (
            _make_run_env({}),
            build_subprocess_env(strip_launch_profile=True, scrub_secrets=False),
            served_profile_child_env(),
        ):
            assert env["ORG_TEMPLATE"] == "routed"
            assert env["ORG_CWD"] == env["ORG_ALIAS"] == "unset"
            assert "ORG_RETIRED" not in env
            assert "HERMES_API_TIMEOUT" not in env
    finally:
        reset_hermes_home_override(token)
