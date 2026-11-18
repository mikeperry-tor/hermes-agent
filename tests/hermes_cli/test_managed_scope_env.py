"""Env integration tests — managed .env applied last with override."""
import os

import pytest


@pytest.fixture
def env_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return home, managed


def test_managed_env_beats_user_env(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("OPENAI_API_BASE=https://user.example/v1\n", encoding="utf-8")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_profile_reload_leaves_admin_claims_in_place(env_homes, monkeypatch):
    from hermes_cli.config import reload_env
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_CONFIG_BACKEND", "file")
    monkeypatch.delenv("HERMES_ACP_AUTH_METHOD", raising=False)
    monkeypatch.delenv("RELOAD_TEST_KEY", raising=False)
    (managed / ".env").write_text("HERMES_CONFIG_BACKEND=file\nHERMES_ACP_AUTH_METHOD=\n", encoding="utf-8")
    (home / ".env").write_text("RELOAD_TEST_KEY=first\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)

    (home / ".env").write_text(
        "HERMES_CONFIG_BACKEND=other\nRELOAD_TEST_KEY=second\n", encoding="utf-8",
    )
    assert reload_env() >= 1
    assert reload_env() == 0
    assert os.environ["HERMES_CONFIG_BACKEND"] == "file"
    assert os.environ["HERMES_ACP_AUTH_METHOD"] == ""
    assert os.environ["RELOAD_TEST_KEY"] == "second"


def test_running_process_retains_managed_backend_during_full_load(env_homes, monkeypatch):
    from hermes_cli import config_backend, env_loader

    home, managed = env_homes
    monkeypatch.setenv("HERMES_CONFIG_BACKEND", "file")
    (home / ".env").write_text("HERMES_CONFIG_BACKEND=other\n", encoding="utf-8")
    (managed / ".env").write_text("HERMES_CONFIG_BACKEND=file\n", encoding="utf-8")

    observed = []
    bootstrap = config_backend.bootstrap_deployment

    def observe_then_bootstrap(*args):
        observed.append(config_backend.get_config_backend().name)
        return bootstrap(*args)

    monkeypatch.setattr(config_backend, "bootstrap_deployment", observe_then_bootstrap)
    env_loader.load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    assert observed and set(observed) == {"file"}


def test_deployed_ssh_values_win_through_launch_and_profile_reload(env_homes, monkeypatch):
    from hermes_cli.config import _validate_config_key
    from hermes_cli.env_loader import load_hermes_dotenv
    from tools.terminal_tool import _get_env_config

    assert _validate_config_key("terminal.ssh_config") == (True, None)

    home, managed = env_homes
    monkeypatch.setenv("HERMES_HOME", str(home))
    flags = {
        "TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "executor.example", "TERMINAL_SSH_USER": "runner",
        "TERMINAL_SSH_PORT": "2222", "TERMINAL_SSH_KEY": "/run/pinned/key",
        "TERMINAL_SSH_CONFIG": "/run/pinned/ssh_config",
    }
    for key in flags:
        monkeypatch.delenv(key, raising=False)
    (managed / ".env").write_text("".join(f"{key}={value}\n" for key, value in flags.items()))
    (managed / "config.yaml").write_text("""terminal:
  backend: ssh
  ssh_host: executor.example
  ssh_user: runner
  ssh_port: 2222
  ssh_key: /run/pinned/key
  ssh_config: /run/pinned/ssh_config
""")
    # The user YAML omits ssh_config. Its explicit ssh_host must still come
    # from managed YAML, while the managed dotenv also pins the launch bridge.
    (home / "config.yaml").write_text("terminal:\n  ssh_host: profile.example\n")
    for suffix in ("first", "reload"):
        (home / ".env").write_text(
            f"TERMINAL_ENV=local\nTERMINAL_SSH_HOST={suffix}.example\n"
            f"TERMINAL_SSH_CONFIG=/tmp/{suffix}\n",
        )
        load_hermes_dotenv(hermes_home=str(home), load_external_secrets=False)
        assert {key: os.environ.get(key) for key in flags} == flags
        effective = _get_env_config()
        assert effective["env_type"] == "ssh"
        assert effective["ssh_host"] == "executor.example"
        assert effective["ssh_config"] == "/run/pinned/ssh_config"


def test_no_managed_env_is_noop(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, _managed = env_homes  # managed dir exists but has no .env
    monkeypatch.setenv("SOME_VALUE", "from_shell")
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["SOME_VALUE"] == "from_user"


def test_managed_overrides_keep_layered_interpolation_private(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv, resolve_home_dotenv

    home, managed = env_homes
    project = home.parent / "project.env"
    (home / ".env").write_text("ORG_VALUE=profile\nPROFILE_EXPANSION=${ORG_VALUE}\n")
    project.write_text("PROJECT_EXPANSION=${ORG_VALUE}\n")
    (managed / ".env").write_text("ORG_VALUE=managed:${ORG_VALUE}\n")
    for name in ("ORG_VALUE", "PROFILE_EXPANSION", "PROJECT_EXPANSION"):
        monkeypatch.delenv(name, raising=False)

    effective = resolve_home_dotenv(home)
    for _ in range(2):
        load_hermes_dotenv(hermes_home=home, project_env=project, load_external_secrets=False)
        assert os.environ["ORG_VALUE"] == effective["ORG_VALUE"] == "managed:profile"
        assert os.environ["PROFILE_EXPANSION"] == os.environ["PROJECT_EXPANSION"] == "profile"


def test_optional_managed_dotenv_io_failure_does_not_block_profile_load(env_homes, monkeypatch):
    from unittest.mock import Mock

    from hermes_cli import managed_scope
    from hermes_cli.env_loader import load_hermes_dotenv, resolve_home_dotenv

    home, _managed = env_homes
    monkeypatch.setattr(managed_scope, "get_managed_dir", Mock(side_effect=PermissionError("unavailable")))
    monkeypatch.delenv("PROFILE_ONLY", raising=False)
    (home / ".env").write_text("PROFILE_ONLY=loaded\n")
    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    assert os.environ["PROFILE_ONLY"] == "loaded"
    assert resolve_home_dotenv(home)["PROFILE_ONLY"] == "loaded"


@pytest.mark.parametrize("fallback", ["project.env", ".op.env"])
def test_managed_interpolation_keeps_fallback_assignments_across_reloads(env_homes, monkeypatch, fallback):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("# Profile exists without the fallback assignment\n")
    (home / fallback).write_text("ORG_FALLBACK=default\n")
    (managed / ".env").write_text("ORG_FALLBACK=managed:${ORG_FALLBACK:-none}\n")
    monkeypatch.delenv("ORG_FALLBACK", raising=False)
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    for _ in range(2):
        load_hermes_dotenv(hermes_home=home, project_env=home / "project.env", load_external_secrets=False)
        assert os.environ["ORG_FALLBACK"] == "managed:default"


def test_unreadable_managed_env_keeps_existing_claims_during_reload(env_homes, monkeypatch):
    from pathlib import Path

    from hermes_cli.config import reload_env
    from hermes_cli.config_backend import get_config_backend
    from hermes_cli.env_loader import load_hermes_dotenv
    from agent.secret_scope import build_profile_secret_scope

    home, managed = env_homes
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_CONFIG_BACKEND", "file")
    policy = managed / ".env"
    assignments = (
        "HERMES_CONFIG_BACKEND=file\nORG_VALUE=admin\nORG_TEMPLATE=${PROFILE_SOURCE}\n"
        "ORG_SOURCE=first\nORG_COPY=${ORG_SOURCE}\nORG_SOURCE=last\n"
    )
    policy.write_text(assignments + "ORG_RETIRED=old\n")
    (home / ".env").write_text("PROFILE_SOURCE=launch\n")
    for name in ("PROFILE_SOURCE", "ORG_VALUE", "ORG_TEMPLATE", "ORG_RETIRED", "ORG_SOURCE", "ORG_COPY"):
        monkeypatch.delenv(name, raising=False)
    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    assert os.environ["ORG_TEMPLATE"] == "launch"
    assert os.environ["ORG_COPY"] == "first"
    policy.write_text(assignments)
    (home / ".env").write_text("ORG_RETIRED=profile\n")
    reload_env()
    assert os.environ["ORG_RETIRED"] == "profile"
    (home / ".env").write_text("HERMES_CONFIG_BACKEND=other\nORG_RETIRED=updated\n")
    routed = home.parent / "routed"
    routed.mkdir()
    (routed / ".env").write_text("ORG_VALUE=lower\nPROFILE_SOURCE=routed\nORG_RETIRED=routed\n")

    open_file = Path.open

    def tracked_open(path, *args, **kwargs):
        if path == policy:
            raise PermissionError("managed dotenv temporarily unavailable")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    reload_env()
    assert get_config_backend().name == "file"
    assert os.environ["ORG_RETIRED"] == "updated"
    load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    assert get_config_backend().name == "file"
    scoped = build_profile_secret_scope(routed)
    assert scoped["ORG_VALUE"] == "admin"
    assert scoped["ORG_TEMPLATE"] == "routed"
    assert scoped["ORG_RETIRED"] == "routed"

    assert scoped["ORG_COPY"] == "first"
    assert scoped["ORG_SOURCE"] == "last"


def test_bootstrapped_managed_env_keeps_published_claims_on_read_error(env_homes, monkeypatch):
    from pathlib import Path

    from hermes_cli.config import reload_env
    from hermes_cli.env_loader import apply_config_bootstrap_env, managed_env_claims, resolve_home_dotenv

    home, managed = env_homes
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CONFIG_BACKEND", raising=False)
    monkeypatch.delenv("ORG_BOOTSTRAP_UNUSED", raising=False)
    monkeypatch.delenv("ORG_BOOTSTRAP_VALUE", raising=False)
    (home / ".env").write_text("HERMES_CONFIG_BACKEND=other\nORG_BOOTSTRAP_UNUSED=profile\n")
    policy = managed / ".env"
    policy.write_text("HERMES_CONFIG_BACKEND=file\nORG_BOOTSTRAP_UNUSED=admin\n"
                      "ORG_BOOTSTRAP_VALUE=${ORG_BOOTSTRAP_UNUSED}\n")
    apply_config_bootstrap_env(lambda _kind: {"ORG_BOOTSTRAP_VALUE"}, home)
    assert os.environ["HERMES_CONFIG_BACKEND"] == "file"
    assert os.environ["ORG_BOOTSTRAP_VALUE"] == "admin"
    assert "ORG_BOOTSTRAP_UNUSED" not in os.environ

    open_file = Path.open

    def unreadable(path, *args, **kwargs):
        if path == policy:
            raise PermissionError("managed dotenv temporarily unavailable")
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    expected = {"HERMES_CONFIG_BACKEND": "file", "ORG_BOOTSTRAP_VALUE": "admin"}
    assert managed_env_claims() == expected.keys()
    assert resolve_home_dotenv(home, lookup_env={}, managed_only=True) == expected
    reload_env()
    assert os.environ["HERMES_CONFIG_BACKEND"] == "file"
    assert os.environ["ORG_BOOTSTRAP_UNUSED"] == "profile"


def test_repeated_managed_bootstrap_uses_the_original_environment(env_homes, monkeypatch):
    from hermes_cli.env_loader import _apply_managed_env, apply_config_bootstrap_env

    home, managed = env_homes
    monkeypatch.setenv("ORG_BOOTSTRAP_PATH", "/instance")
    (managed / ".env").write_text("ORG_BOOTSTRAP_PATH=${ORG_BOOTSTRAP_PATH}/base\n")

    for _ in range(2):
        apply_config_bootstrap_env(lambda _kind: {"ORG_BOOTSTRAP_PATH"}, home)
        assert os.environ["ORG_BOOTSTRAP_PATH"] == "/instance/base"
    _apply_managed_env()
    assert os.environ["ORG_BOOTSTRAP_PATH"] == "/instance/base"
